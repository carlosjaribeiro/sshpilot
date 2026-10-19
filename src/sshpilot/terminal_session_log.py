"""Automatic per-session terminal logging (SecureCRT style).

Pure Python, no GObject: the GTK terminal feeds raw output bytes in and this
module turns them into plain text on disk. Terminal payload is session data,
so nothing here (or in its callers) may send it to the application logs.

Threading: ``SessionLogger`` is not locked. The terminal calls it only from
the GTK main thread: ``GtkClientBridge._schedule_locked`` queues ``_drain``
through ``gtk_thread_idle`` (gtk_client_bridge.py:594), and ``_drain``
delivers output (gtk_client_bridge.py:647) to the widget. Do not call it from
a worker thread.

Pattern placeholders, expanded once when a session starts (SecureCRT's
meaning, which is *not* strftime's: ``%M`` is the month, ``%m`` the minute):

    %S session name   %H host   %Y year   %M month   %D day
    %h hour (24h)     %m minute %s second %% literal percent
"""
from __future__ import annotations

import codecs
import logging
import os
import re
import time
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATTERN = "%Y-%M-%D/%S (%H) -%Y-%M-%D- %h-%m.log"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
_WINDOWS_ABS = re.compile(r"^([A-Za-z]:[\\/]|\\\\)")
# A ".." path component (Windows also drops trailing dots/spaces, so "../"
# and ".. \\" mean the same thing).
_DOTDOT = re.compile(r"(?<![^/\\])\.\.[ .]*(?![^/\\])")


def sanitise_component(value: object, fallback: str = "terminal") -> str:
    """Make *value* safe as one path component on Windows and POSIX."""
    text = _ILLEGAL.sub("_", str(value or "")).rstrip(" .")
    if not text:
        return fallback
    # "CON" and "CON.txt" are both reserved on Windows; the stem decides.
    if _RESERVED.match(text.split(".", 1)[0]):
        return "_" + text
    return text


def expand_pattern(pattern: str, session: str, host: str, now: datetime) -> str:
    """Expand the placeholders of *pattern* (``~`` and env vars as well)."""
    values = {
        "S": sanitise_component(session, "terminal"),
        "H": sanitise_component(host, "host"),
        "Y": f"{now.year:04d}",
        "M": f"{now.month:02d}",
        "D": f"{now.day:02d}",
        "h": f"{now.hour:02d}",
        "m": f"{now.minute:02d}",
        "s": f"{now.second:02d}",
        "%": "%",
    }
    expanded = re.sub(r"%(.)", lambda m: values.get(m.group(1), m.group(0)), pattern)
    # A relative pattern must not climb out of the log folder, so ".." is
    # neutralised (also after ~ and env expansion, which can introduce it).
    return _DOTDOT.sub("_", os.path.expandvars(os.path.expanduser(expanded)))


def windows_path_on_posix(pattern: str, windows: Optional[bool] = None) -> bool:
    """True when *pattern* is a Windows drive/UNC path but this OS is POSIX."""
    if windows is None:
        windows = os.name == "nt"
    return not windows and bool(_WINDOWS_ABS.match((pattern or "").strip()))


def resolve_log_path(
    folder: str,
    pattern: str,
    session: str,
    host: str,
    now: datetime,
    windows: Optional[bool] = None,
) -> str:
    """Resolve folder + pattern to the file path for a session starting *now*.

    An absolute pattern (POSIX ``/``, drive letter or UNC) ignores *folder*.
    Both ``/`` and ``\\`` separate directories in the pattern, so a Windows
    style pattern also works on POSIX. *windows* defaults to the host OS.
    """
    if windows is None:
        windows = os.name == "nt"
    expanded = expand_pattern(pattern or DEFAULT_PATTERN, session, host, now)
    if windows:
        return str(PureWindowsPath(folder or "", expanded))
    # shortcut: a Windows drive/UNC pattern on POSIX is kept as a relative
    # path; it is only meaningful on Windows.
    return str(PurePosixPath(folder or "", expanded.replace("\\", "/")))


class TerminalTextStripper:
    """Streaming terminal-bytes to plain-text converter.

    Drops CSI/OSC/DCS/APC/PM/SOS and other ESC sequences, even when a
    sequence is split across chunks, and normalises CRLF to LF.
    shortcut: backspace and lone CR are dropped, not replayed, so redrawn
    lines (progress bars, readline edits) log every redraw; upgrade if users
    want line-accurate logs.
    """

    _TEXT, _ESC, _CSI, _STRING, _STRING_ESC, _ESC_INT = range(6)

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = self._TEXT

    def feed(self, data: bytes) -> str:
        out = []
        for ch in self._decoder.decode(data):
            self._step(ch, out)
        return "".join(out)

    def _step(self, ch: str, out: list) -> None:
        state = self._state
        if state == self._TEXT:
            if ch == "\x1b":
                self._state = self._ESC
            elif ch == "\n" or ch == "\t" or (ch >= " " and ch != "\x7f"):
                out.append(ch)
            # CR (so CRLF becomes LF) and other C0 controls are dropped
        elif state == self._ESC:
            if ch == "[":
                self._state = self._CSI
            elif ch in "]PX^_":
                self._state = self._STRING
            elif " " <= ch <= "/":
                self._state = self._ESC_INT
            else:
                self._state = self._TEXT
        elif state == self._CSI:
            if "@" <= ch <= "~":
                self._state = self._TEXT
        elif state == self._ESC_INT:
            if "0" <= ch <= "~":
                self._state = self._TEXT
        elif state == self._STRING:
            if ch == "\x07":
                self._state = self._TEXT
            elif ch == "\x1b":
                self._state = self._STRING_ESC
        else:  # _STRING_ESC
            if ch == "\\":
                self._state = self._TEXT
            else:
                # A new escape aborts the unterminated string.
                self._state = self._ESC
                self._step(ch, out)


_FLUSH_INTERVAL = 1.0  # seconds
_open_paths: set = set()  # files held open by a SessionLogger in this process


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _free_path(path: str, used: set) -> str:
    """Add a "-2", "-3"... suffix when *path* is held by a live session or was
    already used by this logger (a reconnect within the same minute/second).
    A file left by an earlier run is still appended to."""
    root, ext = os.path.splitext(path)
    candidate, n = path, 2
    while _path_key(candidate) in _open_paths or candidate in used:
        candidate = f"{root}-{n}{ext}"
        n += 1
    return candidate


class SessionLogger:
    """Append the plain-text transcript of one terminal session to a file.

    ``start`` can be called again (reconnect): it closes the old file and
    opens a new one named for the new start time; if that name equals the
    previous one (same-second reconnect) or another live session's file, a
    ``-2`` style suffix keeps sessions apart. A file error never raises into
    the terminal; it is reported once through ``on_error`` and logging stays
    off until the next ``start``.

    Output is buffered and flushed when a chunk ends a line or a second has
    passed, and always on ``close``. shortcut: a hard crash can lose up to
    one second of an unfinished line; upgrade to an idle-timer flush if that
    matters.
    """

    def __init__(
        self,
        folder: str,
        pattern: str = DEFAULT_PATTERN,
        on_error: Optional[Callable[[OSError], None]] = None,
    ) -> None:
        self.folder = folder
        self.pattern = pattern
        self._on_error = on_error
        self._file = None
        self._stripper = TerminalTextStripper()
        self._last_flush = 0.0
        self._key: Optional[str] = None
        self._used: set = set()
        self.path: Optional[str] = None

    def start(self, session: str, host: str, now: Optional[datetime] = None) -> Optional[str]:
        self.close()
        self._stripper = TerminalTextStripper()
        self.path = None
        path = _free_path(
            resolve_log_path(self.folder, self.pattern, session, host, now or datetime.now()),
            self._used,
        )
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            # newline="\n": no platform translation, the stripper emits LF.
            self._file = open(path, "a", encoding="utf-8", newline="\n")
        except OSError as exc:
            self._fail(exc)
            return None
        self.path = path
        self._used.add(path)
        self._key = _path_key(path)
        _open_paths.add(self._key)
        self._last_flush = time.monotonic()
        return path

    def write(self, chunk: bytes) -> None:
        if self._file is None or not chunk:
            return
        text = self._stripper.feed(chunk)
        if not text:
            return
        try:
            self._file.write(text)
            if text.endswith("\n") or time.monotonic() - self._last_flush >= _FLUSH_INTERVAL:
                self._file.flush()
                self._last_flush = time.monotonic()
        except OSError as exc:
            self._fail(exc)

    def close(self) -> None:
        handle, self._file = self._file, None
        _open_paths.discard(self._key)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                logger.debug("Session log close failed", exc_info=True)

    def _fail(self, exc: OSError) -> None:
        self.close()
        # Only the OS error is logged, never terminal text.
        logger.warning("Session logging stopped: %s", exc)
        if self._on_error is not None:
            try:
                self._on_error(exc)
            except Exception:
                logger.debug("Session log error callback failed", exc_info=True)


def build_session_logger(
    get_setting: Callable[[str, object], object],
    default_folder: Callable[[], str],
    on_error: Optional[Callable[[OSError], None]] = None,
) -> Optional[SessionLogger]:
    """Return a logger for the current settings, or None when disabled."""
    if get_setting("terminal.session_log_enabled", False) is not True:
        return None
    folder = get_setting("terminal.session_log_folder", "")
    pattern = get_setting("terminal.session_log_pattern", "")
    folder = folder if isinstance(folder, str) and folder.strip() else default_folder()
    pattern = pattern if isinstance(pattern, str) and pattern.strip() else DEFAULT_PATTERN
    return SessionLogger(folder, pattern, on_error)
