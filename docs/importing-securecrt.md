# Importing SecureCRT sessions

SSH Pilot does not read SecureCRT configuration directly. Convert the Sessions
folder to an Asbru-compatible JSON document, then use SSH Pilot's existing
**Import connections → From Ásbrú…** preview and apply flow.

## Convert the sessions

The converter is a repository helper and requires an SSH Pilot source checkout;
it is not installed with packaged builds. It uses only Python's standard
library. Give it the SecureCRT Sessions directory and a new output filename. It
never edits SecureCRT files or SSH Pilot configuration.

Windows PowerShell (use the actual profile directory if it differs):

```powershell
python .\scripts\securecrt_to_asbru.py `
  "$env:APPDATA\VanDyke\Config\Sessions" `
  --output "$PWD\securecrt-import.yml" `
  --backup-output "$PWD\securecrt-non-ssh.json"
```

Linux (pass the directory where the SecureCRT sessions were copied):

```sh
python3 scripts/securecrt_to_asbru.py ~/securecrt/Config/Sessions \
  --output ./securecrt-import.yml \
  --backup-output ./securecrt-non-ssh.json
```

The Ásbrú output remains SSH-only. The optional backup output contains Telnet
and Serial sessions. A `.json` backup is accepted directly by SSH Pilot; use a
`.spbk` filename instead to create an unencrypted SSH Pilot backup archive.
Keep both output paths separate from the SecureCRT Sessions directory.

## Field mapping

| SecureCRT value | Imported value |
| --- | --- |
| Nested directories | Nested SSH Pilot groups |
| `.ini` filename | Connection display name; SSH Pilot sanitizes it to a unique alias |
| `Hostname`, `Username` | Host and login user |
| `Protocol Name` set to SSH/SSH2 | Imported as SSH through the Ásbrú output |
| `Protocol Name` set to Telnet | Imported through the backup output; `[Telnet] Port` defaults to 23 |
| `Protocol Name` set to Serial | Imported through the backup output; `Com Port`, `Baud Rate`, `Flow Control`, `Data Bits`, `Parity`, and `Stop Bits` are mapped |
| `[SSH2] Port` | SSH port; DWORD values are hexadecimal, string values are decimal |
| `Identity Filename V2`, then `Identity Filename` | SSH identity file path |
| `Firewall Name` | Jump host resolved only when prefixed with `Session:`; a path is matched first, otherwise a unique filename is used only when no folder is given |

UTF-8, Latin-1, and UTF-16 BOM files are supported. `Default.ini`,
`__FolderData__.ini`, and symlinked session files are skipped. Indented lines
inside multiline SecureCRT values are not treated as records. Telnet and Serial
are omitted from the Ásbrú file but included when `--backup-output` is given;
other protocols are only listed. Telnet uses `[Telnet] Port` (default 23), SSH1
uses `[SSH1] Port` (default 22), RLogin uses 513, and Serial has no TCP port
(the backup schema uses port 22 as a harmless placeholder). Missing hostnames
are skipped with a warning. Malformed port
values are reported and use that protocol's default. `Firewall Name=None` is
ignored; ambiguous filename matches are reported and not imported as a jump
host. Unresolved `${VDS_USER_DATA_PATH}` or `${VDS_CONFIG_PATH}` variables in an
identity path are reported and left intact. The converter resolves those
variables from the process environment when they are defined. Other `${...}`
variables are left intact and reported. Windows drive-letter identity paths
are passed through and reported. Hostname and username values beginning with
`-` or containing whitespace/control characters are rejected with a warning so
they cannot become SSH command options.

Choose a new output filename whose parent directory already exists. The
converter refuses to overwrite files and refuses `.ini` output inside the
Sessions folder.

## Credential handling

Password, Password V2, passphrase, credential, and secret records are excluded
from both generated documents. Passwords are never imported. The converter does
not map or print their values.
Identity file paths are included, but key file contents are never read or
copied. The generated files contain hostnames and IP addresses: review them and
the converter warnings, then delete the files after a successful import.

## Import into SSH Pilot

Open SSH Pilot, choose **Import connections**, then **From Ásbrú…**. Select the
generated `securecrt-import.yml`, review the preview counts and warnings, and
apply the import. The import dialog provides the existing preview and apply
confirmation before connections are added.

Then open **Backup/Restore → Restore**, select `securecrt-non-ssh.json` (or the
`.spbk` form), choose **Merge**, and apply the restore. Merge mode adds the
Telnet/Serial sessions and preserves existing settings and connections.

On Windows, enable/install the optional Telnet client before opening Telnet
sessions. Serial sessions require PuTTY's `plink.exe` to be installed and
available on `PATH`; these are the programs used by SSH Pilot's built-in Telnet
and Serial plugins.

SecureCRT's public documentation describes the serial fields and defaults, but
not their private `.ini` numeric encodings. The converter therefore uses the
best-known DWORD mappings (including the Windows parity/stop-bit constants) and
falls back to SecureCRT's documented defaults with a warning for unknown values.
