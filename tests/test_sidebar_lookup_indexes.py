"""Sidebar rebuild lookups are indexed, not scanned once per row.

A rebuild asks for every connection's groups (to find ungrouped hosts and
each row's group colour) and metadata (tags). Scanning all groups' members or
all metadata entries per connection made a rebuild O(N*G) / O(N*M); these
count the scans so a regression shows up without timing anything.
"""

from types import SimpleNamespace

from sshpilot.api.models.connection_store import (
    ConnectionMetadataSummary,
    ConnectionStoreSnapshot,
)
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.connections import ConnectionSummary
from sshpilot.groups import GroupManager
from sshpilot.gtk.connection_store import ConnectionPresentationStore

GROUPS = 100
CONNECTIONS = 1000


class _CountingList(list):
    scans = 0

    def __contains__(self, item):
        _CountingList.scans += 1
        return super().__contains__(item)

    def __iter__(self):
        _CountingList.scans += 1
        return super().__iter__()


def _manager():
    manager = GroupManager()
    manager.groups = {
        f"g{g}": {
            "id": f"g{g}",
            "parent_id": None,
            "connections": _CountingList(
                f"host-{i}" for i in range(CONNECTIONS) if i % GROUPS == g
            ),
        }
        for g in range(GROUPS)
    }
    return manager


def test_group_lookups_scan_each_group_once_per_projection():
    manager = _manager()
    _CountingList.scans = 0

    for i in range(CONNECTIONS):
        assert manager.get_connection_groups(f"host-{i}") == [f"g{i % GROUPS}"]
        manager.resolve_display_group_id(f"host-{i}", f"g{i % GROUPS}")

    assert _CountingList.scans <= GROUPS


def test_group_lookups_follow_a_new_projection():
    manager = GroupManager()
    manager.groups = {"a": {"id": "a", "connections": ["x"]}}
    assert manager.get_connection_groups("x") == ["a"]

    manager.groups = {
        "b": {"id": "b", "connections": ["x", "x"]},
        "c": {"id": "c", "connections": ["x", "y"]},
    }

    assert manager.get_connection_groups("x") == ["b", "c"]
    assert manager.get_connection_group("y") == "c"
    assert manager.get_connection_groups("missing") == []
    assert manager.get_connection_group(SimpleNamespace(nickname="y")) == "c"


class _CountingTuple(tuple):
    scans = 0

    def __iter__(self):
        _CountingTuple.scans += 1
        return super().__iter__()


def test_metadata_lookups_index_each_snapshot_once():
    metadata = _CountingTuple(
        ConnectionMetadataSummary(
            connection_id=ConnectionId(f"host-{i}"), values={"tags": [f"t{i}"]}
        )
        for i in range(CONNECTIONS)
    )
    store = ConnectionPresentationStore()
    store._install_snapshot(
        SimpleNamespace(metadata=metadata), emit_reset=False
    )
    _CountingTuple.scans = 0

    for i in range(CONNECTIONS):
        assert store.get_metadata(f"host-{i}")["tags"] == (f"t{i}",)
    assert store.get_metadata("missing") == {}

    assert _CountingTuple.scans <= 1


def test_metadata_lookups_follow_a_new_snapshot():
    def snapshot(tag):
        return ConnectionStoreSnapshot(
            generation=1,
            connections=(
                ConnectionSummary(
                    id=ConnectionId("alpha"),
                    nickname="alpha",
                    host="alpha",
                    hostname="alpha.example",
                    username="user",
                    port=22,
                ),
            ),
            groups=(),
            root_connection_ids=(ConnectionId("alpha"),),
            metadata=(
                ConnectionMetadataSummary(
                    connection_id=ConnectionId("alpha"), values={"tags": [tag]}
                ),
            ),
        )

    store = ConnectionPresentationStore()
    store._install_snapshot(snapshot("old"), emit_reset=False)
    assert store.get_metadata("alpha")["tags"] == ("old",)

    store._install_snapshot(snapshot("new"), emit_reset=False)

    assert store.get_metadata("alpha")["tags"] == ("new",)
