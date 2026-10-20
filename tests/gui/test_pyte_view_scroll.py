"""Real GTK coverage for the built-in terminal view's scrollback direction."""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, GLib = requires_gui()

from gi.repository import Gdk  # noqa: E402

from sshpilot.terminal_backend_pyte import _PAD_X, _PAD_Y, PyteTerminalView  # noqa: E402

pytestmark = pytest.mark.gui


class _Event:
    """Stand-in for the scroll controller: only the modifier state is read."""

    def get_current_event_state(self):
        return Gdk.ModifierType(0)


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_wheel_and_shift_page_keys_scroll_the_right_way(model, monkeypatch):
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    view = PyteTerminalView()
    view.model.resize(40, 10)
    view.feed(b"".join(b"%d\r\n" % n for n in range(1, 101)))
    assert view.model.history_length > 20

    view._on_scroll(_Event(), 0, -1)  # wheel up: back into the scrollback
    up = view.scroll_offset
    assert up > 0
    view._on_scroll(_Event(), 0, 1)  # wheel down: forward again
    assert view.scroll_offset < up

    view.scroll_offset = 0
    page_up = Gdk.keyval_from_name("Page_Up")
    page_down = Gdk.keyval_from_name("Page_Down")
    view._on_key_pressed(None, page_up, 0, Gdk.ModifierType.SHIFT_MASK)
    assert view.scroll_offset == view.model.lines - 1
    view._on_key_pressed(None, page_down, 0, Gdk.ModifierType.SHIFT_MASK)
    assert view.scroll_offset == 0


@pytest.mark.parametrize("platform, pulls", [("win32", False), ("linux", True), ("darwin", True)])
@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_daemon_sessions_pull_history_on_grow_only_without_conpty(model, platform, pulls, monkeypatch):
    from sshpilot import terminal_backend_pyte

    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    backend = terminal_backend_pyte.PyteTerminalBackend(object())
    assert backend.view.model.pull_history_on_grow
    monkeypatch.setattr(terminal_backend_pyte.sys, "platform", platform)
    backend.prepare_pty_less_emulation()
    assert backend.view.model.pull_history_on_grow is pulls


@pytest.mark.parametrize(
    "dll, opener, backend_env, setting, passthrough",
    [
        (True, True, "", True, True),
        (False, False, "", True, False),  # kernel32
        (True, False, "", True, False),  # conpty.dll starts the inbox conhost
        (True, True, "pywinpty", True, False),
        (True, True, "", False, False),
    ],
)
def test_only_a_passthrough_conpty_lets_the_model_reflow_its_screen(
    dll, opener, backend_env, setting, passthrough, monkeypatch, tmp_path
):
    import types

    from sshpilot import terminal_backend_pyte
    from sshpilot.platform.windows import conpty

    (tmp_path / "conpty.dll").write_bytes(b"")
    if opener:
        (tmp_path / "OpenConsole.exe").write_bytes(b"")
    source = str(tmp_path / "conpty.dll") if dll else "kernel32"
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", "pyte")
    monkeypatch.setenv("SSHPILOT_CONPTY_BACKEND", backend_env)
    settings = {"terminal.reflow_visible_screen": setting}
    config = types.SimpleNamespace(get_setting=lambda key, default=None: settings.get(key, default))
    backend = terminal_backend_pyte.PyteTerminalBackend(types.SimpleNamespace(config=config))
    monkeypatch.setattr(terminal_backend_pyte.sys, "platform", "win32")
    monkeypatch.setattr(conpty, "default_api", lambda: types.SimpleNamespace(source=source))
    backend.prepare_pty_less_emulation()
    assert backend.view.model.pull_history_on_grow is False
    assert backend.view.model.passthrough is passthrough


def test_local_shells_ignore_the_daemon_conpty_backend_switch(monkeypatch, tmp_path):
    """SSHPILOT_CONPTY_BACKEND only picks the daemon's binding; local shells
    always run under the ctypes one and so the bundled conpty.dll."""
    import types

    from sshpilot import terminal_backend_pyte
    from sshpilot.platform.windows import conpty

    (tmp_path / "conpty.dll").write_bytes(b"")
    (tmp_path / "OpenConsole.exe").write_bytes(b"")
    monkeypatch.setenv("SSHPILOT_CONPTY_BACKEND", "pywinpty")
    monkeypatch.setattr(terminal_backend_pyte.sys, "platform", "win32")
    monkeypatch.setattr(conpty, "default_api", lambda: types.SimpleNamespace(source=str(tmp_path / "conpty.dll")))
    assert terminal_backend_pyte.conpty_passes_through(daemon=False)
    assert not terminal_backend_pyte.conpty_passes_through()


def test_scrolled_back_view_keeps_its_text_when_the_main_screen_returns(monkeypatch):
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", "pyte")
    view = PyteTerminalView()
    view.model.resize(40, 5)
    long = b"".join(bytes([97 + n % 26]) for n in range(60))
    view.feed(b"".join(b"h%d\r\n" % n for n in range(8)) + long + b"\r\n" + long + b"\r\n$ vim")
    view.feed(b"\x1b[?1049h\x1b[Heditor")
    view.model.resize(20, 5)
    view.scroll_lines(3)
    top = view.model.row_text(view.model.viewport_top(view.scroll_offset))
    view.feed(b"\x1b[?1049l")
    assert view.model.row_text(view.model.viewport_top(view.scroll_offset)) == top


@pytest.mark.parametrize("model, setting, hint", [("pyte", True, False), ("vterm", True, True), ("vterm", False, False)])
def test_conpty_terminals_log_their_reflow_state_once(model, setting, hint, monkeypatch, tmp_path, caplog):
    import logging
    import types

    from sshpilot import terminal_backend_pyte
    from sshpilot.platform.windows import conpty

    dll = tmp_path / "conpty.dll"
    dll.write_bytes(b"")
    (tmp_path / "OpenConsole.exe").write_bytes(b"")
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    monkeypatch.delenv("SSHPILOT_CONPTY_BACKEND", raising=False)
    settings = {"terminal.reflow_visible_screen": setting}
    config = types.SimpleNamespace(get_setting=lambda key, default=None: settings.get(key, default))
    backend = terminal_backend_pyte.PyteTerminalBackend(types.SimpleNamespace(config=config))
    # Without libvterm installed the view falls back to pyte; the hint keys on the name.
    monkeypatch.setattr(backend.view, "model_name", model)
    monkeypatch.setattr(terminal_backend_pyte.sys, "platform", "win32")
    monkeypatch.setattr(conpty, "default_api", lambda: types.SimpleNamespace(source=str(dll)))
    with caplog.at_level(logging.INFO, logger=terminal_backend_pyte.logger.name):
        backend.prepare_pty_less_emulation()
        backend.prepare_pty_less_emulation()
    states = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ConPTY terminal:")]
    expected_model = type(backend.view.model).__name__
    assert states == [
        f"ConPTY terminal: model={expected_model} pull_history_on_grow=False "
        f"passthrough={setting} reflow_visible_screen={setting} conpty_passes_through=True source={dll}"
    ]
    hints = [r.getMessage() for r in caplog.records if "only available with the pyte model" in r.getMessage()]
    assert len(hints) == (1 if hint else 0)


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_synchronized_update_holds_the_repaint(model, monkeypatch):
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    view = PyteTerminalView()
    draws = []
    monkeypatch.setattr(view, "queue_draw", lambda: draws.append(1))
    sent = []
    monkeypatch.setattr(view, "_send", sent.append)

    view.feed(b"\x1b[?2026h" + b"half-drawn")
    assert draws == [] and view._sync_timer
    view.feed(b"\x1b[?2026$p")
    assert sent == [b"\x1b[?2026;1$y"]
    view.feed(b" done\x1b[?2026l")
    assert draws == [1] and not view._sync_timer
    assert "half-drawn done" in view.model.row_text(view.model.history_length)

    # An update that never ends is painted after the timeout.
    view.feed(b"\x1b[?2026hstuck")
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + 2_000_000
    while len(draws) < 2 and GLib.get_monotonic_time() < deadline:
        context.iteration(False)
    assert len(draws) == 2


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_console_host_title_is_not_shown(model, monkeypatch):
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    view = PyteTerminalView()
    titles = []
    view.connect("window-title-changed", lambda v: titles.append(v.get_window_title()))
    view.feed(b"\x1b]0;" + rb"C:\Windows\System32\OpenSSH\ssh.exe" + b"\x07")
    assert titles == [] and view.get_window_title() == ""
    view.feed(b"\x1b]0;root@router: ~\x07")
    assert titles == ["root@router: ~"]


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_plain_text_link_under_the_pointer(model, monkeypatch):
    from types import SimpleNamespace

    from sshpilot.terminal_backend_pyte import PyteTerminalBackend

    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    backend = PyteTerminalBackend(SimpleNamespace(config=None))
    view = backend.view
    view.model.resize(80, 5)
    view.feed(b"docs at https://example.org/guide now\r\n")
    assert backend.supports_feature("hyperlinks")
    row = view.model.history_length  # first screen row
    column = len("docs at ") + 4
    # Pointer coordinates include the view's padding around the text grid.
    x = _PAD_X + (column + 0.5) * view._cell_w
    y = _PAD_Y + 0.5 * view._cell_h
    assert view._cell_at(x, y) == (row, column)
    assert backend.hyperlink_at(x, y) == "https://example.org/guide"
    assert backend.hyperlink_at(_PAD_X + 0.5 * view._cell_w, y) is None
    backend.destroy()
