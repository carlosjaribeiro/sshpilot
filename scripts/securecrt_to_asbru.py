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
_VARIABLE = re.compile(r"\$\{([^}]+)\}")


def _read_ini(path: Path) -> tuple[dict[tuple[str, str], str], list[str]]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    records: dict[tuple[str, str], str] = {}
    warnings: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        match = _RECORD_KEY.match(line)
        if not match:
            continue
        key = match.group(1).replace(r'\"', '"')
        if _CREDENTIAL.search(key):
            continue
        record_type = line[0]
        value = line[match.end():].strip()
        record_key = (record_type, key)
        previous = records.get(record_key)
        if previous is not None and previous != value:
            warnings.append(f"{path.stem}: duplicate {record_type}:{key}; using last value")
        other_type = "D" if record_type == "S" else "S"
        other_value = records.get((other_type, key))
        if other_value is not None and other_value != value:
            warnings.append(f"{path.stem}: both S:{key} and D:{key} are present")
        records[record_key] = value
    return records, warnings


def _value(records: dict[tuple[str, str], str], key: str, default: str = "") -> str:
    return records.get(("S", key), records.get(("D", key), default)).strip()


def _port(
    records: dict[tuple[str, str], str], key: str, default: int,
    warnings: list[str], session: str,
) -> int:
    dword = records.get(("D", key))
    string = records.get(("S", key))
    value = dword if dword is not None else string
    if value is None:
        return default
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


def _key_path(value: str, warnings: list[str], session: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name) if name.startswith("VDS_") else None
        if resolved is None:
            warnings.append(f"{session}: cannot resolve ${{{name}}} in identity path")
            return match.group(0)
        return resolved

    value = _VARIABLE.sub(replace, value)
    return os.path.expanduser(value)


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


def _session_summary(name: str, protocol: str, host: str, port: int | None) -> str:
    target = host or "(no host)"
    if port is not None:
        target += f":{port}"
    return f"{name}: {protocol or 'unknown'} {target}"


def _firewall_target(value: str) -> str:
    target = re.sub(r"^Session:", "", value.strip(), flags=re.IGNORECASE)
    return target.replace("\\", "/").strip("/")


def _session_path_key(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    if normalized.casefold().endswith(".ini"):
        normalized = normalized[:-4]
    return Path(normalized).as_posix().casefold()


def _resolve_firewall(
    value: str,
    ssh_paths: dict[str, dict[tuple[str, str], str]],
    ssh_stems: dict[str, list[dict[tuple[str, str], str]]],
) -> tuple[dict[tuple[str, str], str] | None, str]:
    target = _firewall_target(value)
    path_key = _session_path_key(target)
    if path_key in ssh_paths:
        return ssh_paths[path_key], ""
    basename = path_key.rsplit("/", 1)[-1]
    matches = ssh_stems.get(basename, [])
    if len(matches) == 1:
        return matches[0], ""
    return None, "ambiguous" if len(matches) > 1 else "unknown"


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
    ini_files = (
        path for path in root.rglob("*")
        if not path.is_symlink() and path.is_file() and path.suffix.casefold() == ".ini"
    )
    for file in sorted(ini_files):
        if file.name.casefold() in {"default.ini", "__folderdata__.ini"}:
            continue
        try:
            records, record_warnings = _read_ini(file)
        except OSError as exc:
            warnings.append(f"{file}: cannot read session ({exc})")
            continue
        warnings.extend(record_warnings)
        sessions.append((file, records))

    ssh_paths: dict[str, dict[tuple[str, str], str]] = {}
    ssh_stems: dict[str, list[dict[tuple[str, str], str]]] = {}
    for file, records in sessions:
        if _value(records, "Protocol Name", "SSH2").casefold() not in {"ssh", "ssh2"}:
            continue
        if not _value(records, "Hostname"):
            continue
        relative = _session_path_key(file.relative_to(root).as_posix())
        ssh_paths[relative] = records
        ssh_stems.setdefault(file.stem.casefold(), []).append(records)

    data: dict[str, Any] = {}
    for file, records in sessions:
        display = file.stem
        protocol = _value(records, "Protocol Name", "SSH2")
        host = _value(records, "Hostname")
        if protocol.casefold() not in {"ssh", "ssh2"}:
            port = _protocol_port(protocol, records, warnings, display)
            unsupported.append(_session_summary(display, protocol, host, port))
            continue
        if not host:
            warnings.append(f"{display}: missing Hostname; skipped")
            continue

        parent = _add_groups(root, file, data)
        entry: dict[str, Any] = {
            "_is_group": 0,
            "name": display,
            "method": "SSH",
            "ip": host,
            "user": _value(records, "Username"),
            "port": _protocol_port(protocol, records, warnings, display),
            "parent": parent,
        }
        identity = _value(records, "Identity Filename V2") or _value(records, "Identity Filename")
        if identity:
            entry["public_key"] = _key_path(identity, warnings, display)

        firewall = _value(records, "Firewall Name")
        if firewall and firewall.casefold() != "none":
            jump, reason = _resolve_firewall(firewall, ssh_paths, ssh_stems)
            if jump is None:
                warnings.append(f"{display}: firewall session {firewall!r} is {reason}")
            else:
                entry["jump ip"] = _value(jump, "Hostname")
                jump_user = _value(jump, "Username")
                if jump_user:
                    entry["jump user"] = jump_user
                jump_port = _protocol_port("SSH2", jump, warnings, display)
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
    except FileExistsError:
        parser.error(f"output file already exists: {args.output}")
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
