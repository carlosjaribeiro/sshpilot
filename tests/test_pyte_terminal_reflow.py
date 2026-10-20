"""Reflow of soft-wrapped lines in the pyte model (terminal-reflow.md)."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

from sshpilot.pyte_terminal_model import TerminalModel  # noqa: E402

LONG = "".join(chr(ord("a") + n % 26) for n in range(60))


def _rows(model):
    rows = [model.row_text(row).rstrip() for row in range(model.total_rows)]
    while rows and not rows[-1]:
        rows.pop()
    return rows


def test_widening_joins_a_wrapped_line_and_narrowing_rewraps_it():
    model = TerminalModel(40, 5)
    model.feed(LONG.encode() + b"\r\n$ ")
    assert _rows(model) == [LONG[:40], LONG[40:], "$"]
    model.resize(80, 5)
    assert _rows(model) == [LONG, "$"]
    model.resize(40, 5)
    assert _rows(model) == [LONG[:40], LONG[40:], "$"]
    model.resize(25, 5)
    assert _rows(model) == [LONG[:25], LONG[25:50], LONG[50:], "$"]


def test_hard_newlines_are_never_joined():
    model = TerminalModel(40, 5)
    # A line that exactly fills the row, then CRLF: a hard break, not a wrap.
    model.feed(b"x" * 40 + b"\r\n" + b"y" * 10 + b"\r\nshort\r\n")
    model.resize(80, 5)
    assert _rows(model) == ["x" * 40, "y" * 10, "short"]


def test_erased_rows_do_not_join():
    model = TerminalModel(40, 5)
    model.feed(LONG.encode() + b"\x1b[2J\x1b[Hone\r\ntwo")
    model.resize(80, 5)
    assert _rows(model) == ["one", "two"]


def test_alternate_screen_is_not_reflowed():
    model = TerminalModel(40, 5)
    model.feed(b"\x1b[?1049h\x1b[H" + LONG.encode())
    model.resize(80, 5)
    assert model.screen.alternate_active
    assert model.row_text(0).rstrip() == LONG[:40]
    assert model.row_text(1).rstrip() == LONG[40:]


def test_scrollback_is_reflowed_while_the_alternate_screen_is_up():
    model = TerminalModel(40, 3)
    model.feed(LONG.encode() + b"\r\n1\r\n2\r\n3\r\n$ vim")
    model.feed(b"\x1b[?1049h\x1b[Heditor")
    model.resize(80, 3)
    assert model.row_text(0).rstrip() == LONG
    model.feed(b"\x1b[?1049l")
    assert _rows(model) == [LONG, "1", "2", "3", "$ vim"]


def test_under_conpty_only_the_scrollback_is_reflowed():
    model = TerminalModel(40, 3)
    model.pull_history_on_grow = False
    model.feed(LONG.encode() + b"\r\n1\r\n2\r\n" + LONG.encode())
    assert model.history_length == 3
    model.resize(80, 3)
    assert model.history_length == 2
    assert [model.row_text(row).rstrip() for row in range(2)] == [LONG, "1"]  # rewrapped
    # Screen rows stay as ConPTY drew them; it repaints them itself.
    assert [model.row_text(model.history_length + y).rstrip() for y in range(3)] == [
        "2", LONG[:40], LONG[40:]
    ]


def test_wide_characters_never_straddle_rows():
    text = "中文" * 15  # 30 wide characters, 60 cells
    model = TerminalModel(40, 5)
    model.feed(text.encode())
    model.resize(80, 5)
    assert _rows(model) == [text]
    line = model.line(0)
    assert (line[0].data, line[1].data, line[58].data, line[59].data) == ("中", "", "文", "")
    model.resize(15, 5)  # odd width: 7 wide characters per row
    rows = _rows(model)
    assert "".join(rows) == text
    assert all(len(row) == 7 for row in rows[:-1])
    model.resize(80, 5)
    assert _rows(model) == [text]


def test_combining_characters_survive_reflow():
    model = TerminalModel(10, 5)
    model.feed(("x" * 9 + "é" + "café").encode())
    model.resize(20, 5)
    assert _rows(model) == ["x" * 9 + "écafé"]


def test_scrollback_limit_is_kept_and_newest_history_survives():
    model = TerminalModel(20, 3, scrollback_limit=5)
    for number in range(8):
        model.feed(b"%d" % number + b"-" * 30 + b"\r\n")
    model.feed(b"$ ")
    model.resize(10, 3)
    assert model.history_length == 5
    assert model.row_text(model.history_length - 1).rstrip() == "-" * 10
    model.resize(40, 3)
    assert model.history_length <= 5
    assert _rows(model)[-2:] == ["7" + "-" * 30, "$"]


def test_cursor_stays_on_the_same_character():
    model = TerminalModel(40, 5)
    model.feed(b"old\r\n$ " + b"c" * 50)
    assert (model.screen.cursor.y, model.screen.cursor.x) == (2, 12)
    model.resize(80, 5)
    assert (model.screen.cursor.y, model.screen.cursor.x) == (1, 52)
    model.feed(b"Z")
    assert _rows(model) == ["old", "$ " + "c" * 50 + "Z"]
    model.feed(b"\x1b[10D")  # cursor back inside the line, then narrow
    model.resize(20, 5)
    assert (model.screen.cursor.y, model.screen.cursor.x) == (3, 3)
    model.feed(b"!")
    assert "".join(_rows(model)[1:]) == "$ " + "c" * 41 + "!" + "c" * 8 + "Z"


def test_cursor_after_a_trailing_blank_stays_put():
    model = TerminalModel(40, 5)
    model.feed(b"$ ")
    model.resize(80, 5)
    assert (model.screen.cursor.y, model.screen.cursor.x) == (0, 2)


def test_widening_pulls_freed_rows_from_history_and_keeps_the_prompt_last():
    model = TerminalModel(40, 4)
    for _ in range(3):
        model.feed(LONG.encode() + b"\r\n")
    model.feed(b"$ ")
    assert model.history_length == 3
    model.resize(80, 4)
    assert model.history_length == 0
    assert _rows(model) == [LONG, LONG, LONG, "$"]
    assert model.screen.cursor.y == 3


def test_colours_and_attributes_are_preserved():
    # What the output highlighter sends: SGR around tokens, default fg after.
    model = TerminalModel(20, 5)
    model.feed(b"ip \x1b[31m10.0.0.1\x1b[39m and \x1b[1;32m10.0.0.2\x1b[0m end")
    before = [cell for row in range(2) for _, cell in sorted(model.line(row).items())]
    model.resize(60, 5)
    after = [cell for _, cell in sorted(model.line(0).items())]
    assert [c for c in before if c.data.strip()] == [c for c in after if c.data.strip()]
    line = model.line(0)
    assert (line[3].fg, line[10].fg, line[11].fg) == ("red", "red", "default")
    assert (line[16].fg, line[16].bold) == ("green", True)


def test_reflow_can_be_turned_off():
    model = TerminalModel(40, 5)
    model.screen.reflow = False
    model.feed(LONG.encode())
    model.resize(80, 5)
    assert _rows(model) == [LONG[:40], LONG[40:]]


# Erasing or shifting cells breaks the link at the edited edge; it must not
# re-join erased text, or text written later, with unrelated rows.
_EDITS = {
    # ED 1 with the cursor on the continuation row: both rows lose the link.
    "ED1": (LONG.encode() + b"\x1b[2;5H\x1b[1J", ["", "     " + LONG[45:]]),
    "ED0": (b"top\r\n" + LONG.encode() + b"\x1b[2;5H\x1b[0Jnew", ["top", LONG[:4] + "new"]),
    "EL0-at-column-0": (LONG.encode() + b"\x1b[2;1H\x1b[0Knew", [LONG[:40], "new"]),
    "EL1": (LONG.encode() + b"\x1b[2;3H\x1b[1K", [LONG[:40], "   " + LONG[43:]]),
    "EL2": (LONG.encode() + b"\x1b[1;1H\x1b[2Knew", ["new", LONG[40:]]),
    "ECH-at-start": (LONG.encode() + b"\x1b[2;1H\x1b[3X", [LONG[:40], "   " + LONG[43:]]),
    "ECH-at-end": (LONG.encode() + b"\x1b[1;39H\x1b[2X", [LONG[:38], LONG[40:]]),
    "DCH": (LONG.encode() + b"\x1b[1;5H\x1b[3P", [LONG[:4] + LONG[7:40], LONG[40:]]),
    "ICH": (LONG.encode() + b"\x1b[1;5H\x1b[3@", [LONG[:4] + "   " + LONG[4:37], LONG[40:]]),
    "IL": (LONG.encode() + b"\x1b[2;1H\x1b[Lnew", [LONG[:40], "new", LONG[40:]]),
    "DL": (b"top\r\n" + LONG.encode() + b"\x1b[2;1H\x1b[M", ["top", LONG[40:]]),
    # Scrolling inside a region discards the row above the continuation.
    "IND-in-region": (b"head\r\n\x1b[2;3r\x1b[2;1H" + LONG.encode() + b"\n", ["head", LONG[40:]]),
    "RI-in-region": (
        LONG.encode() + b"\x1b[2;4r\x1b[2;1H\x1bMnew",
        [LONG[:40], "new", LONG[40:]],
    ),
}


@pytest.mark.parametrize("data, expected", _EDITS.values(), ids=_EDITS.keys())
def test_edits_break_the_wrap_link_at_the_edited_edge(data, expected):
    model = TerminalModel(40, 6)
    model.feed(data)
    model.resize(80, 6)
    assert _rows(model) == expected


def test_overwriting_a_wrapped_row_in_place_keeps_the_link():
    # Readline-style redraw: cursor back to the row start, text rewritten.
    model = TerminalModel(40, 5)
    model.feed(LONG.encode() + b"\x1b[2;1H" + LONG[40:45].upper().encode())
    model.resize(80, 5)
    assert _rows(model) == [LONG[:40] + LONG[40:45].upper() + LONG[45:]]


def test_cursor_on_a_wide_character_continuation():
    text = "中文" * 15  # 60 cells: 40 on the first row, 20 on the second
    model = TerminalModel(40, 5)
    model.feed(text.encode() + b"\x1b[2;6H")  # the stub of the third wide char
    assert (model.screen.cursor.y, model.screen.cursor.x) == (1, 5)
    model.resize(80, 5)
    assert (model.screen.cursor.y, model.screen.cursor.x) == (0, 44)  # its lead
    model.resize(15, 5)  # 7 wide chars per row: the 23rd starts row 3
    assert (model.screen.cursor.y, model.screen.cursor.x) == (3, 2)
    model.feed(b"X")
    assert model.line(model.history_length + 3)[2].data == "X"
