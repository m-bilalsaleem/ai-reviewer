#!/usr/bin/env bash
#
# Test the AI reviewer without touching a real repository or a real pull request.
#
#   ./tests/run_local_test.sh dry     no API call, no cost. Checks the plumbing:
#                                     diffing, exclusions, line numbering.
#   ./tests/run_local_test.sh live    calls OpenAI for real, prints the review,
#                                     posts nothing. Costs about one cent.
#
# Builds a throwaway git repo in a temp directory, commits a clean file, then
# commits a version with seven planted bugs, and reviews the difference.

set -euo pipefail

MODE="${1:-dry}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$MODE" != "dry" && "$MODE" != "live" ]]; then
  echo "usage: $0 [dry|live]" >&2
  exit 2
fi

if [[ "$MODE" == "live" && -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: live mode needs OPENAI_API_KEY in your environment." >&2
  echo "  export OPENAI_API_KEY='sk-...'" >&2
  exit 2
fi

PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c "import yaml" 2>/dev/null; then
  echo "ERROR: PyYAML is missing. Install the dependencies first:" >&2
  echo "  $PYTHON -m pip install -r .github/scripts/requirements.txt" >&2
  exit 2
fi
if [[ "$MODE" == "live" ]] && ! "$PYTHON" -c "import openai" 2>/dev/null; then
  echo "ERROR: the openai package is missing. Install the dependencies first:" >&2
  echo "  $PYTHON -m pip install -r .github/scripts/requirements.txt" >&2
  exit 2
fi

WORK="$(mktemp -d 2>/dev/null || mktemp -d -t ai-review)"
trap 'rm -rf "$WORK"' EXIT

echo "==> scratch repo: $WORK"
mkdir -p "$WORK/src"
cd "$WORK"
git init -q .
git config user.email "test@example.com"
git config user.name "AI Review Test"

# Copy the reviewer itself into the scratch repo.
mkdir -p "$WORK/.github/scripts"
cp "$ROOT/.github/scripts/ai_review.py" "$WORK/.github/scripts/"
cp "$ROOT/.github/ai-review-config.yml" "$WORK/.github/"

# --- commit 1: the clean baseline -------------------------------------------
cp "$ROOT/tests/fixtures/payments_before.py" src/payments.py
# Two files that SHOULD be ignored, to prove the exclusion rules work.
echo '{"lockfileVersion": 1}' > package-lock.json
mkdir -p dist && echo 'var a=1;' > dist/app.min.js
git add -A
git commit -qm "baseline"
BASE="$(git rev-parse HEAD)"

# --- commit 2: the pull request under review --------------------------------
cp "$ROOT/tests/fixtures/payments_after.py" src/payments.py
echo '{"lockfileVersion": 2}' > package-lock.json
echo 'var a=2;' > dist/app.min.js
git add -A
git commit -qm "add payment helpers"
HEAD_SHA="$(git rev-parse HEAD)"

echo "==> reviewing ${BASE:0:8}..${HEAD_SHA:0:8} in $MODE mode"
echo

if [[ "$MODE" == "dry" ]]; then
  export AI_REVIEW_DRY_RUN=1
else
  export AI_REVIEW_NO_POST=1
fi

export BASE_REF="$BASE"
export HEAD_REF="$HEAD_SHA"
export AI_REVIEW_CONFIG=".github/ai-review-config.yml"

set +e
"$PYTHON" .github/scripts/ai_review.py
STATUS=$?
set -e

echo
echo "==> reviewer exited with $STATUS"
if [[ "$MODE" == "dry" ]]; then
  cat <<'NOTE'

Dry mode checks the plumbing only. What you should see above:
  * exactly one file reviewed: src/payments.py
  * package-lock.json and dist/app.min.js listed as skipped
  * a numbered diff where the line beside STRIPE_SECRET matches the real
    line number in tests/fixtures/payments_after.py

Nothing was sent to OpenAI, so there are no findings. To see real findings:
  ./tests/run_local_test.sh live
NOTE
else
  cat <<'NOTE'

Now grade the findings against tests/ANSWER_KEY.md. The hardcoded secret,
the SQL injection and the swallowed exception should all be reported. Check
that their line numbers point at the real defects.
NOTE
fi
