"""TerminalWidget wiring for session logging: what is written, when it starts/stops."""
import types

import pytest

pytest.importorskip("gi")

import sshpilot.terminal as terminal_module
from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_highlight import Highlighter


@pytest.fixture(autouse=True)
def _fresh_run_registry():
    from sshpilot import terminal_session_log as tsl

    getattr(tsl, "_used_paths", {}).clear()


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


class _FakeDatetime:
    current = None

    @classmethod
    def now(cls):
        return cls.current


def _bind_teardown(widget):
    widget._uninstall_selection_feed_pause = lambda: None
    widget.backend = None
    widget._mouse_tracking = None
    widget._highlighter = Highlighter(enabled=False)
    widget._daemon_mode = False
    widget._daemon_controller = None
    widget.is_connected = False
    widget.get_root = lambda: None
    widget.session_id = "t1"
    widget.emit = lambda *args: None
    widget._is_quitting = True
    return widget


def test_reconnect_opens_a_new_session_file_with_the_new_start_time(tmp_path, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr("sshpilot.terminal_session_log.datetime", _FakeDatetime)
    config = _enabled(tmp_path)
    config.values["terminal.session_log_pattern"] = "%S %h-%m-%s.log"
    widget = _widget(config)
    _FakeDatetime.current = datetime(2026, 1, 1, 9, 0, 0)
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"first\n")
    _FakeDatetime.current = datetime(2026, 1, 1, 9, 30, 15)
    widget._start_session_log()  # what start_daemon_session does on reconnect
    TerminalWidget._on_daemon_output(widget, b"second\n")
    widget._stop_session_log()
    assert (tmp_path / "web 09-00-00.log").read_text() == "first\n"
    assert (tmp_path / "web 09-30-15.log").read_text() == "second\n"


def test_reattach_replay_then_live_output_is_logged_once(tmp_path):
    widget = _widget(_enabled(tmp_path))
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"live1\n")
    widget._daemon_controller.delivering_replay = True  # reattach replays history
    TerminalWidget._on_daemon_output(widget, b"live1\n")
    widget._daemon_controller.delivering_replay = False
    TerminalWidget._on_daemon_output(widget, b"live2\n")
    widget._stop_session_log()
    assert (tmp_path / "web.log").read_text() == "live1\nlive2\n"


def test_disconnect_flushes_and_closes_the_handle(tmp_path):
    widget = _bind_teardown(_widget(_enabled(tmp_path)))
    widget._start_session_log()
    handle = widget._session_log._file
    TerminalWidget._on_daemon_output(widget, b"unfinished")  # buffered, no newline
    TerminalWidget.disconnect(widget)
    assert handle.closed and widget._session_log is None
    assert (tmp_path / "web.log").read_text() == "unfinished"


def test_daemon_backend_uninstall_closes_the_handle(tmp_path):
    widget = _bind_teardown(_widget(_enabled(tmp_path)))
    widget._start_session_log()
    handle = widget._session_log._file
    TerminalWidget._uninstall_daemon_backend_io(widget)
    assert handle.closed and widget._session_log is None


def test_same_minute_reconnect_through_the_widget_gets_a_suffixed_file(tmp_path, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr("sshpilot.terminal_session_log.datetime", _FakeDatetime)
    _FakeDatetime.current = datetime(2026, 1, 1, 9, 0, 0)
    config = _Config(session_log_enabled=True, session_log_folder=str(tmp_path))  # default pattern
    widget = _widget(config)
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"first\n")
    widget._start_session_log()  # reconnect: new SessionLogger, same pattern, same minute
    TerminalWidget._on_daemon_output(widget, b"second\n")
    widget._stop_session_log()
    files = sorted(p.name for p in (tmp_path / "2026-01-01").iterdir())
    assert files == ["web (10.0.0.5) -2026-01-01- 09-00-2.log", "web (10.0.0.5) -2026-01-01- 09-00.log"]
    assert (tmp_path / "2026-01-01" / files[1]).read_text() == "first\n"
    assert (tmp_path / "2026-01-01" / files[0]).read_text() == "second\n"


def test_widget_flushes_an_idle_prompt_with_the_glib_timeout(tmp_path, monkeypatch):
    callbacks = []
    fake_glib = types.SimpleNamespace(
        timeout_add=lambda ms, cb: callbacks.append(cb) or len(callbacks),
        source_remove=lambda _id: None,
    )
    monkeypatch.setattr(terminal_module, "GLib", fake_glib)
    widget = _widget(_enabled(tmp_path))
    widget._start_session_log()
    TerminalWidget._on_daemon_output(widget, b"out\nuser@host:~$ ")
    TerminalWidget._on_daemon_output(widget, b"l")
    path = tmp_path / "web.log"
    assert path.read_text() == "out\nuser@host:~$ "
    assert callbacks[-1]() is False
    assert path.read_text() == "out\nuser@host:~$ l"
    widget._stop_session_log()
