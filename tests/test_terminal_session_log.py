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
    windows_path_on_posix,
)

NOW = datetime(2026, 3, 9, 14, 7, 5)
USER_PATTERN = r"C:\Users\user\Documents\Logs\%Y-%M-%D\%S (%H) -%Y-%M-%D- %h-%m.log"


def test_placeholders_month_is_M_and_minute_is_m():
    assert expand_pattern("%Y|%M|%D|%h|%m|%s|%%", "s", "h", NOW) == "2026|03|09|14|07|05|%"


def test_session_and_host_placeholders():
    assert expand_pattern("%S@%H", "web 1", "192.0.2.5", NOW) == "web 1@192.0.2.5"


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
        "C:\\Users\\user\\Documents\\Logs\\2026-03-09"
        "\\srv (h1) -2026-03-09- 14-07.log"
    )


def test_user_pattern_split_into_folder_and_relative_pattern_windows():
    folder = r"C:\Users\user\Documents\Logs"
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


def test_second_session_in_a_run_with_a_static_pattern_gets_its_own_file(tmp_path):
    for text in (b"one\n", b"two\n"):
        log = SessionLogger(str(tmp_path), "s.log")
        log.start("s", "h", NOW)
        log.write(text)
        log.close()
    assert (tmp_path / "s.log").read_text() == "one\n"
    assert (tmp_path / "s-2.log").read_text() == "two\n"


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
        lambda: "/d",
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


import ntpath  # noqa: E402
import posixpath  # noqa: E402

import pytest  # noqa: E402

from sshpilot import terminal_session_log as tsl  # noqa: E402

TRAVERSAL = [
    "../../outside.log",
    "..\\..\\outside.log",
    "a/../../outside.log",
    "a\\../..\\outside.log",
    "./../outside.log",
    ".. /outside.log",
    "...\\outside.log",
    "%S/../../outside.log",
]


@pytest.mark.parametrize("pattern", TRAVERSAL)
def test_relative_pattern_cannot_escape_folder_posix(pattern):
    path = posixpath.normpath(resolve_log_path("/logs", pattern, "s", "h", NOW, windows=False))
    assert posixpath.commonpath([path, "/logs"]) == "/logs"


@pytest.mark.parametrize("pattern", TRAVERSAL)
def test_relative_pattern_cannot_escape_folder_windows(pattern):
    path = ntpath.normpath(resolve_log_path("C:\\logs", pattern, "s", "h", NOW, windows=True))
    assert ntpath.commonpath([path, "C:\\logs"]) == "C:\\logs"


def test_dotdot_cannot_escape_through_real_files(tmp_path):
    folder = tmp_path / "a" / "b" / "logs"
    log = SessionLogger(str(folder), "../../outside.log")
    path = log.start("s", "h", NOW)
    log.write(b"x\n")
    log.close()
    assert Path(path).resolve().is_relative_to(folder.resolve())
    assert not (tmp_path / "a" / "outside.log").exists()


def test_dotdot_neutralised_in_env_expansion(monkeypatch):
    monkeypatch.setenv("UP", "..")
    path = resolve_log_path("/logs", "$UP/$UP/x.log", "s", "h", NOW, windows=False)
    assert posixpath.commonpath([posixpath.normpath(path), "/logs"]) == "/logs"


def test_names_that_merely_contain_dots_are_kept():
    assert expand_pattern("..hidden/a..b/c.log", "s", "h", NOW) == "..hidden/a..b/c.log"


def test_same_second_reconnect_gets_a_suffix_instead_of_appending(tmp_path):
    log = SessionLogger(str(tmp_path), "%S %h-%m.log")
    first = log.start("s", "h", NOW)
    log.write(b"one\n")
    second = log.start("s", "h", NOW)
    log.write(b"two\n")
    third = log.start("s", "h", NOW)
    log.close()
    assert [Path(p).name for p in (first, second, third)] == [
        "s 14-07.log", "s 14-07-2.log", "s 14-07-3.log",
    ]
    assert Path(first).read_text() == "one\n"
    assert Path(second).read_text() == "two\n"


def test_two_live_sessions_with_one_pattern_never_share_a_file(tmp_path):
    a, b = SessionLogger(str(tmp_path), "s.log"), SessionLogger(str(tmp_path), "s.log")
    assert a.start("s", "h", NOW) != b.start("s", "h", NOW)
    a.close()
    b.close()


def test_reopening_after_close_still_appends_to_an_older_file(tmp_path):
    (tmp_path / "s.log").write_text("old\n")
    log = SessionLogger(str(tmp_path), "s.log")
    assert log.start("s", "h", NOW) == str(tmp_path / "s.log")
    log.write(b"new\n")
    log.close()
    assert (tmp_path / "s.log").read_text() == "old\nnew\n"


def test_output_is_buffered_until_a_line_ends_and_flushed_on_close(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(tsl.time, "monotonic", lambda: clock[0])
    log = SessionLogger(str(tmp_path), "s.log")
    path = Path(log.start("s", "h", NOW))
    log.write(b"partial")
    assert path.read_text() == ""  # not flushed per chunk
    log.write(b" line\n")
    assert path.read_text() == "partial line\n"  # a finished line is flushed
    log.write(b"tail")
    assert path.read_text() == "partial line\n"
    clock[0] += 2
    log.write(b"!")
    assert path.read_text() == "partial line\ntail!"  # interval flush
    log.write(b" more")
    log.close()
    assert path.read_text() == "partial line\ntail! more"  # close flushes


def test_windows_drive_pattern_on_posix_is_flagged():
    assert windows_path_on_posix(USER_PATTERN, windows=False)
    assert windows_path_on_posix("\\\\nas\\share\\x.log", windows=False)
    assert not windows_path_on_posix(USER_PATTERN, windows=True)
    assert not windows_path_on_posix("/var/log/%S.log", windows=False)


@pytest.fixture(autouse=True)
def _fresh_run_registry():
    # getattr: also usable against commits that predate the registry.
    getattr(tsl, "_used_paths", {}).clear()


@pytest.mark.parametrize("nickname", ["~", "~/x", "$HOME", "${HOME}", "%S", "%%", "..", "~root"])
def test_nickname_and_host_values_are_inert(nickname, monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
    for windows in (False, True):
        path = resolve_log_path("/logs", "%S/%H.log", nickname, nickname, NOW, windows=windows)
        norm = (ntpath if windows else posixpath).normpath(path)
        root = "/logs" if not windows else ntpath.normpath("/logs")
        assert (ntpath if windows else posixpath).commonpath([norm, root]) == root, path
        assert "/home/u" not in path.replace("\\", "/")


def test_tilde_and_env_in_the_template_still_expand(monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
    monkeypatch.setenv("SUB", "work")
    assert resolve_log_path("~/logs", "$SUB/%S.log", "s", "h", NOW, windows=False) == "/home/u/logs/work/s.log"


def test_sanitise_values_have_no_expansion_characters():
    for ch in "$%{}":
        assert ch not in sanitise_component(f"a{ch}b")
    assert not sanitise_component("~user").startswith("~")


def test_unc_and_drive_patterns_stay_inside_folder_on_posix():
    for pattern in ("\\\\nas\\share\\%S.log", "C:\\x\\%S.log", "C:/x/%S.log"):
        path = resolve_log_path("/logs", pattern, "s", "h", NOW, windows=False)
        assert posixpath.commonpath([posixpath.normpath(path), "/logs"]) == "/logs", path


def test_rooted_and_drive_relative_patterns_stay_inside_folder_on_windows():
    for pattern in ("\\foo\\%S.log", "/foo/%S.log", "D:foo\\%S.log"):
        path = resolve_log_path("C:\\logs", pattern, "s", "h", NOW, windows=True)
        assert ntpath.commonpath([ntpath.normpath(path), "C:\\logs"]) == "C:\\logs", path
    # a real UNC or drive-absolute pattern still wins over the folder
    assert resolve_log_path("C:\\logs", "\\\\nas\\s\\x.log", "s", "h", NOW, windows=True) == "\\\\nas\\s\\x.log"
    assert resolve_log_path("C:\\logs", "D:\\x.log", "s", "h", NOW, windows=True) == "D:\\x.log"


def test_idle_timer_flushes_an_unfinished_prompt(tmp_path):
    timers = {}
    log = SessionLogger(
        str(tmp_path), "s.log",
        timer_add=lambda ms, cb: timers.setdefault("cb", cb) and "id1",
        timer_remove=lambda tid: timers.update(removed=tid),
    )
    path = Path(log.start("s", "h", NOW))
    log.write(b"out\nuser@host:~$ ")  # a finished line flushes everything written
    assert path.read_text() == "out\nuser@host:~$ "
    log.write(b"ls")
    assert path.read_text() == "out\nuser@host:~$ "  # tail waits for the timer
    assert timers["cb"]() is False  # one-shot GLib timeout
    assert path.read_text() == "out\nuser@host:~$ ls"


def test_close_cancels_the_pending_idle_timer(tmp_path):
    removed = []
    log = SessionLogger(str(tmp_path), "s.log", timer_add=lambda ms, cb: "t", timer_remove=removed.append)
    log.start("s", "h", NOW)
    log.write(b"abc")
    log.close()
    assert removed == ["t"]
    assert (tmp_path / "s.log").read_text() == "abc"


def test_stripper_repeated_esc_and_restart_inside_sequences():
    assert strip(b"a\x1b\x1b[31mb") == "ab"
    assert strip(b"a\x1b[3\x1b[31mb") == "ab"
    assert strip(b"a\x1b(\x1b[0mb") == "ab"


def test_stripper_can_and_sub_abort_a_sequence():
    assert strip(b"a\x1b[3\x18b") == "ab"
    assert strip(b"a\x1b]0;ti\x1ab") == "ab"


def test_bel_ends_osc_but_not_dcs_apc_pm_sos():
    assert strip(b"a\x1b]0;t\x07b") == "ab"
    for intro in (b"P", b"_", b"^", b"X"):
        assert strip(b"a\x1b" + intro + b"hidden\x07still\x1b\\b") == "ab"


def test_truncated_utf8_at_close_is_written_not_lost(tmp_path):
    log = SessionLogger(str(tmp_path), "s.log")
    path = Path(log.start("s", "h", NOW))
    log.write("é".encode()[:1])
    log.close()
    assert path.read_text(encoding="utf-8") == "\ufffd"


@pytest.mark.parametrize("pattern", ["//server/share/%S.log", "\\\\server\\share\\%S.log", "/\\server/share/%S.log", "///x/%S.log"])
def test_double_leading_separator_is_unc_and_stays_inside_folder_on_posix(pattern):
    path = resolve_log_path("/logs", pattern, "s", "h", NOW, windows=False)
    assert posixpath.commonpath([posixpath.normpath(path), "/logs"]) == "/logs", path
    assert windows_path_on_posix(pattern, windows=False)


def test_single_leading_slash_stays_an_explicit_absolute_override():
    assert resolve_log_path("/logs", "/var/log/%S.log", "s", "h", NOW, windows=False) == "/var/log/s.log"
    assert not windows_path_on_posix("/var/log/%S.log", windows=False)


@pytest.mark.parametrize("pattern", ["//server/share/%S.log", "\\\\server\\share\\%S.log"])
def test_double_leading_separator_is_a_real_unc_override_on_windows(pattern):
    assert resolve_log_path("C:\\logs", pattern, "s", "h", NOW, windows=True) == "\\\\server\\share\\s.log"
    assert not windows_path_on_posix(pattern, windows=True)


def test_trailing_dots_and_spaces_are_trimmed_per_component_on_windows():
    path = resolve_log_path("C:\\logs", "dir. /sub .\\%S.log. ", "s", "h", NOW, windows=True)
    assert path == "C:\\logs\\dir\\sub\\s.log"
    # POSIX keeps the names as written
    assert resolve_log_path("/logs", "dir. /%S.log", "s", "h", NOW, windows=False) == "/logs/dir. /s.log"


def test_path_key_treats_windows_trailing_dot_space_aliases_as_one_file():
    assert tsl._path_key("a/b. ", windows=True) == tsl._path_key("a/b", windows=True)
    assert tsl._path_key("a/b. ", windows=False) != tsl._path_key("a/b", windows=False)


def test_used_path_registry_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tsl, "_USED_PATHS_MAX", 3)
    for n in range(10):
        log = SessionLogger(str(tmp_path), f"f{n}.log")
        log.start("s", "h", NOW)
        log.close()
    assert len(tsl._used_paths) == 3


def test_a_used_path_whose_file_was_deleted_is_reused(tmp_path):
    log = SessionLogger(str(tmp_path), "s.log")
    first = log.start("s", "h", NOW)
    log.close()
    Path(first).unlink()
    assert log.start("s", "h", NOW) == first
    log.close()
