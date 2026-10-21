"""A watcher reload skips re-reading sources that match the installed state.

The config watcher also fires for the daemon's own writes, and every reload
reparsed the whole ssh_config (twice, with ``discover_paths``). These pin that
an unchanged source set is not reparsed, and that any real change -- to the
root, an included file or the sidecar -- still is.
"""

from __future__ import annotations

import json

import pytest

import sshpilot.core.connections.ssh_config_store as store_module
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore


@pytest.fixture
def parses(monkeypatch):
    calls = []
    original = store_module.load_ssh_configuration

    def counting(*args, **kwargs):
        calls.append(kwargs.get("_content_overrides") is None)
        return original(*args, **kwargs)

    monkeypatch.setattr(store_module, "load_ssh_configuration", counting)
    return calls


def _repo(tmp_path, text):
    root = tmp_path / "ssh_config"
    root.write_text(text)
    state = tmp_path / "connections.json"
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root, isolated=False),
        state_path=state,
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    return repo, root, state


def _nicknames(snapshot):
    return {connection.nickname for connection in snapshot.connections}


def test_reload_after_own_write_does_not_reparse(tmp_path, parses):
    repo, _root, _state = _repo(tmp_path, "Host alpha\n    HostName a.example\n")
    repo.create_connection(
        {"nickname": "beta", "hostname": "b.example", "username": "u", "protocol": "ssh"}
    )
    parses.clear()

    snapshot = repo.reload()

    assert parses == []
    assert _nicknames(snapshot) == {"alpha", "beta"}


def test_discover_paths_uses_the_installed_config(tmp_path, parses):
    repo, root, state = _repo(tmp_path, "Host alpha\n    HostName a.example\n")
    parses.clear()

    paths = repo.discover_paths()

    assert parses == []
    assert state in paths
    assert any(path.name == root.name for path in paths)


def test_external_root_edit_is_reloaded(tmp_path, parses):
    repo, root, _state = _repo(tmp_path, "Host alpha\n    HostName a.example\n")
    parses.clear()
    root.write_text(root.read_text() + "\nHost gamma\n    HostName g.example\n")

    snapshot = repo.reload()

    assert parses == [True]
    assert _nicknames(snapshot) == {"alpha", "gamma"}


def test_external_include_edit_is_reloaded(tmp_path, parses):
    extra = tmp_path / "extra.conf"
    extra.write_text("Host inc1\n    HostName i1.example\n")
    repo, _root, _state = _repo(
        tmp_path, f"Include {extra}\n\nHost alpha\n    HostName a.example\n"
    )
    assert _nicknames(repo.snapshot()) == {"alpha", "inc1"}
    extra.write_text("Host inc2\n    HostName i2.example\n")

    snapshot = repo.reload()

    assert _nicknames(snapshot) == {"alpha", "inc2"}


def test_changed_sidecar_bytes_force_a_reload(tmp_path, parses):
    repo, _root, state = _repo(tmp_path, "Host alpha\n    HostName a.example\n")
    # Same data, different bytes: the skip must not assume equivalence.
    state.write_text(json.dumps(json.loads(state.read_text()), indent=4))
    parses.clear()

    repo.reload()

    assert parses == [True]
