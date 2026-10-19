#!/usr/bin/env python3
"""Convert SecureCRT session files to the JSON subset accepted by SSH Pilot's Asbru importer."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

_RECORD_KEY = re.compile(r'^[SD]:"((?:[^"\\]|\\.)*)"=')
_CREDENTIAL = re.compile(r"password|credential|passphrase|secret", re.IGNORECASE)


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


def convert_sessions(root: Path) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return Asbru-compatible data, warnings, and unsupported protocol summaries."""
    warnings: list[str] = []
    unsupported: list[str] = []
    sessions: list[tuple[Path, dict[tuple[str, str], str]]] = []
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
    args = parser.parse_args(argv)
    if not args.sessions.is_dir():
        parser.error(f"not a sessions directory: {args.sessions}")
    if args.output.exists():
        parser.error(f"output file already exists: {args.output}")
    sessions_root = args.sessions.resolve()
    output_path = args.output.resolve()
    if output_path.suffix.casefold() == ".ini" and output_path.is_relative_to(sessions_root):
        parser.error("output cannot be an .ini file inside the SecureCRT Sessions directory")

    data, warnings, unsupported = convert_sessions(args.sessions)
    try:
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        parser.error(f"cannot write output file {args.output}: {exc}")
    print(f"Wrote {len(data)} Asbru entries to {args.output}")
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    if unsupported:
        print("Unsupported protocols (add manually):", file=sys.stderr)
        for item in unsupported:
            print(f"  {item}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
