"""Colour network-device terminal output (IPs, MACs, VLANs, interfaces, ...).

``Highlighter.feed`` rewrites a display byte stream, adding zero-width SGR
escapes around recognised tokens. Legacy profiles use palette indices; the
SecureCRT profile supports truecolour role themes and a terminal-palette
option. It restores the default foreground with ``ESC[39m`` so visible text is
never changed: stripping injected SGR from the output gives back the input.

Streaming rules:

* Escape sequences split across chunks are held until complete. Control
  strings (OSC, DCS, SOS, PM, APC) are passed through by state, not buffered;
  OSC ends at BEL or ST, the others at ST only. C1 controls are recognised in
  their UTF-8 form (``C2 9B`` ...), never as raw 8-bit bytes, which are UTF-8
  continuation bytes.
* Legacy profiles never hold text back: a token split across chunks is
  coloured from the chunk boundary on (the already-shown part stays as it was).
* The SecureCRT profile holds each line until its CR, LF or backspace, then
  colours it whole, so splits never change the result. An unfinished line
  (prompt, echo, ``--More--``) is shown by :meth:`Highlighter.flush_due`
  after ``FLUSH_DELAY`` idle seconds, or at once when it looks like a prompt;
  its rest is coloured later with the shown part as context. A line longer
  than ``LINE_MAX`` bytes is cut, and one with a run of more than
  ``DIGIT_RUN_MAX`` digits is matched in pieces, to bound the cost of the
  unmodified source regexes.
* Text is left alone while the remote has set a foreground/background colour,
  reverse or conceal, and while any alternate-screen mode (47/1047/1049) is on.
* A disabled highlighter returns its input unchanged but keeps tracking
  terminal state, so enabling it mid-session needs no resync.

Custom profiles (:mod:`terminal_highlight_custom`) run on the SecureCRT line
engine with their own rules; custom themes recolour it.

This module is pure Python and does not import ``gi``.
"""

from __future__ import annotations

import ipaddress
import itertools
import re
import time
from gettext import gettext as _
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple, Union

from .terminal_highlight_custom import (
    CustomProfile,
    CustomTheme,
    EditableRule,
    Store,
    MAX_DESCRIPTION_LENGTH,
    check_colour,
    custom_name,
    hex_to_rgb,
    load_store,
    theme_colours,
)
from .terminal_highlight_securecrt import (
    SECURECRT_RULES,
    CompiledRule,
    Colour,
    THEMES as THEMES,
    _safe_custom_pattern,
    compile_rules,
    line_owners,
    load_rules,
    normalize_theme,
    style_bytes,
    whole_word,
)

PROFILES = ("generic", "cisco-ios", "cisco-securecrt", "junos", "huawei-vrp")

_RED, _GREEN, _YELLOW, _BLUE, _MAGENTA, _CYAN = 31, 32, 33, 34, 35, 36

_MAX_VLAN = 4094
_MAX_MPLS_LABEL = 1048575

# Called with bytes by the legacy engine and with str by the line engine.
Validator = Callable[[bytes, "re.Match[bytes]"], bool]


class Rule(NamedTuple):
    name: str
    pattern: str
    colour: int
    validate: Optional[Validator] = None


def _bytes(text) -> bytes:
    return text if isinstance(text, bytes) else text.encode("utf-8", "surrogateescape")


def _numbers_at_most(limit: int, low: int = 0) -> Validator:
    def check(_full: bytes, m: "re.Match[bytes]") -> bool:
        return all(low <= int(n) <= limit for n in re.findall(rb"\d+", _bytes(m.group())))

    return check


def _valid_ipv6(_full: bytes, m: "re.Match[bytes]") -> bool:
    address, _sep, prefix = _bytes(m.group()).partition(b"/")
    if prefix and int(prefix) > 128:
        return False
    if not re.search(rb"[0-9A-Fa-f]", address):
        return False
    try:
        ipaddress.IPv6Address(address.decode("ascii"))
    except ValueError:
        return False
    return True


_VERSION_BEFORE = re.compile(rb"(?i)(?:version|ver|release|build)[ :=]+\Z")


def _not_a_version(full: bytes, m: "re.Match[bytes]") -> bool:
    return _VERSION_BEFORE.search(_bytes(full[max(0, m.start() - 16):m.start()])) is None


_OCT = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_BEFORE = r"(?<![0-9A-Za-z])"
_WORD = r"(?<![\w/-])"
_NOT_WORD = r"(?![\w/-])"

_MAC = [
    Rule(
        "mac",
        r"(?<![0-9A-Za-z:])[0-9A-Fa-f]{2}(?P<ms>[:-])(?:[0-9A-Fa-f]{2}(?P=ms)){4}"
        r"[0-9A-Fa-f]{2}(?![0-9A-Za-z]|:[0-9A-Fa-f])",
        _MAGENTA,
    ),
    Rule(
        "mac_dotted",
        r"(?<![0-9A-Za-z:])[0-9A-Fa-f]{4}(?P<md>[.-])[0-9A-Fa-f]{4}(?P=md)[0-9A-Fa-f]{4}"
        r"(?![0-9A-Za-z])",
        _MAGENTA,
    ),
]
_IP = [
    Rule(
        "ipv6",
        r"(?<![0-9A-Za-z:.])(?=[0-9A-Fa-f]{0,4}:)[0-9A-Fa-f:]{2,39}(?:\.\d{1,3}){0,3}"
        r"(?:/\d{1,3})?(?![0-9A-Za-z:])",
        _CYAN,
        _valid_ipv6,
    ),
    Rule(
        "ipv4",
        rf"(?<![0-9A-Za-z.])(?:{_OCT}\.){{3}}{_OCT}(?:/(?:3[0-2]|[12]?\d))?"
        r"(?![0-9A-Za-z]|\.[0-9])",
        _CYAN,
        _not_a_version,
    ),
]
_VLAN = Rule(
    "vlan",
    rf"{_BEFORE}(?:[Vv][Ll][Aa][Nn](?:-id)?[ \t]+\d{{1,4}}(?:[,-]\d{{1,4}})*|VLAN\d{{4}})"
    r"(?![0-9A-Za-z])",
    _BLUE,
    _numbers_at_most(_MAX_VLAN, low=1),
)
_MPLS = Rule(
    "mpls",
    rf"{_BEFORE}[Ll]abel:?[ \t]+\d{{1,7}}(?![0-9.])|{_WORD}(?:implicit|explicit)-null{_NOT_WORD}",
    _BLUE,
    _numbers_at_most(_MAX_MPLS_LABEL),
)

# Interfaces: long forms every vendor spells out; profiles add abbreviations.
_IFACE = Rule(
    "iface",
    rf"{_BEFORE}(?:(?:Gigabit|TenGigabit|TwentyFiveGig|FortyGigabit|HundredGig|Fast|XGigabit)"
    r"Ethernet|Ethernet|Port-channel|Loopback|LoopBack|Tunnel|Serial|Vlanif|Vlan|Eth-Trunk"
    r"|Management|MEth|TwentyFiveGigE|HundredGigE|TenGigE)\d+(?:/\d+){0,4}(?:\.\d+)?"
    r"(?![0-9A-Za-z])"
    rf"|{_BEFORE}(?:(?:ge|xe|et|fe)-\d+/\d+/\d+(?::\d+)?(?:\.\d+)?|ae\d+(?:\.\d+)?)"
    r"(?![0-9A-Za-z])",
    _YELLOW,
)
_EXTRA_IFACE: Dict[str, str] = {
    "cisco-ios": r"(?:Gi|Te|Fa|Eth|Et|Po|Lo|Tu|Hu|Fo|Twe|Vl)\d+(?:/\d+){0,4}(?:\.\d+)?",
    "junos": (
        r"(?:(?:so|vcp|lt|pp|ip|gr|st)-\d+/\d+/\d+(?::\d+)?(?:\.\d+)?"
        r"|(?:lo|em|fxp|me|reth)\d+(?:\.\d+)?|(?:irb|vlan)\.\d+)"
    ),
    "huawei-vrp": r"(?:100GE|40GE|25GE|10GE|XGE|GE|NULL)\d+(?:/\d+){0,3}(?:\.\d+)?",
}

# Link state: generic rules only fire in an obvious status context ("is up",
# "up/down"); vendors add the bare column words their tables use.
_STATE_CONTEXT = r"(?:(?<=\bis )|(?<=\bare )|(?<=: )|(?<=\bstate )|(?<=\bstatus ))"
_STATE = [
    Rule("state_ok", rf"{_STATE_CONTEXT}[Uu]p{_NOT_WORD}(?! to\b)|{_WORD}up/up{_NOT_WORD}", _GREEN),
    Rule(
        "state_bad",
        rf"{_STATE_CONTEXT}(?:administratively )?[Dd]own{_NOT_WORD}"
        rf"|{_WORD}(?:up/down|down/up|down/down){_NOT_WORD}"
        rf"|{_WORD}(?:err-disabled|notconnect){_NOT_WORD}",
        _RED,
    ),
]
# profile -> (good words, bad words, prefix on bad words, suffix on both)
_EXTRA_STATE = {
    "cisco-ios": (r"up|connected|Established", r"down|Idle", "", ""),
    "junos": (r"up|Up|Established", r"down|Down|Idle", "", ""),
    "huawei-vrp": (r"up|UP", r"down|DOWN", r"[*^]?", r"(?:\([a-z]+\))?"),
}
_ERROR = Rule(
    "error",
    r"(?<![A-Za-z0-9_])(?:%[ ]?(?:Invalid|Incomplete|Ambiguous|Unknown|Bad)|syntax error"
    r"|unknown command|missing argument|[Ee]rror:)[^\r\n]*",
    _RED,
)


def _rules(profile: str) -> List[Rule]:
    rules = [*_MAC, *_IP, _IFACE]
    if profile in _EXTRA_IFACE:
        rules.append(Rule("iface_x", rf"{_BEFORE}{_EXTRA_IFACE[profile]}(?![0-9A-Za-z])", _YELLOW))
    rules += [_VLAN, _MPLS]
    if profile == "junos":
        rules.append(Rule("mpls_x", rf"{_BEFORE}(?:push|swap)[ \t]+\d{{2,7}}(?![0-9.])", _BLUE,
                          _numbers_at_most(_MAX_MPLS_LABEL)))
    if profile in _EXTRA_STATE:
        good, bad, prefix, suffix = _EXTRA_STATE[profile]
        rules.append(Rule("state_ok_x", rf"{_WORD}(?:{good}){suffix}{_NOT_WORD}", _GREEN))
        rules.append(Rule("state_bad_x", rf"{_WORD}{prefix}(?:{bad}){suffix}{_NOT_WORD}", _RED))
    rules += _STATE
    rules.append(_ERROR)
    return rules


class _Compiled(NamedTuple):
    regex: Optional["re.Pattern[bytes]"]
    colours: Dict[str, bytes]
    validators: Dict[str, Validator]
    priority_rules: Tuple[CompiledRule, ...]


# Duplicating a legacy profile gives each rule the role closest to its colour.
_LEGACY_ROLES = {
    "mac": "mac", "mac_dotted": "mac", "ipv6": "ip", "ipv4": "ip",
    "iface": "interface", "iface_x": "interface", "vlan": "misc", "mpls": "misc",
    "mpls_x": "misc", "state_ok": "good", "state_ok_x": "good", "state_bad": "bad",
    "state_bad_x": "bad", "error": "bad",
}
# Built-in patterns run verbatim (with their checks) when found in a custom
# profile; any other pattern must pass the custom-rule safety guard.
_TRUSTED: Dict[str, Optional[Validator]] = {
    **{rule.source: None for rule in SECURECRT_RULES},
    **{rule.pattern: rule.validate for profile in PROFILES if profile != "cisco-securecrt"
       for rule in _rules(profile)},
}

_SECURECRT_SOURCES = frozenset(rule.source for rule in SECURECRT_RULES)

ProfileRef = Union[str, CustomProfile]
ThemeRef = Union[str, CustomTheme]


def profile_rules(profile: str) -> List[EditableRule]:
    """Editable copy of a built-in profile: what *Duplicate* starts from."""
    if profile == "cisco-securecrt":
        return [EditableRule(rule.source, rule.role, rule.bold, match_case=rule.match_case)
                for rule in (*SECURECRT_RULES, *load_rules()) if rule.role != "default"]
    # Source order reproduces the legacy result: its only overlaps are tokens
    # inside the error message, which runs to the line end and comes last.
    return [EditableRule(rule.pattern, _LEGACY_ROLES[rule.name], match_case=True,
                         description=rule.name)
            for rule in _rules(normalize_profile(profile))]


def check_rule(rule: EditableRule) -> Tuple[str, Optional[Validator]]:
    """The pattern to compile and its check; ValueError with a message to show."""
    check_colour(rule.colour)
    if len(rule.description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(_("The description is longer than {count} characters.").format(
            count=MAX_DESCRIPTION_LENGTH))
    if rule.pattern in _TRUSTED:
        return rule.pattern, _TRUSTED[rule.pattern]
    if not rule.pattern:
        raise ValueError(_("Enter a pattern."))
    try:
        return _safe_custom_pattern(rule.pattern), None
    except UnicodeEncodeError:
        raise ValueError(_("Only ASCII characters are allowed in a pattern.")) from None
    except re.error as exc:
        raise ValueError(_("Invalid pattern: {error}").format(error=exc)) from None


def _numbered(number: int, rule: EditableRule) -> Tuple[str, Optional[Validator]]:
    try:
        return check_rule(rule)
    except ValueError as exc:
        raise ValueError(_("Rule {number}: {error}").format(number=number, error=exc)) from None


def check_profile(profile: CustomProfile) -> None:
    """Raise ValueError naming the first rule a profile cannot use."""
    for number, rule in enumerate(profile.rules, 1):
        _numbered(number, rule)


def _compile_custom(profile: CustomProfile, theme: ThemeRef) -> Tuple[CompiledRule, ...]:
    """Compile the enabled rules; ValueError names the first unusable one.

    Disabled rules never run, so only saving (check_profile) checks them.
    """
    colours = theme_colours(theme)
    compiled = []
    for number, rule in enumerate(profile.rules, 1):
        if rule.enabled:
            pattern, validate = _numbered(number, rule)
            opening, closing = style_bytes(rule.colour, colours, rule.bold)
            flags = 0 if rule.match_case else re.IGNORECASE
            if rule.pattern in _SECURECRT_SOURCES:  # a copy of a built-in; user rules run as written
                pattern = whole_word(pattern)
            compiled.append(CompiledRule(re.compile(pattern, flags), opening, closing,
                                         rule.bold, validate))
    return tuple(compiled)


Run = Tuple[str, Colour, bool]


def preview_lines(profile: CustomProfile, theme: ThemeRef,
                  lines: List[str]) -> List[List[Run]]:
    """(text, colour, bold) runs of each line as the line engine paints it.

    The colour is ``None`` for unstyled text, else an RGB tuple or a palette
    SGR code (``terminal-palette`` theme). The profile is compiled once.
    """
    enabled = [rule for rule in profile.rules if rule.enabled]
    colours = theme_colours(theme)
    compiled = _compile_custom(profile, theme)
    result = []
    for line in lines:
        line = line[:LINE_MAX]
        runs: List[Run] = []
        pos = 0
        for owner, group in itertools.groupby(line_owners(line, compiled)):
            end = pos + sum(1 for _owner in group)
            if owner is None:
                runs.append((line[pos:end], None, False))
            else:
                rule = enabled[owner]
                colour = (hex_to_rgb(rule.colour) if rule.colour.startswith("#")
                          else colours.get(rule.colour))
                runs.append((line[pos:end], colour, rule.bold))
            pos = end
        result.append(runs)
    return result


def preview_runs(profile: CustomProfile, theme: ThemeRef, line: str) -> List[Run]:
    return preview_lines(profile, theme, [line])[0]


def _compile(profile: ProfileRef, theme: ThemeRef, bold: bool = False) -> _Compiled:
    if isinstance(profile, CustomProfile):
        return _Compiled(None, {}, {}, _compile_custom(profile, theme))
    if profile == "cisco-securecrt":
        return _Compiled(None, {}, {}, compile_rules(theme_colours(theme), bold))
    rules = _rules(profile)
    pattern = "|".join(f"(?P<{r.name}>{r.pattern})" for r in rules)
    return _Compiled(
        re.compile(pattern.encode("ascii")),
        {r.name: b"\x1b[%dm" % r.colour for r in rules},
        {r.name: r.validate for r in rules if r.validate},
        (),
    )


# Only the SecureCRT and custom profiles have themes; legacy profiles share one
# entry. Custom profiles and themes are keyed by content, so an edit compiles anew.
_COMPILED: Dict[Tuple[ProfileRef, ThemeRef, bool], _Compiled] = {}
_COMPILED_MAX = 64

_NEXT_RE = re.compile(rb"\x1b|\xc2(?:[\x80-\x9f]|\Z)")
_CSI_RE = re.compile(rb"(?:\x1b\[|\xc2\x9b)([0-?]*)([ -/]*)([@-~])")
_STRING_INTRO_RE = re.compile(rb"\x1b([\]PX^_])|\xc2([\x90\x98\x9d\x9e\x9f])")
# Short escapes: anything but the CSI/string introducers [ ] P X ^ _ (ESC) or
# their C1 forms.
_ESC_RE = re.compile(rb"\x1b[ -/]*[0-?@-OQ-WYZ\\`-~]|\xc2[\x80-\x8f\x91-\x97\x99\x9a\x9c]")
_PARTIAL_ESC_RE = re.compile(rb"(?:\x1b(?:\[[0-?]*[ -/]*|[ -/]*)|\xc2(?:\x9b[0-?]*[ -/]*)?)\Z")
# A string ends at ST (and BEL for OSC), is cancelled by CAN/SUB, and is cut
# short by a new ESC (matched without consuming it by the last alternative).
_OSC_END_RE = re.compile(rb"\x07|\x1b\\|\xc2\x9c|[\x18\x1a]|\x1b(?!\\|\Z)")
_ST_END_RE = re.compile(rb"\x1b\\|\xc2\x9c|[\x18\x1a]|\x1b(?!\\|\Z)")
_TAIL_RE = re.compile(rb"[^\x00-\x20\x7f]*\Z")  # trailing run of non-break bytes
_ALT_SCREEN_MODES = {b"47", b"1047", b"1049"}
_CARRY_MAX = 64
FLUSH_DELAY = 0.05  # seconds an unfinished SecureCRT line may wait
# Keeps any real Cisco line whole; the quadratic ``.*X`` source rules cost
# about 0.25 s on a hostile 4096-byte line.
LINE_MAX = 4096
# ``\d*\d*\.\d*\(`` is cubic in a digit run (21 s for 4096 digits): a line
# with a longer run is matched in pieces of this size (~7 ms each).
DIGIT_RUN_MAX = 256
_LONG_DIGIT_RUN = re.compile(rb"[0-9]{%d}" % (DIGIT_RUN_MAX + 1))
_LINE_BREAK = re.compile(rb"[\r\n\x08]")
_PROMPT = re.compile(r"\w[^>]*#|\w[^#]*>")
_CTX_MAX = 16  # bytes of look-behind kept for _not_a_version across chunks


def normalize_profile(value: object) -> str:
    return value if isinstance(value, str) and value in PROFILES else "generic"


class Highlighter:
    """Stateful stream rewriter; one instance per terminal."""

    def __init__(self, profile: ProfileRef = "generic", enabled: bool = True,
                 theme: ThemeRef = "securecrt", bold: bool = False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.enabled = enabled
        self._clock = clock
        self._fed_at = 0.0
        self.warning: Optional[str] = None  # set by apply_config on a fallback
        self.theme = theme if isinstance(theme, CustomTheme) else normalize_theme(theme)
        self.bold = bold
        self.set_profile(profile)
        self.reset()

    def set_profile(self, profile: ProfileRef, theme: Optional[ThemeRef] = None,
                    bold: Optional[bool] = None) -> None:
        """Switch rules; nothing changes unless compiling succeeded.

        Raises ValueError for a custom profile with an unusable rule.
        """
        if not isinstance(profile, CustomProfile):
            profile = normalize_profile(profile)
        if theme is None:
            theme = self.theme
        elif not isinstance(theme, CustomTheme):
            theme = normalize_theme(theme)
        if bold is None:
            bold = self.bold
        themed = isinstance(profile, CustomProfile) or profile == "cisco-securecrt"
        # Forced bold is a SecureCRT-profile option; custom rules carry their own.
        key = profile, theme if themed else "", bold and profile == "cisco-securecrt"
        if key not in _COMPILED:
            compiled = _compile(*key)
            if len(_COMPILED) >= _COMPILED_MAX:
                _COMPILED.clear()
            _COMPILED[key] = compiled
        self.profile, self.theme, self.bold, self._compiled = profile, theme, bold, _COMPILED[key]

    def configure(self, enabled: bool, profile: ProfileRef,
                  theme: Optional[ThemeRef] = None, bold: Optional[bool] = None) -> bytes:
        """Apply settings without losing stream state.

        Returns bytes that were held back and must now be displayed unchanged
        (non-empty only when switching off mid-escape, or when a change drops
        a held SecureCRT line).
        """
        old = self.enabled, self.profile, self.theme, self.bold
        self.set_profile(profile, theme, bold)
        flushed = b""
        if old != (enabled, self.profile, self.theme, self.bold):
            # A held line goes out as it is: its rules or the switch are gone.
            flushed, self._segs, self._prefix, self._line_len = (
                b"".join(data for data, _, _ in self._segs), [], b"", 0)
        if self.enabled != enabled:
            self._carry = self._ctx = b""
            # The held bytes are on screen after this call either way; keep them
            # for parsing (the rest of the sequence may follow) but never re-emit.
            flushed += self._hold[self._shown:] if self.enabled else b""
            self._shown = len(self._hold)
        self.enabled = enabled
        return flushed

    def reset(self) -> None:
        """Forget stream state (new session, reattach, lost continuity)."""
        self._hold = b""
        self._in_string: Optional[bytes] = None  # terminator kind: b"osc" or b"st"
        self._alt_modes: Set[bytes] = set()
        self._carry = b""
        self._ctx = b""  # plain text before the carry: lookbehind for validators
        self._shown = 0  # leading bytes of _hold already displayed
        # SecureCRT line buffer: (bytes, paint state or None, is text) pieces
        # not yet shown, the already-shown start of the line, and its length.
        self._segs: List[Tuple[bytes, Optional[Tuple[bool, bool]], bool]] = []
        self._prefix = b""
        self._line_len = 0
        self._reset_sgr()

    def _reset_sgr(self) -> None:
        self._fg = self._bg = self._reverse = self._conceal = False
        self._bold = self._faint = False

    @property
    def pending(self) -> bool:
        """True while an unfinished SecureCRT line is held back."""
        return bool(self._segs)

    def flush_due(self) -> bytes:
        """Show the unfinished line once the stream was idle for FLUSH_DELAY."""
        if self._segs and self._clock() - self._fed_at >= FLUSH_DELAY:
            return self.flush()
        return b""

    def flush(self) -> bytes:
        """Show the unfinished line now, coloured as far as it has arrived."""
        return self._emit_line(partial=True) if self._segs else b""

    def feed(self, data: bytes) -> bytes:
        if (
            not self.enabled
            and not self._hold
            and not self._in_string
            and b"\x1b" not in data
            and b"\xc2" not in data  # UTF-8 C1 controls
        ):
            self._carry = self._ctx = b""
            return data  # nothing that changes tracked state: skip the scan
        shown, self._shown = self._shown, 0
        buf = self._hold + bytes(data)
        self._hold = b""
        out: List[bytes] = []
        i, n = 0, len(buf)
        while i < n:
            if self._in_string:
                end = (_OSC_END_RE if self._in_string == b"osc" else _ST_END_RE).search(buf, i)
                if end is None:
                    if buf.endswith((b"\x1b", b"\xc2")):
                        self._hold = buf[-1:]
                        n -= 1
                    out.append(buf[i:n])
                    break
                stop = end.start() if end.group() == b"\x1b" else end.end()
                out.append(buf[i:stop])
                i = stop
                self._in_string = None
                continue
            nxt = _NEXT_RE.search(buf, i)
            stop = n if nxt is None else nxt.start()
            if stop > i:
                out.append(self._text(buf[i:stop], last=stop == n))
                i = stop
            if nxt is None:
                break
            self._carry = self._ctx = b""
            before = len(out)
            i = self._escape(buf, i, out)
            if self._segs and len(out) > before:  # keep it in line order
                self._segs.append((out.pop(), None, False))
                if self._alt_modes:
                    out.append(self._emit_line(partial=False))
        self._fed_at = self._clock()
        if self._segs and _PROMPT.fullmatch(self._line_text().decode("utf-8", "replace")):
            out.append(self.flush())
        if not self.enabled:
            self._shown = len(self._hold)  # disabled output shows everything
            return bytes(data)
        result = b"".join(out)
        if shown:
            if len(self._hold) == len(buf):  # still the same unfinished sequence
                self._shown = shown
            else:
                result = result[shown:]  # the old hold was displayed already
        return result

    def _escape(self, buf: bytes, i: int, out: List[bytes]) -> int:
        """Pass one escape sequence through, tracking state; return the new index."""
        m = _CSI_RE.match(buf, i)
        if m is not None:
            self._csi(m.group(1), m.group(2), m.group(3))
        else:
            m = _STRING_INTRO_RE.match(buf, i)
            if m is not None:
                self._in_string = b"osc" if m.group(1) == b"]" or m.group(2) == b"\x9d" else b"st"
            else:
                m = _ESC_RE.match(buf, i)
                if m is not None:
                    if m.group() == b"\x1bc":
                        out.append(self.flush())
                        self.reset()
                elif _PARTIAL_ESC_RE.match(buf, i):
                    self._hold = buf[i:]
                    return len(buf)
                else:
                    out.append(buf[i:i + 1])
                    return i + 1
        out.append(m.group())
        return m.end()

    def _csi(self, params: bytes, inter: bytes, final: bytes) -> None:
        if final == b"m" and not inter:
            self._sgr(params)
        elif final == b"p" and inter == b"!":  # DECSTR resets SGR, not the screen
            self._reset_sgr()
        elif final in (b"h", b"l") and params.startswith(b"?"):
            for mode in set(params[1:].split(b";")) & _ALT_SCREEN_MODES:
                (self._alt_modes.add if final == b"h" else self._alt_modes.discard)(mode)

    def _sgr(self, params: bytes) -> None:
        it = iter(params.split(b";") if params else [b"0"])
        for raw in it:
            # Colon sub-parameters (38:2::R:G:B, 4:3) belong to their own
            # code: take the code, consume nothing else.
            head, colon, _ = raw.partition(b":")
            code = int(head) if head.isdigit() else 0
            if code == 0:
                self._reset_sgr()
            elif code == 7:
                self._reverse = True
            elif code == 27:
                self._reverse = False
            elif code == 8:
                self._conceal = True
            elif code == 28:
                self._conceal = False
            elif code == 1:
                self._bold = True
            elif code == 2:
                self._faint = True
            elif code == 21:
                self._bold = False
            elif code == 22:
                self._bold = self._faint = False
            elif code == 39:
                self._fg = False
            elif code == 49:
                self._bg = False
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self._fg = True
            elif 40 <= code <= 47 or 100 <= code <= 107:
                self._bg = True
            elif code in (38, 48, 58):
                if code == 38:
                    self._fg = True
                elif code == 48:
                    self._bg = True
                if not colon:  # semicolon form: 38;5;n / 38;2;r;g;b
                    kind = next(it, b"")
                    for _ in range(3 if kind == b"2" else 1 if kind == b"5" else 0):
                        next(it, None)

    def _text(self, text: bytes, last: bool) -> bytes:
        if self.enabled and not self._alt_modes and self._compiled.regex is None:
            return self._line_text_feed(text)
        ctx, carry, self._ctx, self._carry = self._ctx, self._carry, b"", b""
        if (
            not self.enabled
            or self._alt_modes
            or self._fg
            or self._bg
            or self._reverse
            or self._conceal
        ):
            return text
        regex, colours, validators, _ = self._compiled
        full = ctx + carry + text
        skip = len(ctx) + len(carry)
        parts: List[bytes] = []
        pos = 0
        assert regex is not None
        for m in regex.finditer(full):
            if m.end() <= skip:
                continue
            check = validators.get(m.lastgroup)
            if check is not None and not check(full, m):
                continue
            start = max(m.start(), skip)
            parts.append(full[max(pos, skip):start])
            parts.append(colours[m.lastgroup])
            parts.append(full[start:m.end()])
            parts.append(b"\x1b[39m")
            pos = m.end()
        self._remember_tail(full, last)
        if not parts:
            return text
        parts.append(full[max(pos, skip):])
        return b"".join(parts)

    def _remember_tail(self, full: bytes, last: bool) -> None:
        if last:
            tail = _TAIL_RE.search(full, max(0, len(full) - _CARRY_MAX - 1)).group()
            if len(tail) <= _CARRY_MAX:
                self._carry = tail
                keep = len(full) - len(tail)
                self._ctx = full[max(0, keep - _CTX_MAX):keep]

    # --- SecureCRT profile: whole-line matching ----------------------------

    def _line_text_feed(self, text: bytes) -> bytes:
        out: List[bytes] = []
        pos = 0
        for brk in _LINE_BREAK.finditer(text):
            self._add_text(text[pos:brk.start()], out)
            out.append(self._emit_line(partial=False))
            out.append(brk.group())
            pos = brk.end()
        self._add_text(text[pos:], out)
        return b"".join(out)

    def _add_text(self, data: bytes, out: List[bytes]) -> None:
        while len(data) > LINE_MAX - self._line_len:
            cut = LINE_MAX - self._line_len
            while 0 < cut and 0x80 <= data[cut] < 0xC0:  # keep UTF-8 characters whole
                cut -= 1
            if not cut and not self._line_len:
                cut = LINE_MAX
            self._append_text(data[:cut])
            out.append(self._emit_line(partial=False))
            data = data[cut:]
        self._append_text(data)

    def _append_text(self, data: bytes) -> None:
        if not data:
            return
        plain = not (self._fg or self._bg or self._reverse or self._conceal)
        paint = (self._bold, self._faint) if plain else None
        if self._segs and self._segs[-1][2] and self._segs[-1][1] == paint:
            data = self._segs.pop()[0] + data
        self._segs.append((data, paint, True))
        self._line_len += len(data)

    def _line_text(self) -> bytes:
        return self._prefix + b"".join(data for data, _, is_text in self._segs if is_text)

    def _emit_line(self, partial: bool) -> bytes:
        """Colour the held part of the line using the whole line as context.

        ``partial`` keeps the line open: what is shown now becomes context for
        the rest, and an unfinished UTF-8 character stays held.
        """
        segs, keep = self._segs, []
        if partial and segs and segs[-1][2]:
            data, paint, _ = segs[-1]
            tail = _incomplete_utf8(data)
            if tail:
                segs = segs[:-1] + ([(data[:-tail], paint, True)] if tail < len(data) else [])
                keep = [(data[-tail:], paint, True)]
        line = self._prefix + b"".join(data for data, _, is_text in segs if is_text)
        owners = _byte_owners(line, self._compiled.priority_rules)[len(self._prefix):]
        self._segs = keep
        self._prefix = line if partial else b""
        self._line_len = len(self._prefix) + sum(len(data) for data, _, _ in keep)
        rules = self._compiled.priority_rules
        parts: List[bytes] = []
        pos = 0
        for data, paint, is_text in segs:
            if not is_text:
                parts.append(data)
                continue
            seg_owners, pos = owners[pos:pos + len(data)], pos + len(data)
            if paint is None:
                parts.append(data)
                continue
            start = 0
            # Adjacent rules with the same style form one run.
            styles = (None if owner is None else rules[owner][1:4] for owner in seg_owners)
            for style, run in itertools.groupby(styles):
                end = start + sum(1 for _ in run)
                chunk, start = data[start:end], end
                if style is None:
                    parts.append(chunk)
                    continue
                opening, closing, bold = style
                if bold and paint[0]:  # remote bold: add colour only
                    opening, closing = opening.replace(b"[1;", b"[", 1), b"\x1b[39m"
                elif bold and paint[1]:
                    closing = b"\x1b[22;2;39m"  # 22 also ends the remote's faint: restore it
                parts.extend((opening, chunk, closing))
        return b"".join(parts)


def _incomplete_utf8(data: bytes) -> int:
    """Length of an unfinished UTF-8 character at the end of ``data``."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte < 0x80:
            return 0
        if byte >= 0xC0:
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return back if back < need else 0
    return 0


def _byte_owners(line: bytes, rules: Tuple[CompiledRule, ...]) -> List[Optional[int]]:
    """Per-byte rule owners; the regexes see decoded characters."""
    if _LONG_DIGIT_RUN.search(line):
        step = DIGIT_RUN_MAX
        return [owner for start in range(0, len(line), step)
                for owner in _byte_owners(line[start:start + step], rules)]
    text = line.decode("utf-8", "surrogateescape")
    owners = line_owners(text, rules)
    if len(text) == len(line):
        return owners
    return [owner for char, owner in zip(text, owners)
            for _ in range(len(char.encode("utf-8", "surrogateescape")))]


def _dark_background(config: object) -> bool:
    """Whether the terminal background (``#RRGGBB``) is dark; False if unknown."""
    try:
        value = config.get_terminal_profile()["background"]  # type: ignore[attr-defined]
        if len(value) == 4:  # #RGB
            value = "#" + "".join(c * 2 for c in value[1:])
        red, green, blue = (int(value[i:i + 2], 16) for i in (1, 3, 5))
    except Exception:
        return False
    return 0.299 * red + 0.587 * green + 0.114 * blue < 128


def apply_config(highlighter: Highlighter, config: object,
                 store: Optional[Store] = None) -> bytes:
    """Sync ``highlighter`` with the terminal highlight settings.

    Returns bytes to display unchanged (see ``Highlighter.configure``). A
    custom profile or theme that is missing, unreadable or unusable keeps the
    current one, and ``highlighter.warning`` says why (else it is None).
    """
    get = config.get_setting  # type: ignore[attr-defined]
    store = load_store() if store is None else store
    warnings: List[str] = []

    def resolve(value, entries, current, normalize, message):
        name = custom_name(value)
        if name is None:
            return normalize(value)
        if name in entries:
            return entries[name]
        warnings.append(message.format(name=name) + (f" ({store.error})" if store.error else ""))
        return current

    enabled = get("terminal.highlight_enabled", False) is True
    profile = resolve(
        get("terminal.highlight_profile", "generic"), store.profiles, highlighter.profile,
        normalize_profile, _("Highlight profile “{name}” is unavailable; keeping the previous one."),
    )
    theme = resolve(
        get("terminal.highlight_theme", "securecrt"), store.themes, highlighter.theme,
        normalize_theme, _("Highlight theme “{name}” is unavailable; keeping the previous one."),
    )
    if theme == "securecrt" and _dark_background(config):
        theme = "securecrt-dark"  # its colours are made for a light background
    bold = get("terminal.highlight_bold", True) is True
    try:
        held = highlighter.configure(enabled, profile, theme, bold)
    except ValueError as exc:
        warnings.append(_("Highlight profile “{name}” cannot be used: {error}").format(
            name=getattr(profile, "name", profile), error=exc))
        held = highlighter.configure(enabled, highlighter.profile, theme, bold)
    highlighter.warning = " ".join(warnings) or None
    return held
