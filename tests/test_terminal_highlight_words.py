"""SecureCRT-style rules match whole words: no colour run starts or ends mid-word."""

import colorsys
import re

import pytest

from sshpilot.terminal_highlight import Highlighter, apply_config, profile_rules
from sshpilot.terminal_highlight_custom import CustomProfile, EditableRule
from sshpilot.terminal_highlight_securecrt import THEME_COLOURS, compile_rules, line_owners

LINES = [
    "aaa group server radius ISE_RADIUS",
    "ip dhcp excluded-address 10.250.2.1 10.250.2.69",
    "ip dhcp pool PNP_POOL",
    "ip vrf OT_SRV1",
    "hostname SW-X",
    "ip name-server 10.250.2.1",
    "clock timezone WEST 1",
    "system mtu routing 1500",
    "vtp mode transparent",
    "aaa authorization exec default group ISE_RADIUS local",
    "ip dhcp snooping",
    "spanning-tree mode rapid-pvst",
]
WORD = re.compile(r"[0-9A-Za-z_]")


@pytest.fixture(autouse=True)
def _no_user_rules(tmp_path, monkeypatch):
    import sshpilot.terminal_highlight as highlight
    import sshpilot.terminal_highlight_securecrt as securecrt

    monkeypatch.setattr(securecrt, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(highlight, "_COMPILED", {})


def mid_word_edges(line, owners):
    """Run edges that split a word whose both neighbouring characters are word characters."""
    bad = []
    for i in range(1, len(line)):
        if owners[i] != owners[i - 1] and WORD.match(line[i - 1]) and WORD.match(line[i]):
            bad.append((i, line[max(0, i - 3):i + 3]))
    return bad


@pytest.mark.parametrize("line", LINES)
def test_no_run_edge_inside_a_word(line):
    rules = compile_rules("securecrt")
    assert mid_word_edges(line, line_owners(line, rules)) == []


def runs(data):
    """(colour-or-None, text) runs of SGR-injected output."""
    out, colour = [], None
    for part in re.split(rb"(\x1b\[[0-9;]*m)", data):
        if part.startswith(b"\x1b["):
            colour = None if part == b"\x1b[39m" else part
        elif part:
            out.append((colour, part.decode()))
    return out


@pytest.mark.parametrize("line", LINES)
@pytest.mark.parametrize("split", [1, 5, 11])
def test_chunked_feed_has_no_mid_word_runs(line, split):
    data = (line + "\r\n").encode()
    h = Highlighter("cisco-securecrt")
    out = b"".join(h.feed(data[i:i + split]) for i in range(0, len(data), split)) + h.flush()
    text = "".join(t for _c, t in runs(out))
    owners, pos = [], 0
    for colour, chunk in runs(out):
        owners += [(colour, pos)] * len(chunk)
        pos += len(chunk)
    # a new run starts mid-word only if the colour changes between two word characters
    for i in range(1, len(text)):
        if owners[i][0] != owners[i - 1][0] and WORD.match(text[i - 1]) and WORD.match(text[i]):
            pytest.fail(f"mid-word colour change at {i} in {text!r}")


def test_whole_words_keep_their_colour():
    rules = compile_rules("securecrt")
    line = "interface vlan 10 shutdown"
    owners = line_owners(line, rules)
    assert all(owners[i] is not None for i in range(len("interface")))  # misc
    assert all(owners[i] is not None for i in range(line.index("shutdown"), len(line)))


# --- dark background and bold ---------------------------------------------

def test_securecrt_dark_is_readable_and_keeps_hue():
    light, dark = THEME_COLOURS["securecrt"], THEME_COLOURS["securecrt-dark"]
    assert light.keys() == dark.keys()
    for role, original in light.items():
        if original is None:
            continue
        h0, l0, s0 = colorsys.rgb_to_hls(*(c / 255 for c in original))
        h1, l1, s1 = colorsys.rgb_to_hls(*(c / 255 for c in dark[role]))
        assert l1 >= 0.55, role
        if s0 and l0 < 0.95:
            assert abs(h0 - h1) < 0.02 or abs(abs(h0 - h1) - 1) < 0.02, role


class _Config:
    def __init__(self, background, **settings):
        self.background, self.settings = background, settings

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def get_terminal_profile(self):
        return {"background": self.background}


def _applied(background, **settings):
    h = Highlighter(enabled=False)
    settings = {"terminal.highlight_enabled": True, "terminal.highlight_profile": "cisco-securecrt",
                **settings}
    apply_config(h, _Config(background, **settings))
    return h


def test_short_hex_background_is_understood():
    assert _applied("#000").theme == "securecrt-dark"
    assert _applied("#fff").theme == "securecrt"


def test_dark_background_picks_dark_securecrt_theme_only_for_the_default_theme():
    assert _applied("#000000").theme == "securecrt-dark"
    assert _applied("#FFFFFF").theme == "securecrt"
    assert _applied("#000000", **{"terminal.highlight_theme": "light"}).theme == "light"


def feed(h, text):
    return h.feed(text) + h.flush()


def test_bold_option_bolds_every_highlighted_run_for_securecrt_profile():
    on = feed(_applied("#000000"), b"aaa group\r\n")
    assert on.startswith(b"\x1b[1;38;2;")
    off = feed(_applied("#000000", **{"terminal.highlight_bold": False}), b"aaa group\r\n")
    assert b"\x1b[1;" not in off


def test_forced_bold_keeps_remote_bold_and_ends_with_normal_intensity():
    h = _applied("#000000")
    out = feed(h, b"\x1b[1maaa group\x1b[0m\r\n")
    assert b"\x1b[1;38" not in out  # the remote's bold is not restarted ...
    assert b"\x1b[22m" not in out  # ... nor cancelled
    out = feed(h, b"aaa\r\n")
    assert out.endswith(b"\x1b[22;39m\r\n")


def test_bold_option_does_not_touch_legacy_profiles():
    h = Highlighter("generic", bold=True)
    assert b"\x1b[1;" not in feed(h, b"10.0.0.1\r\n")


# --- whole words are for the built-in rules only ----------------------------

def _owned(profile, line):
    h = Highlighter(profile)
    out = h.feed((line + "\r\n").encode()) + h.flush()
    return [(c, t) for c, t in runs(out) if c]


def test_duplicate_of_securecrt_profile_equals_the_original():
    duplicate = CustomProfile("Copy", tuple(profile_rules("cisco-securecrt")))
    for line in LINES + ["SW#show run", "interface GigabitEthernet0/1 is up, line protocol is up"]:
        assert _owned(duplicate, line) == _owned("cisco-securecrt", line), line


def test_user_rule_matches_exactly_as_written():
    profile = CustomProfile("Mine", (EditableRule("err", "bad"),))
    coloured = [t for _c, t in _owned(profile, "an error occurred")]
    assert coloured == ["err"]  # inside "error": not a whole word, still matched
    assert _owned("cisco-securecrt", "an error occurred") == []
