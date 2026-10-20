from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.securecrt_to_asbru import build_backup_manifest, convert_sessions, main
from sshpilot.api.models.connections import AsbruImportRequest
from sshpilot.backup_archive import read_spbk
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
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


def test_only_vds_variables_expand_and_identity_filename_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "Sessions"
    root.mkdir()
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-appear")
    monkeypatch.setenv("VDS_USER_DATA_PATH", "/securecrt/data")
    monkeypatch.setenv("VDS_CONFIG_PATH", "Config")
    monkeypatch.setenv("VDS_SECRET_PATH", "must-not-expand")
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
    _session(root, "Windows Key", 'S:"Hostname"=windows.example\nS:"Identity Filename V2"=C:\\keys\\id\n')
    _session(root, "Other VDS", 'S:"Hostname"=other-vds.example\nS:"Identity Filename V2"=${VDS_SECRET_PATH}/key\n')

    data, warnings, _ = convert_sessions(root)
    parsed = parse_asbru_export(data)
    serialized = json.dumps(data)

    cloud = next(item for item in parsed.connections if item.hostname == "cloud.example")
    local = next(item for item in parsed.connections if item.hostname == "local.example")
    windows = next(item for item in data.values() if item.get("ip") == "windows.example")
    other_vds = next(item for item in data.values() if item.get("ip") == "other-vds.example")
    assert cloud.identity_files == ("${AWS_SECRET_ACCESS_KEY}/key",)
    assert local.identity_files == ("/securecrt/data/Config/key",)
    assert windows["public_key"] == "C:\\keys\\id"
    assert other_vds["public_key"] == "${VDS_SECRET_PATH}/key"
    assert any("cannot resolve ${AWS_SECRET_ACCESS_KEY}" in warning for warning in warnings)
    assert any("cannot resolve ${VDS_SECRET_PATH}" in warning for warning in warnings)
    assert sum("Windows identity path passed through unchanged" in w for w in warnings) == 1
    assert "must-not-appear" not in serialized


def test_firewall_path_resolution_ignores_none_and_reports_ambiguous_stems(tmp_path: Path):
    root = tmp_path / "Sessions"
    _session(
        root, "Bastion",
        'S:"Hostname"=bastion.example\nS:"Username"=jumpuser\n'
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
    _session(root, "Proxy", 'S:"Hostname"=proxy.example\n')
    _session(
        root, "Plain Firewall",
        'S:"Hostname"=plain.example\nS:"Firewall Name"=Proxy\n',
    )
    _session(
        root, "Stale Folder",
        'S:"Hostname"=stale.example\nS:"Firewall Name"=Session:Nowhere/Proxy\n',
    )
    _session(
        root, "Self",
        'S:"Hostname"=self.example\nS:"Firewall Name"=Session:Self\n',
    )

    data, warnings, _ = convert_sessions(root)
    parsed = parse_asbru_export(data)
    via_path = next(item for item in parsed.connections if item.hostname == "target.example")
    plain = next(item for item in parsed.connections if item.hostname == "plain.example")
    stale = next(item for item in parsed.connections if item.hostname == "stale.example")
    self_jump = next(item for item in parsed.connections if item.hostname == "self.example")

    assert via_path.proxy_jump == ("jumpuser@bastion.example:2200",)
    assert any("firewall session 'Session:Bastion' is ambiguous" in warning for warning in warnings)
    assert any("firewall session 'Proxy' does not start with Session:" in warning for warning in warnings)
    assert any("firewall session 'Session:Nowhere/Proxy' is unknown" in warning for warning in warnings)
    assert any("firewall session refers to itself" in warning for warning in warnings)
    assert not plain.proxy_jump and not stale.proxy_jump and not self_jump.proxy_jump
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
    assert not warnings


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
    assert warnings == [f"{linked}: symlinked session skipped"]


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

    monkeypatch.setattr(securecrt_to_asbru, "convert_sessions", lambda _root: ({}, [], []))
    with pytest.raises(SystemExit) as missing_parent_error:
        main([str(root), "--output", str(tmp_path / "missing" / "out.yml")])
    assert missing_parent_error.value.code == 2


def test_rejects_option_like_and_whitespace_host_and_user_values(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    _session(root, "Bad User", 'S:"Hostname"=valid.example\nS:"Username"=-oProxyCommand=x\n')
    _session(root, "Bad Host", 'S:"Hostname"=-oProxyCommand=x\nS:"Username"=valid\n')
    _session(root, "Host Space", 'S:"Hostname"=evil.example other\nS:"Username"=valid\n')
    _session(root, "User Control", 'S:"Hostname"=valid.example\nS:"Username"=bad\tuser\n')
    _session(root, "Good", 'S:"Hostname"=good.example\nS:"Username"=alice\n')

    data, warnings, _ = convert_sessions(root)
    parsed = parse_asbru_export(data)
    output = json.dumps(data)

    assert [(item.hostname, item.username) for item in parsed.connections] == [("good.example", "alice")]
    assert sum("invalid Username; skipped" in warning for warning in warnings) == 2
    assert sum("invalid Hostname; skipped" in warning for warning in warnings) == 2
    assert "ProxyCommand" not in output and "evil.example other" not in output and "bad\\tuser" not in output


def test_indented_multiline_ini_content_is_not_parsed_as_a_record(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    _session(
        root, "Multiline",
        'S:"Hostname"=real.example\nZ:"Notes"=first\n  S:"Hostname"=evil.example\n',
    )

    data, _, _ = convert_sessions(root)

    assert data["session:Multiline"]["ip"] == "real.example"


def test_decodes_utf16_bom_sessions(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    (root / "Unicode.ini").write_bytes('S:"Hostname"=utf16.example\n'.encode("utf-16"))

    data, warnings, _ = convert_sessions(root)

    assert data["session:Unicode"]["ip"] == "utf16.example"
    assert not warnings


def test_jump_port_warning_is_emitted_once_under_the_jump_name(tmp_path: Path):
    root = tmp_path / "Sessions"
    _session(root, "Jump", 'S:"Hostname"=jump.example\nD:"[SSH2] Port"=invalid\n')
    _session(
        root, "Through Jump",
        'S:"Hostname"=target.example\nS:"Firewall Name"=Session:Jump\n',
    )

    _, warnings, _ = convert_sessions(root)

    assert warnings == ["Jump: invalid DWORD port [SSH2] Port; using 22"]


def test_backup_manifest_maps_telnet_serial_and_nested_groups(tmp_path: Path):
    root = tmp_path / "Sessions"
    _session(
        root, "Console Port",
        'S:"Protocol Name"=Serial\nS:"Com Port"=COM3\nD:"Baud Rate"=0001c200\n'
        'D:"Flow Control"=00000001\nD:"Data Bits"=00000007\n'
        'D:"Parity"=00000002\nD:"Stop Bits"=00000002\n',
        "Lab/Racks",
    )
    _session(
        root, "Legacy Switch",
        'S:"Protocol Name"=Telnet\nS:"Hostname"=192.0.2.10\n'
        'S:"[Telnet] Port"=2323\n',
        "Lab",
    )

    manifest, warnings = build_backup_manifest(root)

    assert not warnings
    store = manifest["connection_store"]
    assert manifest["backup_options"] == {
        "app_settings": True,
        "ssh_config": False,
        "known_hosts": False,
        "private_keys": False,
        "secrets": False,
    }
    telnet = next(c for c in store["connections"] if c["protocol"] == "telnet")
    serial = next(c for c in store["connections"] if c["protocol"] == "serial")
    assert telnet["plugin_data"] == {"host": "192.0.2.10", "port": 2323}
    assert serial["port"] == 22
    assert serial["plugin_data"] == {
        "device": "COM3", "baud": "115200", "flow": "hard",
        "databits": "7", "parity": "even", "stopbits": "2",
    }
    assert [(g["name"], g["parent_id"], g["connection_ids"]) for g in store["groups"]] == [
        ("Lab", None, ["Legacy-Switch"]),
        ("Racks", "group:Lab", ["Console-Port"]),
    ]


def test_unknown_serial_values_warn_and_use_defaults(tmp_path: Path):
    root = tmp_path / "Sessions"
    _session(
        root, "Fallback Serial",
        'S:"Protocol Name"=Serial\nS:"Com Port"=COM3\nD:"Baud Rate"=oops\n'
        'D:"Flow Control"=000000ff\nS:"Data Bits"=nine\n'
        'D:"Parity"=00000004\nD:"Stop Bits"=00000001\n',
    )

    manifest, warnings = build_backup_manifest(root)

    data = manifest["connection_store"]["connections"][0]["plugin_data"]
    assert data == {
        "device": "COM3", "baud": "38400", "flow": "hard",
        "databits": "8", "parity": "none", "stopbits": "1",
    }
    assert len(warnings) == 5


def test_generated_json_restores_telnet_and_serial_through_repository(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    _session(
        root, "Telnet Node",
        'S:"Protocol Name"=Telnet\nS:"Hostname"=192.0.2.20\n'
        'D:"[Telnet] Port"=00000017\n',
        "Imported",
    )
    _session(
        root, "Serial Node",
        'S:"Protocol Name"=Serial\nS:"Com Port"=COM3\nS:"Baud Rate"=9600\n'
        'S:"Flow Control"=None\nS:"Data Bits"=8\nS:"Parity"=None\nS:"Stop Bits"=1\n',
        "Imported",
    )
    _session(root, "SSH Node", 'S:"Hostname"=192.0.2.40\n', "Imported")
    expected_asbru, _, _ = convert_sessions(root)
    asbru = tmp_path / "ssh.json"
    backup = tmp_path / "non-ssh.json"
    assert main([str(root), "--output", str(asbru), "--backup-output", str(backup)]) == 0

    assert json.loads(asbru.read_text(encoding="utf-8")) == expected_asbru
    manifest = json.loads(backup.read_text(encoding="utf-8"))
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(tmp_path / "ssh_config"),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    result = repo.restore_connection_store(manifest["connection_store"], mode="merge")

    assert not result.warnings
    assert {c.protocol for c in repo.snapshot().connections} == {"telnet", "serial"}
    assert repo.get_record("Telnet-Node").data["hostname"] == "192.0.2.20"
    assert repo.get_record("Serial-Node").data["device"] == "COM3"
    group = repo.snapshot().groups[0]
    assert group.name == "Imported"
    assert set(group.connection_ids) == {"Telnet-Node", "Serial-Node"}


def test_backup_output_spbk_is_valid_unencrypted_archive(tmp_path: Path):
    root = tmp_path / "Sessions"
    root.mkdir()
    _session(
        root, "Telnet Node",
        'S:"Protocol Name"=Telnet\nS:"Hostname"=192.0.2.30\n',
    )
    asbru = tmp_path / "ssh.json"
    backup = tmp_path / "non-ssh.spbk"

    assert main([str(root), "--output", str(asbru), "--backup-output", str(backup)]) == 0

    manifest = read_spbk(str(backup))
    assert manifest["connection_store"]["connections"][0]["protocol"] == "telnet"
