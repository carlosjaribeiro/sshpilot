# Update the personal build: merge upstream/main into carlos-custom, re-merge the
# feature branches, run ruff + the feature tests. Merges only; never pushes.
#   powershell -ExecutionPolicy Bypass -File scripts\update-custom.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$features = "feat/securecrt-style-profile", "feat/highlight-profile-editor", "feat/securecrt-r4", "feat/session-logging", "feat/securecrt-session-import", "feat/terminal-reflow", "feat/reflow-visible-windows"
$py = if (Test-Path ".venv-win\Scripts\python.exe") { ".venv-win\Scripts\python.exe" } else { "python" }

function Git { & git @args; if ($LASTEXITCODE -ne 0) { throw "git $args failed" } }

if ((git rev-parse --abbrev-ref HEAD) -ne "carlos-custom") { throw "Check out carlos-custom first." }
if (git status --porcelain --untracked-files=no) { throw "Commit or stash your changes first." }

git remote get-url upstream 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Git remote add upstream https://github.com/mfat/sshpilot.git }
Git fetch upstream
Git fetch origin
$before = git rev-parse HEAD

foreach ($ref in @("upstream/main") + ($features | ForEach-Object { "origin/$_" })) {
    git merge-base --is-ancestor $ref HEAD
    if ($LASTEXITCODE -eq 0) { Write-Host "up to date: $ref"; continue }
    git merge --no-ff -m "Merge $ref into carlos-custom" $ref
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`nMerge of $ref stopped on conflicts. Fix them, then:"
        Write-Host "  git add <files>; git commit      (finish the merge)"
        Write-Host "  scripts\update-custom.ps1        (continue)"
        Write-Host "Or give up on it with: git merge --abort"
        exit 1
    }
}

& $py -m ruff check src tests scripts/generate_api_artifacts.py scripts/securecrt_to_asbru.py
if ($LASTEXITCODE -ne 0) { throw "ruff failed" }
& $py -m pytest -q tests/test_terminal_highlight.py tests/test_terminal_session_log.py `
    tests/test_terminal_highlight_custom.py tests/test_terminal_highlight_words.py `
    tests/test_terminal_session_log_widget.py tests/core/test_securecrt_to_asbru.py `
    tests/test_highlight_session_log_integration.py tests/test_pyte_terminal_reflow.py `
    tests/test_pyte_terminal_model.py tests/test_pyte_csi_normalizer.py
if ($LASTEXITCODE -ne 0) { throw "tests failed" }

Write-Host "`nChanged since ${before}:"
git log --oneline --no-merges "$before..HEAD"
git diff --stat $before HEAD | Select-Object -Last 1
Write-Host "`nPublish when happy (normal push, no force):"
Write-Host "  git push origin carlos-custom"
