#!/usr/bin/env python3
"""
AI code reviewer for GitHub pull requests.

What it does
------------
1. Works out which files the PR changed, and produces a unified diff per file.
2. Annotates each diff line with its line number in the *new* version of the
   file, so the model can point at a real, commentable line.
3. Asks an OpenAI model for structured JSON findings, one request per file.
4. Posts the findings back to the PR as inline review comments plus one
   rolling summary comment that is edited in place on every push.

Design notes
------------
* Diff-only. The whole repository is never sent to the API, so the cost of a
  review scales with the size of the change, not the size of the codebase.
* Structured output. The model must answer with JSON matching a schema.
  Prose cannot be attached to a line number; JSON can.
* Fail-soft. An API hiccup should not turn a green PR red. Unexpected errors
  are reported and the script exits 0, unless `fail_on_severity` is configured.

Environment
-----------
OPENAI_API_KEY      required
GITHUB_TOKEN        required (the automatic Actions token is enough)
GITHUB_REPOSITORY   "owner/repo"
PR_NUMBER           pull request number
BASE_REF / HEAD_REF commit shas to diff between
AI_REVIEW_CONFIG    path to the YAML config (default .github/ai-review-config.yml)
AI_REVIEW_DRY_RUN   "1" to build prompts and render output without calling
                    OpenAI or GitHub at all. Costs nothing.
AI_REVIEW_NO_POST   "1" to call OpenAI for real but print the review to stdout
                    instead of posting it. Costs tokens, touches no PR.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import yaml

# Hidden HTML marker so the script can find its own comments on a later run
# and update or delete them instead of stacking new ones.
MARKER = "<!-- ai-code-review -->"

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SEVERITIES = ["critical", "high", "medium", "low"]
CATEGORIES = [
    "correctness",
    "security",
    "performance",
    "error-handling",
    "maintainability",
    "testing",
]

DEFAULTS = {
    "model": "gpt-4o-mini",
    "temperature": 0,
    "request_timeout": 90,
    "max_retries": 2,
    "max_files": 40,
    "max_file_diff_bytes": 12000,
    "max_total_diff_bytes": 60000,
    "diff_context_lines": 3,
    "concurrency": 4,
    "min_severity": "low",
    "max_findings_per_file": 10,
    "inline_comments": True,
    "prune_old_comments": True,
    "fail_on_severity": "none",
    "exclude": [],
    "extra_instructions": "",
}

SYSTEM_PROMPT = """You are a meticulous senior engineer reviewing one file of a pull request.

You are shown a unified diff of a single file. Every line is prefixed with its
line number in the NEW version of the file:

    42 + some code      <- a line this PR ADDED
       - some code      <- a line this PR REMOVED (no number; never report on these)
    42   some code      <- unchanged context, shown only for orientation

Rules:
1. Report problems only on lines marked `+`. Those are the lines this pull
   request is responsible for.
2. The `line` field of every finding MUST be one of the numbers printed beside
   a `+` line. Never invent a line number.
3. You are seeing a fragment, not the whole codebase. Do not speculate about
   code you were not shown. Do not report missing imports, undefined names, or
   "this function may not exist" unless the diff itself proves it.
4. Report defects, not preferences. Formatters and linters already run on this
   code, so skip naming, spacing, quote style and import order.
5. An empty findings list is a good answer. Returning nothing is strictly
   better than returning a weak or speculative finding.

Severity:
  critical - data loss, a security hole, or a crash on a normal code path
  high     - a bug real users will hit; an unhandled failure mode
  medium   - a correctness risk under specific conditions; missing error handling
  low      - a maintainability or clarity problem worth mentioning

Keep `title` under 80 characters. Keep `detail` to two sentences: what breaks
and under what conditions. Put concrete code in `suggestion`."""

FINDING_SCHEMA = {
    "name": "code_review_findings",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "line",
                        "severity",
                        "category",
                        "title",
                        "detail",
                        "suggestion",
                    ],
                    "properties": {
                        "line": {
                            "type": "integer",
                            "description": "Line number shown beside a '+' line.",
                        },
                        "severity": {"type": "string", "enum": SEVERITIES},
                        "category": {"type": "string", "enum": CATEGORIES},
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                },
            }
        },
    },
}


def log(message: str) -> None:
    print(f"[ai-review] {message}", flush=True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def load_config(path: str) -> dict:
    config = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        config.update({k: v for k, v in loaded.items() if v is not None})
        log(f"loaded config from {path}")
    else:
        log(f"no config at {path}; using defaults")
    return config


# --------------------------------------------------------------------------
# Git plumbing
# --------------------------------------------------------------------------


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def merge_base(base: str, head: str) -> str:
    """The commit the branch actually forked from.

    Diffing against the base branch *tip* would show unrelated commits that
    landed on main after this PR was opened. The merge base shows only what
    this PR changed.
    """
    try:
        return git("merge-base", base, head).strip()
    except RuntimeError:
        log("merge-base unavailable; falling back to the base ref")
        return base


def changed_files(base: str, head: str) -> list[str]:
    # ACMR = added, copied, modified, renamed. Deleted files have nothing to
    # review, so they are filtered out here.
    output = git("--no-pager", "diff", "--name-status", "--diff-filter=ACMR", base, head)
    paths = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            paths.append(parts[-1].strip())
    return paths


def file_diff(base: str, head: str, path: str, context: int) -> str:
    return git(
        "--no-pager", "diff", f"--unified={context}", base, head, "--", path
    )


def is_excluded(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return True
        if fnmatch.fnmatch(os.path.basename(path), pattern):
            return True
    return False


SKIP_PREFIXES = (
    "diff --git",
    "index ",
    "--- ",
    "+++ ",
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
    "similarity index",
    "rename from",
    "rename to",
    "Binary files",
)


def render_numbered_diff(diff: str) -> tuple[str, set[int]]:
    """Turn a raw unified diff into a line-numbered view.

    Returns the rendered text and the set of line numbers that were ADDED by
    this PR. Only those lines can carry an inline comment: GitHub rejects the
    entire review with a 422 if any comment points outside the diff.
    """
    rendered: list[str] = []
    added: set[int] = set()
    new_line_number = 0

    for raw in diff.splitlines():
        if raw.startswith("@@"):
            # Header looks like: @@ -12,7 +14,9 @@ def some_function():
            try:
                new_segment = raw.split("+", 1)[1].split(" ", 1)[0]
                new_line_number = int(new_segment.split(",")[0])
            except (IndexError, ValueError):
                continue
            rendered.append(f"       {raw}")
            continue

        if raw.startswith(SKIP_PREFIXES):
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue

        if raw.startswith("+"):
            rendered.append(f"{new_line_number:>6} + {raw[1:]}")
            added.add(new_line_number)
            new_line_number += 1
        elif raw.startswith("-"):
            rendered.append(f"{'':>6} - {raw[1:]}")
        else:
            rendered.append(f"{new_line_number:>6}   {raw[1:] if raw else ''}")
            new_line_number += 1

    return "\n".join(rendered), added


# --------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------


def build_user_prompt(path: str, numbered_diff: str, extra: str) -> str:
    sections = [f"File: {path}", "", "Diff:", "```", numbered_diff, "```"]
    if extra.strip():
        sections += ["", "Project-specific instructions:", extra.strip()]
    return "\n".join(sections)


def review_file(client, config: dict, path: str, numbered_diff: str, added: set[int]):
    """Ask the model about one file. Never raises: returns [] on failure."""
    prompt = build_user_prompt(path, numbered_diff, config["extra_instructions"])
    last_error = None

    for attempt in range(int(config["max_retries"]) + 1):
        try:
            response = client.chat.completions.create(
                model=config["model"],
                temperature=config["temperature"],
                timeout=config["request_timeout"],
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_schema", "json_schema": FINDING_SCHEMA},
            )
            payload = json.loads(response.choices[0].message.content or '{"findings":[]}')
            findings = payload.get("findings", [])
            break
        except Exception as error:  # noqa: BLE001 - never let one file kill the run
            last_error = error
            if attempt < int(config["max_retries"]):
                log(f"{path}: attempt {attempt + 1} failed ({error}); retrying")
    else:
        log(f"{path}: giving up after retries ({last_error})")
        return []

    cleaned = []
    for finding in findings:
        line = finding.get("line")
        finding["path"] = path
        # The model was told to use added lines only. Trust but verify: a
        # finding on a non-added line still gets reported, just without an
        # inline anchor, because an invalid anchor would 422 the whole review.
        finding["anchored"] = isinstance(line, int) and line in added
        cleaned.append(finding)

    minimum = SEVERITY_ORDER.get(str(config["min_severity"]).lower(), 0)
    cleaned = [
        f for f in cleaned if SEVERITY_ORDER.get(f.get("severity", "low"), 0) >= minimum
    ]
    cleaned.sort(
        key=lambda f: -SEVERITY_ORDER.get(f.get("severity", "low"), 0)
    )
    return cleaned[: int(config["max_findings_per_file"])]


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------


def gh(method: str, url: str, token: str, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8")
            return response.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001
        return 0, str(error)


def prune_previous_inline_comments(repo: str, pr: int, token: str) -> None:
    """Remove this bot's inline comments from earlier runs.

    Without this, pushing five times to a PR leaves five copies of the same
    comment on the same line.
    """
    status, data = gh(
        "GET",
        f"https://api.github.com/repos/{repo}/pulls/{pr}/comments?per_page=100",
        token,
    )
    if status != 200 or not isinstance(data, list):
        return
    removed = 0
    for comment in data:
        if MARKER in (comment.get("body") or ""):
            code, _ = gh(
                "DELETE",
                f"https://api.github.com/repos/{repo}/pulls/comments/{comment['id']}",
                token,
            )
            removed += 1 if code in (200, 204) else 0
    if removed:
        log(f"removed {removed} inline comment(s) from a previous run")


def post_review(repo: str, pr: int, head_sha: str, token: str, body: str, comments):
    payload = {"body": body, "event": "COMMENT", "commit_id": head_sha}
    if comments:
        payload["comments"] = comments
    status, data = gh(
        "POST", f"https://api.github.com/repos/{repo}/pulls/{pr}/reviews", token, payload
    )
    if status in (200, 201):
        return True
    # A 422 almost always means one comment pointed at a line GitHub does not
    # consider part of the diff. Retry without inline comments so the findings
    # still reach the author.
    log(f"review with inline comments rejected ({status}): {data}")
    if comments:
        status, data = gh(
            "POST",
            f"https://api.github.com/repos/{repo}/pulls/{pr}/reviews",
            token,
            {"body": body, "event": "COMMENT", "commit_id": head_sha},
        )
        if status in (200, 201):
            log("posted summary-only review instead")
            return True
    log(f"could not post review ({status}): {data}")
    return False


def upsert_summary_comment(repo: str, pr: int, token: str, body: str) -> None:
    """Edit the previous summary comment if there is one, else create it."""
    status, data = gh(
        "GET",
        f"https://api.github.com/repos/{repo}/issues/{pr}/comments?per_page=100",
        token,
    )
    if status == 200 and isinstance(data, list):
        for comment in data:
            if MARKER in (comment.get("body") or ""):
                gh(
                    "PATCH",
                    f"https://api.github.com/repos/{repo}/issues/comments/{comment['id']}",
                    token,
                    {"body": body},
                )
                log("updated the existing summary comment")
                return
    gh(
        "POST",
        f"https://api.github.com/repos/{repo}/issues/{pr}/comments",
        token,
        {"body": body},
    )
    log("created a new summary comment")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def inline_comment_body(finding: dict) -> str:
    return (
        f"{MARKER}\n"
        f"**{finding['severity'].upper()} / {finding['category']}** - {finding['title']}\n\n"
        f"{finding['detail']}\n\n"
        f"**Suggested fix**\n{finding['suggestion']}"
    )


def summary_body(config: dict, findings: list, reviewed: list, skipped: list) -> str:
    counts = {s: sum(1 for f in findings if f.get("severity") == s) for s in SEVERITIES}
    tally = ", ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts[s])

    lines = [MARKER, "## AI code review", ""]
    if not findings:
        lines += [
            f"No issues flagged across **{len(reviewed)}** changed file(s).",
            "",
        ]
    else:
        lines += [
            f"**{len(findings)}** finding(s) across **{len(reviewed)}** changed file(s) "
            f"({tally}).",
            "",
            "| Severity | Category | File | Line | Issue |",
            "| --- | --- | --- | --- | --- |",
        ]
        for finding in findings:
            title = finding["title"].replace("|", "\\|")
            line = finding["line"] if finding.get("anchored") else f"~{finding['line']}"
            lines.append(
                f"| {finding['severity']} | {finding['category']} | "
                f"`{finding['path']}` | {line} | {title} |"
            )
        lines.append("")

        unanchored = [f for f in findings if not f.get("anchored")]
        if unanchored:
            lines += [
                "<details><summary>Findings that could not be attached to a diff line"
                f" ({len(unanchored)})</summary>",
                "",
            ]
            for finding in unanchored:
                lines += [
                    f"**`{finding['path']}` around line {finding['line']}** - "
                    f"{finding['severity']} / {finding['category']}",
                    "",
                    f"{finding['title']}. {finding['detail']}",
                    "",
                    f"_Suggested fix:_ {finding['suggestion']}",
                    "",
                ]
            lines += ["</details>", ""]

    if skipped:
        lines += [
            f"<details><summary>Skipped {len(skipped)} file(s)</summary>",
            "",
        ]
        lines += [f"- `{path}` - {reason}" for path, reason in skipped]
        lines += ["", "</details>", ""]

    lines += [
        "---",
        f"_Reviewed by `{config['model']}` on the diff only. This is a second "
        "opinion, not a gate - confirm anything before acting on it._",
    ]
    return "\n".join(lines)


def write_step_summary(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(body.replace(MARKER, "") + "\n")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    dry_run = os.environ.get("AI_REVIEW_DRY_RUN") == "1"
    no_post = os.environ.get("AI_REVIEW_NO_POST") == "1"
    offline = dry_run or no_post
    config = load_config(
        os.environ.get("AI_REVIEW_CONFIG", ".github/ai-review-config.yml")
    )

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = int(os.environ.get("PR_NUMBER") or 0)
    token = os.environ.get("GITHUB_TOKEN", "")
    base_ref = os.environ.get("BASE_REF", "")
    head_ref = os.environ.get("HEAD_REF", "HEAD")

    if not offline and not all([repo, pr_number, token, base_ref]):
        log("missing one of GITHUB_REPOSITORY / PR_NUMBER / GITHUB_TOKEN / BASE_REF")
        return 0

    base = merge_base(base_ref, head_ref)
    log(f"diffing {base[:8]}..{head_ref[:8]}")

    paths = changed_files(base, head_ref)
    log(f"{len(paths)} changed file(s)")

    reviewed: list[tuple[str, str, set[int]]] = []
    skipped: list[tuple[str, str]] = []
    budget = int(config["max_total_diff_bytes"])

    for path in paths:
        if len(reviewed) >= int(config["max_files"]):
            skipped.append((path, f"file limit of {config['max_files']} reached"))
            continue
        if is_excluded(path, config["exclude"] or []):
            skipped.append((path, "excluded by config"))
            continue

        raw = file_diff(base, head_ref, path, int(config["diff_context_lines"]))
        if "Binary files" in raw.split("\n@@")[0]:
            skipped.append((path, "binary file"))
            continue
        size = len(raw.encode("utf-8"))
        if size > int(config["max_file_diff_bytes"]):
            skipped.append((path, f"diff too large ({size} bytes)"))
            continue
        if size > budget:
            skipped.append((path, "run-wide token budget exhausted"))
            continue
        budget -= size

        numbered, added = render_numbered_diff(raw)
        if not added:
            skipped.append((path, "no added lines"))
            continue
        reviewed.append((path, numbered, added))

    log(f"reviewing {len(reviewed)} file(s), skipping {len(skipped)}")

    findings: list[dict] = []

    if dry_run:
        for path, numbered, added in reviewed:
            log(f"DRY RUN {path}: {len(added)} added line(s) -> {sorted(added)[:12]}")
            print(build_user_prompt(path, numbered, config["extra_instructions"]))
            print("-" * 70)
        fixture = os.environ.get("AI_REVIEW_FAKE_FINDINGS")
        if fixture and os.path.exists(fixture):
            with open(fixture, "r", encoding="utf-8") as handle:
                findings = json.load(handle)
            for finding in findings:
                anchors = {p: a for p, _, a in reviewed}
                finding["anchored"] = finding["line"] in anchors.get(finding["path"], set())
    elif reviewed:
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        workers = max(1, int(config["concurrency"]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(review_file, client, config, path, numbered, added): path
                for path, numbered, added in reviewed
            }
            for future in concurrent.futures.as_completed(futures):
                findings.extend(future.result())

    findings.sort(
        key=lambda f: (
            -SEVERITY_ORDER.get(f.get("severity", "low"), 0),
            f.get("path", ""),
            f.get("line", 0),
        )
    )
    log(f"{len(findings)} finding(s)")

    body = summary_body(config, findings, reviewed, skipped)

    if offline:
        print("=" * 70)
        print(body)
        print("=" * 70)
        log("offline mode: nothing was posted to GitHub")
    else:
        if config["prune_old_comments"]:
            prune_previous_inline_comments(repo, pr_number, token)

        comments = []
        if config["inline_comments"]:
            comments = [
                {
                    "path": f["path"],
                    "line": f["line"],
                    "side": "RIGHT",
                    "body": inline_comment_body(f),
                }
                for f in findings
                if f.get("anchored")
            ]

        head_sha = git("rev-parse", head_ref).strip()
        posted = post_review(repo, pr_number, head_sha, token, MARKER + "\nAI review complete.", comments) if comments else True
        if not posted:
            log("inline review failed; the summary comment still carries every finding")
        upsert_summary_comment(repo, pr_number, token, body)

    write_step_summary(body)

    threshold = str(config["fail_on_severity"]).lower()
    if threshold != "none":
        limit = SEVERITY_ORDER.get(threshold, 99)
        blocking = [
            f
            for f in findings
            if SEVERITY_ORDER.get(f.get("severity", "low"), 0) >= limit
        ]
        if blocking:
            log(f"failing: {len(blocking)} finding(s) at or above '{threshold}'")
            return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001
        # A broken reviewer must not block a pull request.
        log(f"unexpected failure: {error}")
        sys.exit(0)
