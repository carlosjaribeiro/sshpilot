#!/usr/bin/env python3
"""Convert SecureCRT session files to the JSON subset accepted by SSH Pilot's Asbru importer."""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

_RECORD_KEY = re.compile(r'^[SD]:"((?:[^"\\]|\\.)*)"=')
_CREDENTIAL = re.compile(r"password|credential|passphrase|secret", re.IGNORECASE)
_NON_ALIAS = re.compile(r"[^\w.-]+", re.UNICODE)
_MULTI_DASH = re.compile(r"-{2,}")
_COPY_SUFFIX = re.compile(r"\s*- copy$", re.IGNORECASE)


def _read_ini(path: Path) -> dict[tuple[str, str], str]:
    raw = path.read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    records: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        match = _RECORD_KEY.match(line)
        if not match:
            continue
        key = match.group(1).replace(r'\"', '"')
        if _CREDENTIAL.search(key):
            continue
        # Keep SecureCRT's record type so string ports remain decimal and DWORDs hexadecimal.
        records[(line[0], key)] = line[match.end():]
    return records


def _raw_value(records: dict[tuple[str, str], str], key: str, default: str = "") -> str:
    return records.get(("S", key), records.get(("D", key), default))


def _value(records: dict[tuple[str, str], str], key: str, default: str = "") -> str:
    return _raw_value(records, key, default).strip()


def _bad_target(value: str) -> bool:
    return value.startswith("-") or any(
        char.isspace() or unicodedata.category(char) == "Cc" for char in value
    )


def _port(
    records: dict[tuple[str, str], str], key: str, default: int,
    warnings: list[str], session: str,
) -> int:
    dword = records.get(("D", key))
    string = records.get(("S", key))
    value = dword if dword is not None else string
    if value is None:
        return default
    value = value.strip()
    try:
        if dword is not None:
            if not re.fullmatch(r"[0-9a-fA-F]{1,8}", value):
                raise ValueError
            port = int(value, 16)
        else:
            if not re.fullmatch(r"[0-9]+", value):
                raise ValueError
            port = int(value, 10)
        if not 1 <= port <= 65535:
            raise ValueError
        return port
    except ValueError:
        kind = "DWORD" if dword is not None else "string"
        warnings.append(f"{session}: invalid {kind} port {key}; using {default}")
        return default


def _protocol_port(
    protocol: str, records: dict[tuple[str, str], str],
    warnings: list[str], session: str,
) -> int | None:
    name = protocol.casefold()
    if name in {"ssh", "ssh2"}:
        return _port(records, "[SSH2] Port", 22, warnings, session)
    if name == "ssh1":
        return _port(records, "[SSH1] Port", 22, warnings, session)
    if name == "telnet":
        return _port(records, "[Telnet] Port", 23, warnings, session)
    if name == "rlogin":
        return 513
    return None


def _key_path(value: str, warnings: list[str], session: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in {"VDS_USER_DATA_PATH", "VDS_CONFIG_PATH"} and name in os.environ:
            return os.environ[name]
        warnings.append(f"{session}: cannot resolve ${{{name}}} in identity path")
        return match.group(0)

    value = re.sub(r"\$\{([^}]+)\}", replace, value)
    value = os.path.expanduser(value)
    if re.match(r"^[A-Za-z]:[\\/]", value):
        warnings.append(f"{session}: Windows identity path passed through unchanged")
    return value


def _session_key(value: str) -> str:
    normalized = re.sub(r"^Session:", "", value.strip(), flags=re.IGNORECASE)
    normalized = normalized.replace("\\", "/").strip("/")
    if normalized.casefold().endswith(".ini"):
        normalized = normalized[:-4]
    return Path(normalized).as_posix().casefold()


def _resolve_firewall(
    value: str,
    known_ssh: dict[str, list[tuple[str, dict[tuple[str, str], str], int]]],
) -> tuple[tuple[str, dict[tuple[str, str], str], int] | None, str]:
    if not value.casefold().startswith("session:"):
        return None, "does not start with Session:"
    path_key = _session_key(value)
    by_path = known_ssh.get(f"path:{path_key}", [])
    if len(by_path) == 1:
        return by_path[0], ""
    if len(by_path) > 1:
        return None, "is ambiguous"
    target = value[len("Session:"):].replace("\\", "/").strip("/")
    if "/" in target:
        return None, "is unknown"
    matches = known_ssh.get(f"stem:{path_key.rsplit('/', 1)[-1]}", [])
    if len(matches) == 1:
        return matches[0], ""
    return None, "is ambiguous" if matches else "is unknown"


def _add_groups(root: Path, file: Path, data: dict[str, Any]) -> str | None:
    relative_dir = file.parent.relative_to(root)
    parent: str | None = None
    for index in range(len(relative_dir.parts)):
        folder = Path(*relative_dir.parts[: index + 1])
        group_id = f"group:{folder.as_posix()}"
        if group_id not in data:
            data[group_id] = {"_is_group": 1, "name": folder.name, "parent": parent}
        parent = group_id
    return parent


def _collect_sessions(
    root: Path, warnings: list[str],
) -> list[tuple[Path, dict[tuple[str, str], str]]]:
    sessions = []
    for file in sorted(root.rglob("*")):
        if file.suffix.casefold() != ".ini":
            continue
        if file.is_symlink():
            warnings.append(f"{file}: symlinked session skipped")
            continue
        if not file.is_file() or file.name.casefold() in {"default.ini", "__folderdata__.ini"}:
            continue
        try:
            sessions.append((file, _read_ini(file)))
        except OSError as exc:
            warnings.append(f"{file}: cannot read session ({exc})")
    return sessions


def _alias(name: str, used: set[str]) -> str:
    name = _COPY_SUFFIX.sub("", name).strip()
    alias = _MULTI_DASH.sub("-", _NON_ALIAS.sub("-", name.replace(" ", "-")))
    alias = alias.strip("-._") or "host"
    candidate = alias
    index = 2
    while candidate.casefold() in used:
        candidate = f"{alias}-{index}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _encoded_int(records: dict[tuple[str, str], str], key: str) -> int | None:
    dword = records.get(("D", key))
    string = records.get(("S", key))
    raw = dword if dword is not None else string
    if raw is None:
        return None
    raw = raw.strip()
    pattern = r"[0-9a-fA-F]+" if dword is not None else r"[0-9]+"
    if not raw or not re.fullmatch(pattern, raw):
        raise ValueError
    return int(raw, 16 if dword is not None else 10)


def _serial_choice(
    records: dict[tuple[str, str], str], key: str, default: str,
    numeric: dict[int, str], textual: dict[str, str], warnings: list[str], session: str,
) -> str:
    raw = _value(records, key)
    if not raw:
        return default
    try:
        value = numeric[_encoded_int(records, key)]
    except (KeyError, TypeError, ValueError):
        value = textual.get(raw.casefold())
    if value is None:
        warnings.append(f"{session}: unknown Serial {key} {raw!r}; using {default}")
        return default
    return value


def _serial_data(
    records: dict[tuple[str, str], str], warnings: list[str], session: str,
) -> dict[str, str]:
    device = _value(records, "Com Port")
    if not device:
        device = "COM1"
        warnings.append(f"{session}: missing Serial Com Port; using COM1")
    try:
        baud_number = _encoded_int(records, "Baud Rate")
        if baud_number is not None and baud_number <= 0:
            raise ValueError
        baud = str(baud_number or 38400)
    except ValueError:
        baud = "38400"
        warnings.append(f"{session}: invalid Serial Baud Rate; using 38400")
    return {
        "device": device,
        "baud": baud,
        "flow": _serial_choice(
            records, "Flow Control", "hard", {0: "none", 1: "hard", 2: "soft"},
            {"none": "none", "rts/cts": "hard", "hardware": "hard",
             "xon/xoff": "soft", "software": "soft"}, warnings, session,
        ),
        "databits": _serial_choice(
            records, "Data Bits", "8", {i: str(i) for i in range(5, 9)},
            {str(i): str(i) for i in range(5, 9)}, warnings, session,
        ),
        "parity": _serial_choice(
            records, "Parity", "none", {0: "none", 1: "odd", 2: "even"},
            {"none": "none", "odd": "odd", "even": "even"}, warnings, session,
        ),
        # SecureCRT stores the Windows STOPBITS enum: 0=1, 1=1.5, 2=2.
        "stopbits": _serial_choice(
            records, "Stop Bits", "1", {0: "1", 2: "2"},
            {"1": "1", "2": "2"}, warnings, session,
        ),
    }


def build_backup_manifest(root: Path) -> tuple[dict[str, Any], list[str]]:
    """Build a Backup/Restore v1 manifest containing Telnet and Serial sessions."""
    warnings: list[str] = []
    sessions = _collect_sessions(root, warnings)

    used: set[str] = set()
    for file, records in sessions:
        if _value(records, "Protocol Name", "SSH2").casefold() in {"ssh", "ssh2"}:
            _alias(file.stem, used)

    connections: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    roots: list[str] = []
    for file, records in sessions:
        protocol = _value(records, "Protocol Name", "SSH2").casefold()
        if protocol not in {"telnet", "serial"}:
            continue
        if protocol == "telnet":
            hostname = _value(records, "Hostname")
            if not hostname or _bad_target(hostname):
                warnings.append(f"{file.stem}: invalid or missing Telnet Hostname; skipped")
                continue
            port = _port(records, "[Telnet] Port", 23, warnings, file.stem)
            plugin_data = {"host": hostname, "port": port}
        else:
            hostname = ""
            port = 22
            plugin_data = _serial_data(records, warnings, file.stem)
        nickname = _alias(file.stem, used)

        connections.append({
            "id": nickname,
            "protocol": protocol,
            "nickname": nickname,
            "hostname": hostname,
            "username": "",
            "port": port,
            "display_name": "" if nickname == file.stem else file.stem,
            "plugin_data": plugin_data,
        })

        parent_id: str | None = None
        relative_dir = file.parent.relative_to(root)
        for index in range(len(relative_dir.parts)):
            folder = Path(*relative_dir.parts[: index + 1])
            group_id = f"group:{folder.as_posix()}"
            groups.setdefault(group_id, {
                "id": group_id,
                "name": folder.name,
                "parent_id": parent_id,
                "order": len(groups),
                "color": "",
                "connection_ids": [],
            })
            parent_id = group_id
        if parent_id is None:
            roots.append(nickname)
        else:
            groups[parent_id]["connection_ids"].append(nickname)

    return {
        "version": 1,
        "app_config": {},
        "backup_options": {
            "app_settings": True,
            "ssh_config": False,
            "known_hosts": False,
            "private_keys": False,
            "secrets": False,
        },
        "ssh_config": "",
        "known_hosts": None,
        "connection_store": {
            "version": 1,
            "connections": connections,
            "groups": list(groups.values()),
            "root_connection_ids": roots,
            "metadata": [],
        },
    }, warnings


def _write_backup(path: Path, manifest: dict[str, Any]) -> None:
    if path.suffix.casefold() != ".spbk":
        with path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
        return
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest).encode("utf-8"))
    header = json.dumps({"v": 1, "enc": None}).encode("utf-8")
    with path.open("xb") as output:
        output.write(b"SPBK1\n" + header + b"\n" + payload.getvalue())
    os.chmod(path, 0o600)


def convert_sessions(root: Path) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return Asbru-compatible data, warnings, and unsupported protocol summaries."""
    warnings: list[str] = []
    unsupported: list[str] = []
    sessions = _collect_sessions(root, warnings)

    known_ssh: dict[str, list[tuple[str, dict[tuple[str, str], str], int]]] = {}
    prepared: dict[str, tuple[str, str, int] | None] = {}
    for file, records in sessions:
        relative = file.relative_to(root).as_posix()
        path_key = _session_key(relative)
        protocol = _value(records, "Protocol Name", "SSH2")
        if protocol.casefold() not in {"ssh", "ssh2"}:
            continue
        hostname = _raw_value(records, "Hostname")
        username = _raw_value(records, "Username")
        if not hostname.strip():
            warnings.append(f"{file.stem}: missing Hostname; skipped")
            prepared[path_key] = None
            continue
        if _bad_target(hostname):
            warnings.append(f"{file.stem}: invalid Hostname; skipped")
            prepared[path_key] = None
            continue
        if username and _bad_target(username):
            warnings.append(f"{file.stem}: invalid Username; skipped")
            prepared[path_key] = None
            continue
        port = _protocol_port(protocol, records, warnings, file.stem)
        prepared[path_key] = (hostname.strip(), username.strip(), port)
        session = (path_key, records, port)
        known_ssh.setdefault(f"path:{path_key}", []).append(session)
        known_ssh.setdefault(f"stem:{file.stem.casefold()}", []).append(session)

    data: dict[str, Any] = {}
    for file, records in sessions:
        display = file.stem
        protocol = _value(records, "Protocol Name", "SSH2")
        host = _value(records, "Hostname")
        relative_key = _session_key(file.relative_to(root).as_posix())
        if protocol.casefold() not in {"ssh", "ssh2"}:
            port = _protocol_port(protocol, records, warnings, display)
            destination = host or "(no host)"
            if port is not None:
                destination += f":{port}"
            unsupported.append(f"{display}: {protocol or 'unknown'} {destination}")
            continue
        connection = prepared.get(relative_key)
        if connection is None:
            continue
        host, username, port = connection
        parent = _add_groups(root, file, data)
        entry: dict[str, Any] = {
            "_is_group": 0,
            "name": display,
            "method": "SSH",
            "ip": host,
            "user": username,
            "port": port,
            "parent": parent,
        }
        identity = _value(records, "Identity Filename V2") or _value(records, "Identity Filename")
        if identity:
            entry["public_key"] = _key_path(identity, warnings, display)

        firewall = _value(records, "Firewall Name")
        if firewall and firewall.casefold() != "none":
            jump, reason = _resolve_firewall(firewall, known_ssh)
            if jump is None:
                warnings.append(f"{display}: firewall session {firewall!r} {reason}")
            elif jump[0] == relative_key:
                warnings.append(f"{display}: firewall session refers to itself; ignored")
            else:
                _, jump_records, jump_port = jump
                entry["jump ip"] = _value(jump_records, "Hostname")
                jump_user = _value(jump_records, "Username")
                if jump_user:
                    entry["jump user"] = jump_user
                if jump_port != 22:
                    entry["jump port"] = jump_port

        data[f"session:{file.relative_to(root).with_suffix('').as_posix()}"] = entry
    return data, warnings, unsupported


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", type=Path, help="SecureCRT Sessions folder")
    parser.add_argument("-o", "--output", type=Path, required=True, help="new JSON/YAML output file")
    parser.add_argument(
        "--backup-output", type=Path,
        help="optional new .json manifest or unencrypted .spbk for Telnet/Serial sessions",
    )
    args = parser.parse_args(argv)
    if not args.sessions.is_dir():
        parser.error(f"not a sessions directory: {args.sessions}")
    if args.output.exists():
        parser.error(f"output file already exists: {args.output}")
    if args.backup_output and args.backup_output.exists():
        parser.error(f"backup output file already exists: {args.backup_output}")
    if args.backup_output and args.output.resolve() == args.backup_output.resolve():
        parser.error("output and backup output must be different files")
    sessions_root = args.sessions.resolve()
    output_path = args.output.resolve()
    if output_path.suffix.casefold() == ".ini" and output_path.is_relative_to(sessions_root):
        parser.error("output cannot be an .ini file inside the SecureCRT Sessions directory")
    if args.backup_output:
        backup_path = args.backup_output.resolve()
        if backup_path.suffix.casefold() == ".ini" and backup_path.is_relative_to(sessions_root):
            parser.error("backup output cannot be an .ini file inside the Sessions directory")

    data, warnings, unsupported = convert_sessions(args.sessions)
    backup_manifest = None
    if args.backup_output:
        backup_manifest, backup_warnings = build_backup_manifest(args.sessions)
        warnings = list(dict.fromkeys([*warnings, *backup_warnings]))
    try:
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        if args.backup_output and backup_manifest is not None:
            _write_backup(args.backup_output, backup_manifest)
    except OSError as exc:
        parser.error(f"cannot write output file: {exc}")
    print(f"Wrote {len(data)} Asbru entries to {args.output}")
    if args.backup_output and backup_manifest is not None:
        count = len(backup_manifest["connection_store"]["connections"])
        print(f"Wrote {count} Telnet/Serial entries to {args.backup_output}")
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    if unsupported:
        heading = "Protocols omitted from the Asbru output:"
        if not args.backup_output:
            heading = "Unsupported protocols (add manually):"
        print(heading, file=sys.stderr)
        for item in unsupported:
            print(f"  {item}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
