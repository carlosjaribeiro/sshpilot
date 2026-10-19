"""Highlighting and session logging together: the log is plain, the screen is coloured."""
import re
import types

import pytest

pytest.importorskip("gi")

from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_highlight import Highlighter

SGR = re.compile(rb"\x1b\[[0-9;]*m")


class _Config:
    def __init__(self, **values):
        self.values = {f"terminal.{k}": v for k, v in values.items()}

    def get_setting(self, key, default=None):
        return self.values.get(key, default)


def _widget(config):
    screen = []
    widget = types.SimpleNamespace(
        config=config,
        connection=types.SimpleNamespace(nickname="web", hostname="10.0.0.5"),
        _session_log=None,
        _daemon_controller=types.SimpleNamespace(delivering_replay=False),
        backend=types.SimpleNamespace(feed=screen.append),
        _highlighter=Highlighter("generic"),
        _mouse_tracking=None,
        _shell_output_seen=True,
        _daemon_running_gate_active=lambda: False,
        _update_daemon_connection_state=lambda: None,
        _show_toast=lambda message, timeout=3: None,
    )
    # Real hooks, so the order of log vs highlighter is the production one.
    widget._feed_display = lambda data: TerminalWidget._paint_display(widget, data)
    widget._start_session_log = lambda: TerminalWidget._start_session_log(widget)
    widget._stop_session_log = lambda: TerminalWidget._stop_session_log(widget)
    return widget, screen


def test_log_is_plain_text_while_backend_gets_highlighted_output(tmp_path):
    config = _Config(
        highlight_enabled=True,
        session_log_enabled=True,
        session_log_folder=str(tmp_path),
        session_log_pattern="%S.log",
    )
    widget, screen = _widget(config)
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"host 10.0.0.1 is up\r\n")
    widget._stop_session_log()

    painted = b"".join(screen)
    assert SGR.search(painted), "backend should receive highlighter colours"
    assert SGR.sub(b"", painted) == b"host 10.0.0.1 is up\r\n"
    assert (tmp_path / "web.log").read_text() == "host 10.0.0.1 is up\n"


def test_terminal_preferences_page_has_both_groups():
    import inspect

    from sshpilot.preferences import PreferencesWindow

    source = inspect.getsource(PreferencesWindow._build_terminal_preferences_page)
    assert "_add_terminal_highlight_group(terminal_page)" in source
    assert "_add_session_log_group(terminal_page)" in source
