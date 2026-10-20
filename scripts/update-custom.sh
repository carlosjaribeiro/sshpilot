#!/usr/bin/env bash
# Update the personal build: merge upstream/main into carlos-custom, re-merge the
# feature branches, run ruff + the feature tests. Merges only; never pushes.
set -euo pipefail
cd "$(dirname "$0")/.."

FEATURES=(feat/securecrt-style-profile feat/highlight-profile-editor feat/securecrt-r4 feat/session-logging feat/securecrt-session-import feat/terminal-reflow)
PY="${PYTHON:-python3}"

[ "$(git rev-parse --abbrev-ref HEAD)" = carlos-custom ] || { echo "Check out carlos-custom first." >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "Commit or stash your changes first." >&2; exit 1; }

conflict() {
    echo >&2
    echo "Merge of $1 stopped on conflicts. Fix them, then:" >&2
    echo "  git add <files> && git commit      (finish the merge)" >&2
    echo "  scripts/update-custom.sh           (continue)" >&2
    echo "Or give up on it with: git merge --abort" >&2
    exit 1
}

git remote get-url upstream >/dev/null 2>&1 || git remote add upstream https://github.com/mfat/sshpilot.git
git fetch upstream
git fetch origin
before=$(git rev-parse HEAD)

for ref in upstream/main "${FEATURES[@]/#/origin/}"; do
    if git merge-base --is-ancestor "$ref" HEAD; then
        echo "up to date: $ref"
    else
        git merge --no-ff -m "Merge $ref into carlos-custom" "$ref" || conflict "$ref"
    fi
done

"$PY" -m ruff check src tests scripts/generate_api_artifacts.py scripts/securecrt_to_asbru.py
"$PY" -m pytest -q tests/test_terminal_highlight.py tests/test_terminal_session_log.py \
    tests/test_terminal_highlight_custom.py tests/test_terminal_highlight_words.py \
    tests/test_terminal_session_log_widget.py tests/core/test_securecrt_to_asbru.py \
    tests/test_highlight_session_log_integration.py tests/test_pyte_terminal_reflow.py \
    tests/test_pyte_terminal_model.py tests/test_pyte_csi_normalizer.py

echo
echo "Changed since $before:"
git log --oneline --no-merges "$before..HEAD"
git diff --stat "$before" HEAD | tail -1
echo
echo "Publish when happy (normal push, no force):"
echo "  git push origin carlos-custom"
