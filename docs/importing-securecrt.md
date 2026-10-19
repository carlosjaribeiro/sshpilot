# Importing SecureCRT sessions

SSH Pilot does not read SecureCRT configuration directly. Convert the Sessions
folder to an Asbru-compatible JSON document, then use SSH Pilot's existing
**Import connections → From Ásbrú…** preview and apply flow.

## Convert the sessions

The converter uses only Python's standard library. Give it the SecureCRT
Sessions directory and a new output filename. It never edits SecureCRT files or
SSH Pilot configuration.

Windows PowerShell (use the actual profile directory if it differs):

```powershell
python .\scripts\securecrt_to_asbru.py `
  "$env:APPDATA\VanDyke\Config\Sessions" `
  --output "$PWD\securecrt-import.yml"
```

Linux (pass the directory where the SecureCRT sessions were copied):

```sh
python3 scripts/securecrt_to_asbru.py ~/securecrt/Config/Sessions \
  --output ./securecrt-import.yml
```

The output is JSON, which is valid YAML and is accepted by the Asbru importer.
Keep the output path separate from the SecureCRT Sessions directory.

## Field mapping

| SecureCRT value | Imported value |
| --- | --- |
| Nested directories | Nested SSH Pilot groups |
| `.ini` filename | Connection display name; SSH Pilot sanitizes it to a unique alias |
| `Hostname`, `Username` | Host and login user |
| `Protocol Name` set to SSH/SSH2 | Imported as SSH |
| `[SSH2] Port` DWORD | SSH port (hexadecimal DWORD, for example `00000016` is 22) |
| `Identity Filename V2` | SSH identity file path |
| `Firewall Name` | Jump host only when it matches a known SSH session by filename |

`Default.ini` and `__FolderData__.ini` are ignored. Sessions using Telnet or any
other protocol are skipped and listed with their protocol, host, and port so
they can be added manually. Missing hostnames are skipped with a warning.
Malformed port DWORDs are reported and default to port 22. Unresolved
`${VDS_USER_DATA_PATH}` or `${VDS_CONFIG_PATH}` variables in an identity path
are reported and left intact. The converter resolves those variables from the
process environment when they are defined.

## Credential handling

Password, Password V2, passphrase, credential, and secret records are excluded
from the generated document. The converter does not map or print their values.
Identity file paths are included, but key file contents are never read or
copied. Review the generated file and converter warnings before importing.

## Import into SSH Pilot

Open SSH Pilot, choose **Import connections**, then **From Ásbrú…**. Select the
generated `securecrt-import.yml`, review the preview counts and warnings, and
apply the import. The import dialog provides the existing preview and apply
confirmation before connections are added.
