"""Automatic session logging: pattern expansion, text stripping, file handling."""
from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

from sshpilot.terminal_session_log import (
    DEFAULT_PATTERN,
    SessionLogger,
    TerminalTextStripper,
    build_session_logger,
    expand_pattern,
    resolve_log_path,
    sanitise_component,
)

NOW = datetime(2026, 3, 9, 14, 7, 5)
USER_PATTERN = r"C:\Users\carlos\OneDrive - VINCI Energies\Documentos\Logs\%Y-%M-%D\%S (%H) -%Y-%M-%D- %h-%m.log"


def test_placeholders_month_is_M_and_minute_is_m():
    assert expand_pattern("%Y|%M|%D|%h|%m|%s|%%", "s", "h", NOW) == "2026|03|09|14|07|05|%"


def test_session_and_host_placeholders():
    assert expand_pattern("%S@%H", "web 1", "10.0.0.5", NOW) == "web 1@10.0.0.5"


def test_session_name_cannot_inject_separators():
    assert expand_pattern("%S", "../../etc/x", "h", NOW) == ".._.._etc_x"
    assert sanitise_component("..") == "terminal"


def test_sanitise_illegal_characters_and_trailing_dots():
    assert sanitise_component('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"
    assert sanitise_component("name. . ") == "name"
    assert sanitise_component("") == "terminal"
    assert sanitise_component("a\x00b\x1fc") == "a_b_c"


def test_sanitise_guards_windows_reserved_names():
    for name in ("CON", "nul", "COM1", "lpt9", "aux.txt"):
        assert sanitise_component(name).startswith("_")
    assert sanitise_component("COM10") == "COM10"
    assert sanitise_component("console") == "console"


def test_default_pattern_makes_per_day_subfolder_posix():
    path = resolve_log_path("/logs", DEFAULT_PATTERN, "srv", "h1", NOW, windows=False)
    assert path == "/logs/2026-03-09/srv (h1) -2026-03-09- 14-07.log"


def test_user_pattern_as_one_absolute_windows_path():
    path = resolve_log_path("", USER_PATTERN, "srv", "h1", NOW, windows=True)
    assert path == (
        "C:\\Users\\carlos\\OneDrive - VINCI Energies\\Documentos\\Logs\\2026-03-09"
        "\\srv (h1) -2026-03-09- 14-07.log"
    )


def test_user_pattern_split_into_folder_and_relative_pattern_windows():
    folder = r"C:\Users\carlos\OneDrive - VINCI Energies\Documentos\Logs"
    relative = r"%Y-%M-%D\%S (%H) -%Y-%M-%D- %h-%m.log"
    full = resolve_log_path("", USER_PATTERN, "srv", "h1", NOW, windows=True)
    assert resolve_log_path(folder, relative, "srv", "h1", NOW, windows=True) == full


def test_absolute_pattern_ignores_folder_windows_unc_and_posix():
    assert resolve_log_path("D:\\other", r"\\nas\share\%S.log", "s", "h", NOW, windows=True) == "\\\\nas\\share\\s.log"
    assert resolve_log_path("/ignored", "/var/log/%S.log", "s", "h", NOW, windows=False) == "/var/log/s.log"


def test_backslash_pattern_creates_subfolders_on_posix():
    assert resolve_log_path("/logs", r"%Y-%M-%D\%S.log", "s", "h", NOW, windows=False) == "/logs/2026-03-09/s.log"


def test_home_and_env_expansion(monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
    monkeypatch.setenv("LOGROOT", "/srv/l")
    assert resolve_log_path("", "~/x/%S.log", "s", "h", NOW, windows=False) == "/home/u/x/s.log"
    assert resolve_log_path("", "$LOGROOT/%S.log", "s", "h", NOW, windows=False) == "/srv/l/s.log"


def strip(*chunks: bytes) -> str:
    stripper = TerminalTextStripper()
    return "".join(stripper.feed(chunk) for chunk in chunks)


def test_strips_csi_osc_dcs_and_normalises_crlf():
    raw = (
        b"\x1b[31mred\x1b[0m\r\nnext\x1b]0;title\x07 \x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\"
        b"\x1bP1$r0m\x1b\\ \x1b(B\x1b7ok\x07\r\n"
    )
    assert strip(raw) == "red\nnext link ok\n"


def test_escape_split_across_chunks_is_stripped():
    assert strip(b"a\x1b", b"[3", b"1mb\x1b]0;ti", b"tle\x1b", b"\\c") == "abc"
    assert strip(b"x\r", b"\ny") == "x\ny"


def test_utf8_split_across_chunks_is_kept():
    data = "olá ✓".encode()
    assert strip(data[:3], data[3:]) == "olá ✓"


def test_logger_writes_plain_text_in_subfolder(tmp_path):
    log = SessionLogger(str(tmp_path), "%Y-%M-%D/%S.log")
    path = log.start("srv", "h", NOW)
    log.write(b"\x1b[1mhello\x1b[0m\r\n")
    log.close()
    assert Path(path) == tmp_path / "2026-03-09" / "srv.log"
    assert Path(path).read_text(encoding="utf-8") == "hello\n"


def test_logger_appends_to_existing_file(tmp_path):
    for text in (b"one\n", b"two\n"):
        log = SessionLogger(str(tmp_path), "s.log")
        log.start("s", "h", NOW)
        log.write(text)
        log.close()
    assert (tmp_path / "s.log").read_text() == "one\ntwo\n"


def test_reconnect_starts_a_new_file_with_new_start_time(tmp_path):
    log = SessionLogger(str(tmp_path), "%S %h-%m.log")
    first = log.start("s", "h", datetime(2026, 1, 1, 10, 0))
    log.write(b"a\x1b")
    second = log.start("s", "h", datetime(2026, 1, 1, 10, 5))
    log.write(b"b\n")  # stripper state is reset, the dangling ESC is not carried over
    log.close()
    assert first != second
    assert Path(first).read_text() == "a"
    assert Path(second).read_text() == "b\n"


def test_oserror_on_open_is_reported_once_and_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    errors = []
    log = SessionLogger(str(blocker), "sub/s.log", errors.append)  # parent is a file
    assert log.start("s", "h", NOW) is None
    log.write(b"data")
    log.write(b"more")
    assert len(errors) == 1


def test_oserror_on_write_stops_logging_and_reports_once(tmp_path):
    errors = []
    log = SessionLogger(str(tmp_path), "s.log", errors.append)
    log.start("s", "h", NOW)

    class Broken:
        def write(self, _text):
            raise OSError("disk full")

        def close(self):
            pass

    log._file = Broken()
    log.write(b"x")
    log.write(b"y")
    assert len(errors) == 1 and log._file is None


def test_build_session_logger_toggle_and_fallbacks():
    def settings(values):
        return lambda key, default=None: values.get(key, default)

    assert build_session_logger(settings({}), lambda: "/d") is None
    assert build_session_logger(settings({"terminal.session_log_enabled": False}), lambda: "/d") is None
    # A truthy non-bool (e.g. a mocked config) must not enable logging.
    assert build_session_logger(settings({"terminal.session_log_enabled": object()}), lambda: "/d") is None
    log = build_session_logger(settings({"terminal.session_log_enabled": True}), lambda: "/d")
    assert (log.folder, log.pattern) == ("/d", DEFAULT_PATTERN)
    log = build_session_logger(
        settings({
            "terminal.session_log_enabled": True,
            "terminal.session_log_folder": "/mine",
            "terminal.session_log_pattern": "%S.log",
        }),
        "/d",
    )
    assert (log.folder, log.pattern) == ("/mine", "%S.log")


def test_module_is_gi_free():
    path = Path(__file__).resolve().parents[1] / "src" / "sshpilot" / "terminal_session_log.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    assert not imported & {"gi", "Gtk", "Adw", "Gio", "GLib", "Vte"}
