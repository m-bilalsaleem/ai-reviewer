# AI Code Review for GitHub Pull Requests

An OpenAI-powered reviewer that runs as a GitHub Action. On every pull request
it reads the diff, asks a model to flag real defects, and posts the findings
back to the PR as inline comments plus one rolling summary comment.

```
PR opened / pushed
   |
   v
GitHub Actions  ->  git diff (changed files only)
   |
   v
filter + budget  ->  drop lockfiles, binaries, oversized diffs
   |
   v
OpenAI (structured JSON findings, one call per file)
   |
   v
inline PR comments  +  summary comment  +  Actions job summary
```

## Files

| Path | Purpose |
| --- | --- |
| `.github/workflows/ai-code-review.yml` | The trigger. Runs on PR open / push / reopen. |
| `.github/scripts/ai_review.py` | All the logic: diffing, prompting, posting. |
| `.github/ai-review-config.yml` | Tuning: model, cost caps, exclusions, severity. |
| `.github/scripts/requirements.txt` | Two dependencies: `openai`, `PyYAML`. |

## Setup

1. Copy the `.github` directory into your repository.
2. Add your OpenAI key as a repository secret:
   **Settings -> Secrets and variables -> Actions -> New repository secret**
   - Name: `OPENAI_API_KEY`
   - Value: your key (`sk-...`)
3. Commit, push, and open a pull request.

Nothing else is needed. The workflow uses the `GITHUB_TOKEN` that Actions
provides automatically, scoped to read code and write PR comments only.

## How it decides what to review

Only the **diff** is sent to OpenAI, never the whole repository, so cost tracks
the size of the change rather than the size of the codebase. Each diff line is
labelled with its line number in the new version of the file, which is what
lets a finding be attached to the exact line in the PR.

Files are skipped when they are excluded by config (lockfiles, `dist/`,
`node_modules/`, images), are binary, have a diff larger than
`max_file_diff_bytes`, or arrive after the run-wide `max_total_diff_bytes`
budget is spent. Every skipped file is listed in the summary comment, so a
silent skip never happens.

## Cost

With `gpt-4o-mini` and the default caps, a typical PR costs roughly **$0.01 to
$0.05**. The hard ceiling per run is `max_total_diff_bytes` (60 KB of diff),
which is what stops one accidental 5,000-file PR from running up a bill.
Switching `model` to `gpt-4o` improves reasoning on subtle logic at roughly
10x the cost.

## Tuning

Everything lives in `.github/ai-review-config.yml`:

- `model` - swap between `gpt-4o-mini` and `gpt-4o`
- `min_severity` - drop `low` findings once the noise floor bothers you
- `exclude` - add your own generated or vendored paths
- `extra_instructions` - encode team conventions, e.g.
  `"We use structlog. Flag any use of print() in library code."`
- `fail_on_severity` - `none` by default. Set to `critical` or `high` once you
  trust the output, then add the check to branch protection to block merges.

## Design decisions worth knowing

**Fork PRs are skipped.** GitHub does not expose repository secrets to
pull requests opened from a fork, by design. The workflow detects this and
exits with a notice rather than failing the check with an auth error an outside
contributor cannot fix.

**Comments do not stack.** Each run deletes the inline comments it left on the
previous run and edits its summary comment in place, so pushing five times to a
PR does not leave five copies of the same comment.

**A finding on a line outside the diff cannot be an inline comment.** GitHub
rejects the *entire* review with a 422 if any single comment points at an
uncommentable line. Those findings are collected into a collapsible section of
the summary comment instead, marked with a `~` in the table.

**The reviewer never blocks a PR by accident.** Any unexpected error is logged
and the script exits 0. The only path to a non-zero exit is a deliberate
`fail_on_severity` setting.

## Limitations

This is a second opinion, not a gate. It is reliable on hardcoded secrets,
SQL built by string concatenation, unhandled `None`, swallowed exceptions,
missing error handling and obvious N+1 queries. It is unreliable on
architectural judgement, and because it only sees a diff it cannot reason about
how a change interacts with code it was not shown. Expect occasional false
positives, and keep `fail_on_severity: none` until you have watched it on real
PRs for a few weeks.

## Testing it locally

Three rungs, cheapest first. Do not skip to the top one.

### 1. Plumbing check - free, no network

```bash
./tests/run_local_test.sh dry
```

Builds a throwaway git repo in a temp directory, commits a clean file, then
commits a version with seven planted bugs, and runs the reviewer against the
difference without calling OpenAI. Confirms that the diff is extracted, the
exclusion rules fire, and the line numbers in the annotated diff match the real
file. If line numbers are wrong here, inline comments would land on the wrong
lines on a real PR.

### 2. Model check - costs about one cent, posts nothing

```bash
python3 -m pip install -r .github/scripts/requirements.txt
export OPENAI_API_KEY='sk-...'
./tests/run_local_test.sh live
```

Same scratch repo, but the findings are real. Grade them against
`tests/ANSWER_KEY.md`, which lists the seven planted defects and which ones a
working reviewer should catch. This is the run that tells you whether the model
and prompt are actually earning their keep.

### 3. End-to-end check - a real pull request

In a throwaway repository (not your main one):

```bash
git checkout -b test-ai-review
cp tests/fixtures/payments_after.py src/payments.py   # or paste in any bad code
git add -A && git commit -m "test the reviewer"
git push -u origin test-ai-review
```

Open the PR and watch the Actions tab. What to verify:

- the **AI Code Review** check appears and goes green
- inline comments land on the correct lines
- one summary comment appears at the bottom of the conversation
- push a second commit, then confirm the summary comment was **edited** rather
  than duplicated, and that old inline comments were removed

### Testing the failure paths

| What to test | How | Expected |
| --- | --- | --- |
| Missing secret | Temporarily rename the `OPENAI_API_KEY` secret | Check passes with a warning annotation, does not fail |
| Fork PR | Open a PR from a fork of the repo | Check passes with a notice, no API call |
| Blocking mode | Set `fail_on_severity: critical`, push the fixture | Check fails; exit code 1 |
| Huge diff | Set `max_total_diff_bytes: 500`, push anything | Files listed under "Skipped" with "budget exhausted" |

### Reading the Actions logs

Every line the reviewer prints is prefixed `[ai-review]`. The useful ones:

```
[ai-review] N changed file(s)
[ai-review] reviewing N file(s), skipping N
[ai-review] N finding(s)
```

If `reviewing 0 file(s)` appears on a PR that clearly changed code, your
`exclude` patterns are too aggressive. If findings are produced but no comment
appears, look for a 403 in the log - that means the `permissions` block in the
workflow was edited.
