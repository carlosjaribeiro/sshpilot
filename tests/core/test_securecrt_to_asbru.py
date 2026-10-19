from __future__ import annotations

import json
from pathlib import Path

from scripts.securecrt_to_asbru import convert_sessions, main
from sshpilot.api.models.connections import AsbruImportRequest
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.import_export.asbru import parse_asbru_export
from tests.helpers.fake_connection_repository import make_test_repository


def _session(root: Path, name: str, text: str, folder: str = "") -> Path:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.ini"
    path.write_text(text, encoding="utf-8")
    return path


def test_converts_nested_ssh_sessions_and_ignores_metadata_files(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    _session(root, "Build Server", 'S:"Hostname"=build.example\nS:"Username"=alice\nD:"[SSH2] Port"=00000016\nS:"Identity Filename V2"=~/.ssh/build\n', "Office/Lab")
    _session(root, "Default", 'S:"Hostname"=ignored.example\n')
    _session(root, "__FolderData__", 'S:"Hostname"=ignored.example\n', "Office")

    data, warnings, unsupported = convert_sessions(root)

    parsed = parse_asbru_export(data)
    assert not warnings
    assert not unsupported
    assert [(group.name, group.parent_source_id) for group in parsed.groups] == [
        ("Office", None), ("Lab", "group:Office")
    ]
    connection = parsed.connections[0]
    assert (connection.display_name, connection.nickname) == ("Build Server", "Build-Server")
    assert (connection.hostname, connection.username, connection.port) == (
        "build.example", "alice", 22,
    )
    assert connection.identity_files[0].endswith("/.ssh/build")


def test_reads_utf8_bom_and_latin1_and_uses_unique_safe_import_aliases(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    (root / "R&D.ini").write_bytes('\ufeffS:"Hostname"=one.example\n'.encode("utf-8"))
    (root / "R D.ini").write_bytes('S:"Hostname"=two.example\nS:"Username"=josé\n'.encode("latin-1"))

    data, warnings, _ = convert_sessions(root)
    parsed = parse_asbru_export(data)

    assert not warnings
    assert [item.nickname for item in parsed.connections] == ["R-D", "R-D-2"]
    assert next(item for item in parsed.connections if item.hostname == "two.example").username == "josé"


def test_skips_non_ssh_and_reports_bad_records_without_credentials(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    _session(root, "Telnet Box", 'S:"Hostname"=telnet.example\nS:"Protocol Name"=Telnet\nD:"Port"=00000017\n')
    _session(root, "Broken", 'S:"Username"=bob\nD:"[SSH2] Port"=not-hex\nS:"Password"=DO_NOT_EXPORT\nS:"Password V2"=ALSO_SECRET\n')
    _session(root, "Bad Firewall", 'S:"Hostname"=host.example\nS:"Firewall Name"=missing-hop\nD:"[SSH2] Port"=oops\n')

    data, warnings, unsupported = convert_sessions(root)
    output = json.dumps(data)
    parsed = parse_asbru_export(data)

    assert "Telnet Box: Telnet telnet.example:23" in unsupported
    assert any("Broken: missing Hostname" in warning for warning in warnings)
    assert any("Bad Firewall: firewall session" in warning for warning in warnings)
    assert any("Bad Firewall: invalid DWORD port [SSH2] Port" in warning for warning in warnings)
    assert "DO_NOT_EXPORT" not in output and "ALSO_SECRET" not in output
    assert all("Password" not in key for key in data)
    assert len(parsed.connections) == 1


def test_generated_json_works_with_asbru_preview_and_apply(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    _session(root, "Import Me", 'S:"Hostname"=import.example\nS:"Username"=carol\nD:"[SSH2] Port"=000008ae\n')
    data, warnings, _ = convert_sessions(root)
    export = tmp_path / "converted.yml"
    export.write_text(json.dumps(data), encoding="utf-8")
    assert not warnings

    service = ConnectionApplicationService(make_test_repository())
    preview = service.preview_asbru_import(str(export))
    assert preview.ok
    assert "Import-Me" in preview.connections_to_add
    applied = service.import_asbru(AsbruImportRequest(source=str(export)))
    assert applied.ok, (applied.errors, applied.partial_failures)
    connection = next(item for item in service.list_connections() if item.nickname == "Import-Me")
    details = service.get_connection(connection.id)
    assert (details.hostname, details.username, details.port) == (
        "import.example", "carol", 2222,
    )
    service.close()


def test_cli_does_not_overwrite_an_existing_output_file(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    output = tmp_path / "existing.yml"
    output.write_text("keep this file", encoding="utf-8")

    try:
        main([str(root), "--output", str(output)])
    except FileExistsError:
        pass
    else:
        raise AssertionError("converter overwrote an existing file")

    assert output.read_text(encoding="utf-8") == "keep this file"
