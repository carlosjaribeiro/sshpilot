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


def test_rows_pushed_by_a_diagonal_shrink_under_conpty_are_rewrapped():
    model = TerminalModel(80, 6)
    model.pull_history_on_grow = False
    for number in range(6):
        model.feed(b"%d" % number + b"-" * 70 + b"\r\n")
    model.feed(b"$ ")
    model.resize(40, 3)  # narrower and shorter at once
    assert all(max(model.line(row)) < 40 for row in range(model.history_length))
    model.resize(80, 3)
    history = [model.row_text(row).rstrip() for row in range(model.history_length)]
    assert history == [f"{number}" + "-" * 70 for number in range(len(history))]


def test_a_conpty_repaint_does_not_join_its_first_row_to_the_scrollback():
    from tests.test_pyte_terminal_model import _conhost_repaint

    model = TerminalModel(40, 3)
    model.pull_history_on_grow = False
    model.feed(b"head\r\n" + LONG.encode() + b"\r\n\r\n")  # LONG[40:] now on row 0
    _conhost_repaint(model, [b"other", b"x", b"$ "])
    model.feed(b"\r\n" * 3)
    model.resize(80, 3)
    assert _rows(model)[:4] == ["head", LONG[:40], "other", "x"]


def test_main_screen_behind_the_alternate_screen_is_reflowed_on_return():
    model = TerminalModel(40, 5)
    model.feed(LONG.encode() + b"\r\n$ vim")
    model.feed(b"\x1b[?1049h\x1b[Heditor")
    model.resize(20, 5)
    model.resize(40, 5)
    model.feed(b"\x1b[?1049l")
    assert _rows(model) == [LONG[:40], LONG[40:], "$ vim"]
    model.feed(b"X")
    model.resize(80, 5)
    assert _rows(model) == [LONG, "$ vimX"]


def test_main_screen_cut_on_the_alternate_screen_under_conpty_is_never_spliced():
    model = TerminalModel(40, 5)
    model.pull_history_on_grow = False
    model.feed(LONG.encode() + b"\r\n$ vim")
    model.feed(b"\x1b[?1049h\x1b[Heditor")
    model.resize(20, 5)  # cuts LONG[20:40] off the saved row
    model.resize(40, 5)
    model.feed(b"\x1b[?1049l" + b"\r\n" * 5)  # into the scrollback, which is reflowed
    model.resize(80, 5)
    assert LONG[:20] + LONG[40:] not in "\n".join(_rows(model))
    assert _rows(model) == [LONG[:20], LONG[40:], "$ vim"]


def test_spaces_at_wrap_points_survive_round_trips():
    text = "a" * 39 + " b" + "c d " * 10
    model = TerminalModel(40, 5)
    model.feed(text.encode())
    narrow = _rows(model)
    for columns in (80, 13, 17, 7, 40):
        model.resize(columns, 5)
    assert _rows(model) == narrow
    model.resize(80, 5)
    assert _rows(model) == [text.rstrip()]


def _links(model):
    return [bool(getattr(model.screen.buffer.get(y), "continued", False)) for y in range(model.lines)]


# Rows 1 and 2 continue row 0; the cursor goes to row 1 (column 5 or 1).
_ERASE_LINKS = {
    "ED0": (b"\x1b[2;5H\x1b[0J", [False, True, False, False]),
    "ED0-at-column-0": (b"\x1b[2;1H\x1b[0J", [False, False, False, False]),
    "ED1": (b"\x1b[2;5H\x1b[1J", [False, False, True, False]),
    "ED2": (b"\x1b[2;5H\x1b[2J", [False, False, False, False]),
    "EL0": (b"\x1b[2;5H\x1b[0K", [False, True, False, False]),
    "EL0-at-column-0": (b"\x1b[2;1H\x1b[0K", [False, False, False, False]),
    "EL1": (b"\x1b[2;5H\x1b[1K", [False, False, True, False]),
    "EL2": (b"\x1b[2;5H\x1b[2K", [False, False, False, False]),
}


@pytest.mark.parametrize("data, links", _ERASE_LINKS.values(), ids=_ERASE_LINKS.keys())
def test_erases_leave_exactly_the_valid_links(data, links):
    model = TerminalModel(40, 4)
    model.feed((LONG + LONG[:30]).encode())
    assert _links(model) == [False, True, True, False]
    model.feed(data)
    assert _links(model) == links


def _conpty_model():
    model = TerminalModel(40, 3)
    model.pull_history_on_grow = False
    return model


def test_conpty_wraps_split_across_feeds_still_rejoin():
    data = b"x\r\n" + LONG.encode() + b"\r\n$ \r\n\r\n\r\n"  # LONG ends up in the scrollback
    for cut in range(len(data) + 1):
        model = _conpty_model()
        model.feed(data[:cut])
        model.feed(data[cut:])
        model.resize(80, 3)
        assert _rows(model)[:2] == ["x", LONG], cut


def test_a_conpty_repaint_that_starts_mid_row_does_not_join_the_scrollback():
    model = _conpty_model()
    model.feed(b"head\r\n" + LONG.encode() + b"\r\n\r\n")  # LONG[40:] on row 0
    model.feed(b"\x1b[H\x1b[2Cindented\r\n\r\n\r\n")
    model.resize(80, 3)
    assert _rows(model)[:3] == ["head", LONG[:40], LONG[40:42] + "indented" + LONG[50:]]


def test_a_conpty_overwrite_without_erase_unlinks_the_row_below():
    model = _conpty_model()
    model.feed(LONG.encode() + b"\r\n" + LONG.encode())
    model.feed(b"\x1b[H\r\nnew_row2" + b"\r\n" * 4)  # all rows into the scrollback
    model.resize(80, 3)
    # Row 0 was not repainted, so it still continues the scrollback.
    assert _rows(model)[:3] == [LONG, "new_row2" + LONG[8:40], LONG[40:]]


@pytest.mark.parametrize("sizes", [((40, 2),), ((20, 2), (40, 5)), ((20, 5), (40, 5))])
def test_main_screen_rows_survive_alternate_screen_resizes(sizes):
    model = TerminalModel(40, 5)
    model.feed(b"1\r\n2\r\n3\r\n" + LONG.encode())
    model.feed(b"\x1b[?1049h\x1b[Heditor")
    for columns, lines in sizes:
        model.resize(columns, lines)
    model.feed(b"\x1b[?1049lX")
    assert _rows(model) == ["1", "2", "3", LONG[:40], LONG[40:] + "X"]


def test_scroll_region_line_edits_with_a_top_margin():
    model = TerminalModel(40, 5)
    model.feed(b"head\r\n" + LONG.encode() + b"\x1b[2;4r\x1b[3;1H\x1b[Lnew")
    model.resize(80, 5)
    assert _rows(model) == ["head", LONG[:40], "new", LONG[40:]]
    model = TerminalModel(40, 5)
    model.feed(b"head\r\nmid\r\n" + LONG.encode() + b"\x1b[2;4r\x1b[2;1H\x1b[M")
    model.resize(80, 5)
    assert _rows(model) == ["head", LONG]


# -- the bundled conpty.dll (passthrough) ------------------------------------
#
# Recorded from Microsoft.Windows.Console.ConPTY 1.25 on Windows 11 with a fake
# Cisco CLI, wsl.exe running bash, and cmd.exe: it relays a VT client's bytes
# unchanged and sends nothing on a resize. When the client asks for the cursor
# it sends ESC[6n and, after the reply, a CUP to the reported position.

CONPTY_START = b"\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001h"


def _passthrough(columns, lines):
    replies = []
    model = TerminalModel(columns, lines, on_reply=replies.append)
    model.pull_history_on_grow = False
    model.passthrough = True
    return model, replies


def _cursor(model):
    return model.screen.cursor.y, model.screen.cursor.x


def test_passthrough_narrowing_rewraps_the_screen_instead_of_cutting_it():
    model, _ = _passthrough(80, 6)
    model.feed(CONPTY_START + b"banner\r\n" + LONG.encode() + b"\r\nshort\r\nSW1#")
    model.resize(40, 6)
    assert _rows(model) == ["banner", LONG[:40], LONG[40:], "short", "SW1#"]
    assert _cursor(model) == (4, 4)


def test_passthrough_widening_joins_rows_wrapped_on_screen():
    model, _ = _passthrough(40, 6)
    model.feed(b"banner\r\n" + LONG.encode() + b"\r\nSW1#")
    model.resize(80, 6)
    assert _rows(model) == ["banner", LONG, "SW1#"]
    assert _cursor(model) == (2, 4)


def test_passthrough_cursor_sync_after_a_resize_moves_nothing():
    model, replies = _passthrough(40, 6)
    model.feed(b"x\r\n" + LONG.encode() + b"\r\n$")
    model.resize(80, 6)
    model.feed(b"\x1b[6n\r\x1b[K\r$")  # ConPTY's query, then bash's SIGWINCH redraw
    assert replies[-1] == b"\x1b[3;2R"
    model.feed(b"\x1b[3;2H")  # ConPTY moves to the position it was told
    model.feed(b"ls\r\nfile\r\n$")
    assert _rows(model) == ["x", LONG, "$ls", "file", "$"]


def test_passthrough_output_after_a_reflow_scrolls_without_duplicates():
    model, _ = _passthrough(40, 4)
    model.feed(LONG.encode() + b"\r\n$")
    model.resize(20, 4)
    assert _rows(model) == [LONG[:20], LONG[20:40], LONG[40:], "$"]
    lines = [b"line%d" % number for number in range(6)]
    model.feed(b"\r\n" + b"\r\n".join(lines) + b"\r\n$")
    expected_tail = [line.decode() for line in lines] + ["$"]
    assert _rows(model) == [LONG[:20], LONG[20:40], LONG[40:], "$"] + expected_tail
    model.resize(60, 4)
    assert _rows(model) == [LONG, "$"] + expected_tail


def test_typing_at_the_prompt_after_a_passthrough_reflow():
    model, _ = _passthrough(40, 5)
    model.feed(LONG.encode() + b"\r\nSW1#")
    model.resize(80, 5)
    model.feed(b"sh ruX")  # the remote echoes
    model.resize(30, 5)
    model.feed(b"\x08 \x08n")
    assert _rows(model) == [LONG[:30], LONG[30:], "SW1#sh run"]
    assert _cursor(model) == (2, 10)


def test_passthrough_widening_keeps_history_where_conhost_keeps_it():
    # cmd.exe writes through console APIs, so conhost positions its output in
    # its own buffer, which it reflows without pulling history back down.
    title = b"\x1b]0;C:\\Windows\\System32\\cmd.exe\x1b\\"
    a90 = b"A" * 90
    model, replies = _passthrough(100, 8)
    model.feed(CONPTY_START + b"\x1b[?7l\x1b[?7h" + title + b"\r\n>")
    model.feed(b"\x1b[2;2Hecho " + a90 + b"\x1b[2;97H\x1b[2;97H\r\n" + title + a90 + b"\r\n" + title + b"\r\n>")
    model.resize(40, 8)
    model.feed(b"\x1b[8;2Hecho x\x1b[8;8H\x1b[8;8H\r\n" + title + b"x\r\n" + title + b"\r\n>\x1b[6n")
    assert replies[-1] == b"\x1b[8;2R"
    model.feed(b"\x1b[8;2H")
    model.resize(100, 8)
    model.feed(b"\x1b[6;2Hecho y\x1b[6;8H\x1b[6;8H\r\n" + title + b"y\r\n" + title + b"\r\n>\x1b[6n")
    assert replies[-1] == b"\x1b[8;2R"
    assert _rows(model) == ["", ">echo " + "A" * 90, "A" * 90, "", ">echo x", "x", "", ">echo y", "y", "", ">"]


def test_passthrough_main_screen_is_reflowed_after_the_alternate_screen():
    model, _ = _passthrough(80, 6)
    model.feed(b"banner\r\n" + LONG.encode() + b"\r\nSW1#")
    model.feed(b"\x1b[?1049h\x1b[H\x1b[2JALT TOP\x1b[5;3Hmid")
    model.resize(40, 6)
    assert model.row_text(model.history_length).rstrip() == "ALT TOP"  # not reflowed
    model.feed(b"\x1b[?1049l")
    assert _rows(model) == ["banner", LONG[:40], LONG[40:], "SW1#"]
    assert _cursor(model) == (3, 4)


def test_inbox_conhost_still_keeps_its_screen():
    model = TerminalModel(80, 6)
    model.pull_history_on_grow = False  # passthrough stays off: conhost repaints
    model.feed(LONG.encode() + b"\r\n$")
    model.resize(40, 6)
    assert _rows(model) == [LONG[:40], "$"]


@pytest.mark.parametrize("passthrough", [False, True])
def test_rows_the_main_screen_pushes_on_return_count_as_scrollback_pushes(passthrough):
    model = TerminalModel(40, 5)
    model.pull_history_on_grow = not passthrough
    model.passthrough = passthrough
    for number in range(8):
        model.feed(b"h%d\r\n" % number)
    model.feed(LONG.encode() + b"\r\n" + LONG.encode() + b"\r\n$ vim")
    model.feed(b"\x1b[?1049h\x1b[Heditor")
    model.resize(20, 5)
    history, pushes = model.history_length, model.screen.scrollback_pushes
    model.feed(b"\x1b[?1049l")
    assert model.history_length > history
    assert model.screen.scrollback_pushes - pushes == model.history_length - history


@pytest.mark.parametrize("passthrough", [False, True])
@pytest.mark.parametrize("filled", [50, 100, 300])  # below, at and past the limit of 100
@pytest.mark.parametrize("size", [(20, 5), (40, 3), (20, 3)])
def test_alt_exit_pushes_are_counted_even_when_the_scrollback_is_full(passthrough, filled, size):
    model = TerminalModel(40, 5, scrollback_limit=100)
    model.pull_history_on_grow = not passthrough
    model.passthrough = passthrough
    for number in range(filled):
        model.feed(b"h%05d\r\n" % number)
    model.feed(LONG.encode() + b"\r\n" + LONG.encode() + b"\r\n$ vim")
    model.feed(b"\x1b[?1049h\x1b[Heditor")
    model.resize(*size)
    offset, pushes = 3, model.screen.scrollback_pushes
    text = model.row_text(model.viewport_top(offset))
    model.feed(b"\x1b[?1049l")
    # What PyteTerminalView.feed does with a scrolled-back view.
    offset = min(model.history_length, offset + model.screen.scrollback_pushes - pushes)
    assert model.screen.scrollback_pushes > pushes
    assert model.row_text(model.viewport_top(offset)) == text
