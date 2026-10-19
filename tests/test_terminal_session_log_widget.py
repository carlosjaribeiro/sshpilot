"""TerminalWidget wiring for session logging: what is written, when it starts/stops."""
import types

import pytest

pytest.importorskip("gi")

import sshpilot.terminal as terminal_module
from sshpilot.terminal import TerminalWidget


class _Config:
    def __init__(self, **values):
        self.values = {f"terminal.{k}": v for k, v in values.items()}

    def get_setting(self, key, default=None):
        return self.values.get(key, default)


def _widget(config, replay=False):
    widget = types.SimpleNamespace(
        config=config,
        connection=types.SimpleNamespace(nickname="web", hostname="10.0.0.5"),
        _session_log=None,
        _daemon_controller=types.SimpleNamespace(delivering_replay=replay),
        painted=[],
        toasts=[],
        _shell_output_seen=True,
        _daemon_running_gate_active=lambda: False,
        _update_daemon_connection_state=lambda: None,
    )
    widget._feed_display = lambda data: widget.painted.append(data)
    widget._show_toast = lambda message, timeout=3: widget.toasts.append(message)
    widget._stop_session_log = lambda: TerminalWidget._stop_session_log(widget)
    widget._start_session_log = lambda: TerminalWidget._start_session_log(widget)
    return widget


def _enabled(tmp_path):
    return _Config(session_log_enabled=True, session_log_folder=str(tmp_path), session_log_pattern="%S.log")


def test_output_is_logged_as_plain_text_and_still_painted(tmp_path):
    widget = _widget(_enabled(tmp_path))
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"\x1b[32mok\x1b[0m\r\n")
    widget._stop_session_log()
    assert (tmp_path / "web.log").read_text() == "ok\n"
    assert widget.painted == [b"\x1b[32mok\x1b[0m\r\n"]


def test_disabled_logging_creates_nothing(tmp_path):
    config = _enabled(tmp_path)
    config.values["terminal.session_log_enabled"] = False
    widget = _widget(config)
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"hello\n")
    assert widget._session_log is None and list(tmp_path.iterdir()) == []


def test_replayed_output_is_not_logged(tmp_path):
    widget = _widget(_enabled(tmp_path), replay=True)
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"old\n")
    widget._stop_session_log()
    assert (tmp_path / "web.log").read_text() == ""
    assert widget.painted == [b"old\n"]


def test_stop_closes_the_log_and_later_output_is_not_written(tmp_path):
    widget = _widget(_enabled(tmp_path))
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"before\n")
    widget._stop_session_log()
    TerminalWidget._on_daemon_output(widget, b"after\n")
    assert (tmp_path / "web.log").read_text() == "before\n"


def test_unwritable_folder_toasts_once_and_does_not_break_output(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    config = _enabled(tmp_path)
    config.values["terminal.session_log_folder"] = str(blocker)
    widget = _widget(config)
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"one\n")
    TerminalWidget._on_daemon_output(widget, b"two\n")
    assert len(widget.toasts) == 1
    assert widget.painted == [b"one\n", b"two\n"]


def test_session_log_hook_sits_before_painting():
    import inspect

    source = inspect.getsource(terminal_module.TerminalWidget._on_daemon_output)
    assert source.index("session_log.write") < source.index("self._feed_display")
