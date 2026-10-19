from __future__ import annotations

import json
from pathlib import Path

import pytest

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

    with pytest.raises(SystemExit) as error:
        main([str(root), "--output", str(output)])
    assert error.value.code == 2

    assert output.read_text(encoding="utf-8") == "keep this file"


def test_only_vds_variables_expand_and_identity_filename_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "Sessions"
    root.mkdir()
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-appear")
    monkeypatch.setenv("VDS_USER_DATA_PATH", "/securecrt/data")
    monkeypatch.setenv("VDS_CONFIG_PATH", "Config")
    _session(
        root,
        "Cloud",
        'S:"Hostname"=cloud.example\nS:"Identity Filename V2"=\n'
        'S:"Identity Filename"=${AWS_SECRET_ACCESS_KEY}/key\n',
    )
    _session(
        root,
        "Local",
        'S:"Hostname"=local.example\n'
        'S:"Identity Filename V2"=${VDS_USER_DATA_PATH}/${VDS_CONFIG_PATH}/key\n',
    )

    data, warnings, _ = convert_sessions(root)
    parsed = parse_asbru_export(data)
    serialized = json.dumps(data)

    cloud = next(item for item in parsed.connections if item.hostname == "cloud.example")
    local = next(item for item in parsed.connections if item.hostname == "local.example")
    assert cloud.identity_files == ("${AWS_SECRET_ACCESS_KEY}/key",)
    assert local.identity_files == ("/securecrt/data/Config/key",)
    assert any("cannot resolve ${AWS_SECRET_ACCESS_KEY}" in warning for warning in warnings)
    assert "must-not-appear" not in serialized


def test_firewall_path_resolution_ignores_none_and_reports_ambiguous_stems(tmp_path: Path):
    root = tmp_path / "Sessions"
    _session(
        root, "Bastion",
        'S:"Hostname"= bastion.example \nS:"Username"= jumpuser \n'
        'D:"[SSH2] Port"=00000898\nS:"Firewall Name"=None\n',
        "Folder/Sub",
    )
    _session(root, "Bastion", 'S:"Hostname"=other.example\n', "Other")
    _session(
        root, "Via Path",
        'S:"Hostname"=target.example\nS:"Firewall Name"=Session:Folder\\Sub\\Bastion\n',
    )
    _session(
        root, "Ambiguous",
        'S:"Hostname"=ambiguous.example\nS:"Firewall Name"=Session:Bastion\n',
    )

    data, warnings, _ = convert_sessions(root)
    parsed = parse_asbru_export(data)
    via_path = next(item for item in parsed.connections if item.hostname == "target.example")

    assert via_path.proxy_jump == ("jumpuser@bastion.example:2200",)
    assert any("firewall session 'Session:Bastion' is ambiguous" in warning for warning in warnings)
    assert not any("firewall session 'None'" in warning for warning in warnings)


def test_protocol_specific_ports_and_record_types(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    _session(
        root, "SSH String Port",
        'S:"Hostname"=string.example\nS:"[SSH2] Port"=22\nS:"Port"=bad\n',
    )
    _session(
        root, "SSH DWORD Port",
        'S:"Hostname"=dword.example\nS:"[SSH2] Port"=22\n'
        'D:"[SSH2] Port"=000008ae\n',
    )
    _session(
        root, "Telnet Default",
        'S:"Protocol Name"=Telnet\nS:"Hostname"=telnet.example\nS:"Port"=bad\n',
    )
    _session(
        root, "Telnet Custom",
        'S:"Protocol Name"=Telnet\nS:"Hostname"=telnet2.example\n'
        'D:"[Telnet] Port"=0000270f\n',
    )
    _session(
        root, "SSH1",
        'S:"Protocol Name"=SSH1\nS:"Hostname"=ssh1.example\nS:"[SSH1] Port"=2222\n',
    )
    _session(root, "RLogin", 'S:"Protocol Name"=RLogin\nS:"Hostname"=rlogin.example\n')
    _session(root, "Serial", 'S:"Protocol Name"=Serial\nS:"Hostname"=serial-device\nS:"Port"=bad\n')

    data, warnings, unsupported = convert_sessions(root)
    parsed = parse_asbru_export(data)
    ssh_ports = {item.hostname: item.port for item in parsed.connections}

    assert ssh_ports == {"string.example": 22, "dword.example": 2222}
    assert "Telnet Default: Telnet telnet.example:23" in unsupported
    assert "Telnet Custom: Telnet telnet2.example:9999" in unsupported
    assert "SSH1: SSH1 ssh1.example:2222" in unsupported
    assert "RLogin: RLogin rlogin.example:513" in unsupported
    assert "Serial: Serial serial-device" in unsupported
    assert warnings == ["SSH DWORD Port: both S:[SSH2] Port and D:[SSH2] Port are present"]


def test_importable_sessions_alone_create_groups_and_symlink_sessions_are_skipped(tmp_path: Path):
    root = tmp_path / "Sessions"
    _session(root, "Telnet", 'S:"Protocol Name"=Telnet\nS:"Hostname"=telnet.example\n', "Empty")
    valid = _session(root, "SSH", 'S:"Hostname"=ssh.example\n', "Kept")
    linked = root / "Linked.ini"
    try:
        linked.symlink_to(valid)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    data, warnings, _ = convert_sessions(root)
    parsed = parse_asbru_export(data)

    assert [group.name for group in parsed.groups] == ["Kept"]
    assert [item.hostname for item in parsed.connections] == ["ssh.example"]
    assert not warnings


def test_output_validation_happens_before_session_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from scripts import securecrt_to_asbru

    root = tmp_path / "Sessions"
    root.mkdir()
    existing = tmp_path / "existing.yml"
    existing.write_text("preserve", encoding="utf-8")
    inside_ini = root / "converted.ini"

    def unexpected_scan(_root: Path):
        raise AssertionError("sessions were scanned before output validation")

    monkeypatch.setattr(securecrt_to_asbru, "convert_sessions", unexpected_scan)
    with pytest.raises(SystemExit) as existing_error:
        main([str(root), "--output", str(existing)])
    assert existing_error.value.code == 2
    with pytest.raises(SystemExit) as inside_error:
        main([str(root), "--output", str(inside_ini)])
    assert inside_error.value.code == 2
    assert existing.read_text(encoding="utf-8") == "preserve"
