#!/usr/bin/env python3
"""Convert SecureCRT session files to the JSON subset accepted by SSH Pilot's Asbru importer."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_RECORD_KEY = re.compile(r'^[SD]:"((?:[^"\\]|\\.)*)"=')
_CREDENTIAL = re.compile(r"password|credential|passphrase|secret", re.IGNORECASE)


def _read_ini(path: Path) -> dict[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    records: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        match = _RECORD_KEY.match(line)
        if not match:
            continue
        key = match.group(1)
        if _CREDENTIAL.search(key):
            continue
        records[key.replace(r'\"', '"')] = line[match.end():].strip()
    return records


def _port(records: dict[str, str], key: str, warnings: list[str], session: str) -> int:
    value = records.get(key)
    if value is None:
        return 22
    try:
        port = int(value, 16)
        if not 1 <= port <= 65535:
            raise ValueError
        return port
    except ValueError:
        warnings.append(f"{session}: invalid DWORD port {key}; using 22")
        return 22


def _key_path(value: str, warnings: list[str], session: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if resolved is None:
            warnings.append(f"{session}: cannot resolve ${{{name}}} in identity path")
            return match.group(0)
        return resolved

    value = re.sub(r"\$\{([^}]+)\}", replace, value)
    # SecureCRT commonly nests VDS_CONFIG_PATH under VDS_USER_DATA_PATH.
    return os.path.expanduser(value)


def convert_sessions(root: Path) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return Asbru-compatible data, warnings, and unsupported protocol summaries."""
    warnings: list[str] = []
    unsupported: list[str] = []
    sessions: list[tuple[Path, dict[str, str]]] = []
    ini_files = (path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() == ".ini")
    for file in sorted(ini_files):
        if file.name.casefold() in {"default.ini", "__folderdata__.ini"}:
            continue
        try:
            records = _read_ini(file)
        except OSError as exc:
            warnings.append(f"{file}: cannot read session ({exc})")
            continue
        sessions.append((file, records))

    known_ssh = {
        file.stem.casefold(): records
        for file, records in sessions
        if records.get("Protocol Name", "SSH2").casefold() in {"ssh", "ssh2"}
    }
    data: dict[str, Any] = {}
    group_ids: dict[Path, str] = {}

    for file, records in sessions:
        relative_dir = file.parent.relative_to(root)
        parent: str | None = None
        for index in range(len(relative_dir.parts)):
            folder = Path(*relative_dir.parts[: index + 1])
            group_id = group_ids.setdefault(folder, f"group:{folder.as_posix()}")
            if group_id not in data:
                data[group_id] = {
                    "_is_group": 1,
                    "name": folder.name,
                    "parent": parent,
                }
            parent = group_id

        display = file.stem
        protocol = records.get("Protocol Name", "SSH2").strip()
        host = records.get("Hostname", "").strip()
        telnet_port = _port(records, "Port", warnings, display)
        if protocol.casefold() not in {"ssh", "ssh2"}:
            unsupported.append(f"{display}: {protocol or 'unknown'} {host or '(no host)'}:{telnet_port}")
            continue
        if not host:
            warnings.append(f"{display}: missing Hostname; skipped")
            continue

        entry: dict[str, Any] = {
            "_is_group": 0,
            "name": display,
            "method": "SSH",
            "ip": host,
            "user": records.get("Username", "").strip(),
            "port": _port(records, "[SSH2] Port", warnings, display),
            "parent": parent,
        }
        identity = records.get("Identity Filename V2", "").strip()
        if identity:
            entry["public_key"] = _key_path(identity, warnings, display)
        firewall = records.get("Firewall Name", "").strip()
        if firewall:
            jump = known_ssh.get(Path(firewall).stem.casefold())
            if jump and jump.get("Hostname"):
                entry["jump ip"] = jump["Hostname"]
                if jump.get("Username"):
                    entry["jump user"] = jump["Username"]
            else:
                warnings.append(f"{display}: firewall session {firewall!r} is not a known SSH session")
        data[f"session:{file.relative_to(root).with_suffix('').as_posix()}"] = entry
    return data, warnings, unsupported


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", type=Path, help="SecureCRT Sessions folder")
    parser.add_argument("-o", "--output", type=Path, required=True, help="new JSON/YAML output file")
    args = parser.parse_args(argv)
    if not args.sessions.is_dir():
        parser.error(f"not a sessions directory: {args.sessions}")
    data, warnings, unsupported = convert_sessions(args.sessions)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
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
