# Personal build (`carlos-custom`)

`carlos-custom` is upstream `main` plus seven fork feature branches, merged (never rebased):

| Branch | Adds |
|---|---|
| `feat/securecrt-style-profile` | Network-output highlighting (includes `feat/network-output-highlighting-pr`) |
| `feat/highlight-profile-editor` | Custom highlight profile and theme editor |
| `feat/securecrt-r4` | SecureCRT R4 terminal highlighting patterns and word handling |
| `feat/session-logging` | SecureCRT-style automatic session logging |
| `feat/securecrt-session-import` | SecureCRT session converter |
| `feat/terminal-reflow` | Correct pyte terminal resizing and ConPTY repaint behavior |
| `feat/reflow-visible-windows` | Reflow the visible screen under the bundled ConPTY |

## Install

Replace `<your-github-owner>` in the clone URL with the repository owner's GitHub handle.

Windows (PowerShell):

```powershell
git clone https://github.com/<your-github-owner>/sshpilot.git
cd sshpilot
git checkout carlos-custom
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1
```

Linux:

```sh
git clone https://github.com/<your-github-owner>/sshpilot.git
cd sshpilot
git checkout carlos-custom
scripts/install-run-linux.sh
```

## Highlighting

**Settings → Terminal → Highlighting**: switch on *Highlight Network Output*, then pick
*Highlight Profile* (Generic, Cisco IOS, Cisco (SecureCRT-style), Junos, Huawei VRP) and
*Highlight Colour Theme* (the theme selector is active for the SecureCRT-style profile).
Details: [highlighting.md](highlighting.md).

## Session Logging

**Settings → Terminal → Session Logging**: switch on *Log Terminal Sessions*, set the
*Log folder* and the file-name pattern. Example:

- Folder: `C:\Users\<user>\Documents\Logs` (Linux: `~/sshpilot-logs`)
- Pattern: `%Y-%M-%D\%S (%H) -%Y-%M-%D- %h-%m.log`

gives `...\Logs\2026-10-19\web (192.0.2.10) -2026-10-19- 09-30.log`.

The log gets the raw output from the daemon *before* highlighting is applied, so it holds
plain text only: no colour escapes, even with highlighting on.

## SecureCRT importer

Convert the SecureCRT `Sessions` folder, then use **Import connections → From Ásbrú…**:

```powershell
python .\scripts\securecrt_to_asbru.py "$env:APPDATA\VanDyke\Config\Sessions" --output "$PWD\securecrt-import.yml"
```

```sh
python3 scripts/securecrt_to_asbru.py ~/securecrt/Config/Sessions --output ./securecrt-import.yml
```

Details: [importing-securecrt.md](importing-securecrt.md).

## Update

From the `carlos-custom` checkout:

```sh
scripts/update-custom.sh          # Linux
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\update-custom.ps1   # Windows
```

It fetches `upstream` and `origin`, merges `upstream/main` and then the seven feature
branches into `carlos-custom` (merge, not rebase), runs ruff and the highlight / logging /
import / terminal-reflow tests (including the custom profile editor and SecureCRT word tests), and prints what changed. On a merge conflict it stops and tells you how to
finish or abort. It never pushes: publish with `git push origin carlos-custom` (never force).
