"""Sidebar port-forwarding indicator source: the store's summaries.

``ConnectionSummary.forwards`` carries each connection's forwarding rules, so
the sidebar attaches them as the rule dicts its rows render instead of asking
the daemon for every connection's editor details. With hundreds of
connections that was one daemon request per connection after startup and
after every store change.
"""

import json
from types import SimpleNamespace

from sshpilot.api.models.connections import ConnectionSummary, ForwardingRule
from sshpilot.api.transport.codec import (
    connection_summary_from_wire,
    connection_summary_to_wire,
)

local_rule = ForwardingRule(
    type="local", listen_port=8080, remote_host="localhost", remote_port=80
)
dynamic_rule = ForwardingRule(type="dynamic", listen_port=1080)
LOCAL_RULE_DICT = {
    'type': 'local',
    'listen_addr': '',
    'listen_port': 8080,
    'enabled': True,
    'remote_host': 'localhost',
    'remote_port': 80,
}


def _connection(connection_id, *forwards, protocol="ssh"):
    return ConnectionSummary(
        id=connection_id,
        nickname=connection_id,
        host="host",
        hostname="host.example",
        username="alice",
        port=22,
        protocol=protocol,
        forwards=tuple(forwards),
    )


def test_attach_exposes_summary_forwards_as_rule_dicts():
    from sshpilot.window import MainWindow

    alpha = _connection("alpha", local_rule, dynamic_rule)
    beta = _connection("beta")

    MainWindow._attach_sidebar_forwarding_rules([alpha, beta])

    assert alpha.forwarding_rules[0] == LOCAL_RULE_DICT
    assert {rule["type"] for rule in alpha.forwarding_rules} == {"local", "dynamic"}
    assert beta.forwarding_rules == ()
    # The attached dicts are not fields: the summary stays hashable, so it
    # still keys ``connection_rows``.
    assert hash(alpha) == hash(_connection("alpha", local_rule, dynamic_rule))


def test_attach_leaves_objects_without_summary_forwards_alone():
    from sshpilot.window import MainWindow

    legacy = SimpleNamespace(forwarding_rules=[LOCAL_RULE_DICT])

    MainWindow._attach_sidebar_forwarding_rules([legacy])

    assert legacy.forwarding_rules == [LOCAL_RULE_DICT]


class _RecordingClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"sidebar called the daemon: {name}")

        return record


class _RecordingBridge:
    def __init__(self):
        self.submits = 0

    def submit(self, *_args, **_kwargs):
        self.submits += 1


class _EmptyList:
    def get_first_child(self):
        return None


def test_display_refresh_of_many_connections_makes_no_daemon_requests(monkeypatch):
    from sshpilot import window as window_module
    from sshpilot.window import MainWindow

    idles = []
    monkeypatch.setattr(
        window_module.GLib,
        "idle_add",
        lambda callback, *args, **kwargs: idles.append(callback),
    )
    # Built without ``forwards`` so this also runs against the old fetch path.
    connections = [
        ConnectionSummary(
            id=f"host-{index}",
            nickname=f"host-{index}",
            host="host",
            hostname="host.example",
            username="alice",
            port=22,
        )
        for index in range(800)
    ]
    window = MainWindow.__new__(MainWindow)
    window.config = SimpleNamespace(get_setting=lambda _key, default=None: default)
    window.client = client = _RecordingClient()
    window.client_bridge = bridge = _RecordingBridge()
    window.connection_manager = SimpleNamespace(
        generation=1, get_connections=lambda: connections
    )
    window.connection_rows = {}
    window.connection_list = _EmptyList()

    MainWindow.update_sidebar_display(window)
    for callback in idles:
        callback()

    assert client.calls == []
    assert bridge.submits == 0


def test_summary_forwards_round_trip_through_the_wire():
    summary = _connection("alpha", local_rule, dynamic_rule)

    wire = json.loads(json.dumps(connection_summary_to_wire(summary)))

    assert connection_summary_from_wire(wire) == summary


def test_summary_without_forwards_keeps_its_wire_shape():
    """Additive: older clients validate summary fields strictly."""
    wire = connection_summary_to_wire(_connection("alpha"))

    assert "forwards" not in wire
    assert connection_summary_from_wire(wire).forwards == ()


def test_repository_snapshot_carries_ssh_config_forwards(tmp_path):
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    root = tmp_path / "ssh_config"
    root.write_text(
        "Host alpha\n"
        "    HostName alpha.example\n"
        "    LocalForward 8080 localhost:80\n"
        "    DynamicForward 1080\n"
        "\n"
        "Host beta\n"
        "    HostName beta.example\n"
    )
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root, isolated=False),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )

    by_nickname = {c.nickname: c for c in repo.snapshot().connections}

    assert {rule.type for rule in by_nickname["alpha"].forwards} == {"local", "dynamic"}
    local = next(r for r in by_nickname["alpha"].forwards if r.type == "local")
    assert (local.listen_port, local.remote_host, local.remote_port) == (
        8080,
        "localhost",
        80,
    )
    assert by_nickname["beta"].forwards == ()


def _forwarding_service(tmp_path):
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    root = tmp_path / "ssh_config"
    root.write_text(
        "Host alpha\n"
        "    HostName alpha.example\n"
        "    LocalForward 8080 localhost:80\n"
    )
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root, isolated=False),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    return ConnectionApplicationService(repo, client_name="test")


def test_details_carry_and_round_trip_forwards(tmp_path):
    """``ConnectionDetails`` inherits ``forwards``: produce and decode it."""
    from sshpilot.api.transport.codec import (
        connection_details_from_wire,
        connection_details_to_wire,
    )

    service = _forwarding_service(tmp_path)
    details = service.get_connection("alpha")

    assert [rule.listen_port for rule in details.forwards] == [8080]
    wire = json.loads(json.dumps(connection_details_to_wire(details)))
    assert connection_details_from_wire(wire) == details


def test_editor_details_carry_and_round_trip_forwards(tmp_path):
    from sshpilot.api.transport.codec import (
        connection_editor_details_from_wire,
        connection_editor_details_to_wire,
    )

    service = _forwarding_service(tmp_path)
    editor = service.get_connection_editor("alpha")

    assert editor.forwards == editor.forwarding_rules != ()
    wire = json.loads(json.dumps(connection_editor_details_to_wire(editor)))
    assert connection_editor_details_from_wire(wire) == editor


def test_details_decoders_accept_forwards():
    from sshpilot.api.models.connections import (
        AuthenticationMethod,
        ConnectionDetails,
        ConnectionEditorDetails,
    )
    from sshpilot.api.transport import codec

    fields = dict(
        id="alpha",
        nickname="alpha",
        host="host",
        hostname="host.example",
        username="alice",
        port=22,
        authentication_method=AuthenticationMethod.KEY,
        forwards=(local_rule,),
    )
    for model, encode, decode in (
        (
            ConnectionDetails,
            codec.connection_details_to_wire,
            codec.connection_details_from_wire,
        ),
        (
            ConnectionEditorDetails,
            codec.connection_editor_details_to_wire,
            codec.connection_editor_details_from_wire,
        ),
    ):
        value = model(**fields)
        assert decode(json.loads(json.dumps(encode(value)))) == value


class _IndicatorBox:
    def __init__(self):
        self.children = []
        self.visible = True

    def get_first_child(self):
        return self.children[0] if self.children else None

    def remove(self, child):
        self.children.remove(child)

    def append(self, child):
        self.children.append(child)

    def set_visible(self, visible):
        self.visible = visible


class _Image:
    def __init__(self, icon_name):
        self.icon_name = icon_name
        self.tooltip = None

    def set_pixel_size(self, _size):
        pass

    def set_halign(self, _align):
        pass

    def set_valign(self, _align):
        pass

    def set_tooltip_text(self, text):
        self.tooltip = text


def _stub_ssh_row_dependencies(monkeypatch, icon_utils):
    from sshpilot.plugins import registry
    from sshpilot.plugins.api import Capability

    monkeypatch.setattr(icon_utils, "new_image_from_icon_name", _Image)
    # The protocol registry is only populated once the app loads its plugins.
    monkeypatch.setattr(
        registry,
        "capabilities_for",
        lambda _connection: frozenset({Capability.PORT_FORWARDING}),
    )


def test_row_for_a_forwarded_summary_shows_the_indicator(monkeypatch):
    """End to end on the GTK side: summary ``forwards`` -> row icon + tooltip."""
    from sshpilot import icon_utils
    from sshpilot.sidebar import ConnectionRow
    from sshpilot.window import MainWindow

    _stub_ssh_row_dependencies(monkeypatch, icon_utils)
    alpha = _connection("alpha", local_rule)
    MainWindow._attach_sidebar_forwarding_rules([alpha])
    row = ConnectionRow.__new__(ConnectionRow)
    row.connection = alpha
    row.config = SimpleNamespace(get_setting=lambda _key, default=None: default)
    row.indicator_box = _IndicatorBox()

    ConnectionRow._update_forwarding_indicators(row, refresh_tooltip=False)

    assert len(row.indicator_box.children) == 1
    image = row.indicator_box.children[0]
    assert image.icon_name == "mail-forward-symbolic"
    assert "8080" in image.tooltip and "localhost:80" in image.tooltip


def test_row_for_a_summary_without_forwards_has_no_indicator(monkeypatch):
    from sshpilot import icon_utils
    from sshpilot.sidebar import ConnectionRow
    from sshpilot.window import MainWindow

    _stub_ssh_row_dependencies(monkeypatch, icon_utils)
    beta = _connection("beta")
    MainWindow._attach_sidebar_forwarding_rules([beta])
    row = ConnectionRow.__new__(ConnectionRow)
    row.connection = beta
    row.config = SimpleNamespace(get_setting=lambda _key, default=None: default)
    row.indicator_box = _IndicatorBox()

    ConnectionRow._update_forwarding_indicators(row, refresh_tooltip=False)

    assert row.indicator_box.children == []
