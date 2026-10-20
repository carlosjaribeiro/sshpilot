"""SecureCRT-compatible Cisco keyword rules, themes, and custom-rule loading.

The 51 entries in :data:`SECURECRT_RULES` preserve the non-label rules of
SecureCRT's ``Cisco Words`` list as source text and in source order. Built-ins
run case-insensitively (``Match Case`` off) with a whole-word guard; each
``.*`` is bounded to one non-whitespace word. Custom rules must fit a small
tokenised allowlist (:func:`_safe_custom_pattern`). See ``docs/highlighting.md``.
"""

from __future__ import annotations

import csv
import logging
import colorsys
import re
from dataclasses import dataclass, replace
from pathlib import Path
from gettext import gettext as _
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

from .platform.paths import get_config_dir

logger = logging.getLogger(__name__)

THEMES = ("securecrt", "dark", "light", "high-contrast", "terminal-palette", "securecrt-dark")

RGB = Tuple[int, int, int]
Colour = Union[int, RGB, None]


@dataclass(frozen=True)
class SecureCRTRule:
    """One rule: its source text and the pattern actually compiled.

    Built-in rules compile their source unchanged; custom rules compile the
    allowlisted rebuild from :func:`_safe_custom_pattern`.
    """

    source: str
    pattern: str
    role: str
    bold: bool = False
    match_case: bool = False


class CompiledRule(NamedTuple):
    regex: "re.Pattern[str]"
    opening: bytes
    closing: bytes
    bold: bool
    # (line, match) -> keep the match; built-in patterns of the legacy profiles
    # used in a custom profile bring their checks along.
    validate: Optional[Callable[[str, "re.Match[str]"], bool]] = None


def _r(source: str, role: str, bold: bool = False) -> SecureCRTRule:
    return SecureCRTRule(source, source, role, bold)


def colourref_to_rgb(value: str) -> RGB:
    """Convert SecureCRT ``00BBGGRR`` COLORREF text to an RGB tuple."""
    raw = value.strip().split()[0]
    if not re.fullmatch(r"[0-9A-Fa-f]{8}", raw):
        raise ValueError(f"invalid SecureCRT COLORREF: {value!r}")
    number = int(raw, 16)
    return number & 0xFF, (number >> 8) & 0xFF, (number >> 16) & 0xFF


# Exact SecureCRT colours. Roles which shared a COLORREF in the source share a
# value here, while remaining separate so alternate themes can distinguish them.
_SECURECRT: Dict[str, Colour] = {
    "quoted": (128, 128, 128),
    "prompt_enable": (0, 105, 210),
    "prompt_user": (0, 128, 128),
    "interface": (0, 105, 210),
    "wwn": (255, 128, 64),
    "ip": (0, 174, 0),
    "netmask": (0, 225, 0),
    "mac": (0, 128, 64),
    "version": (255, 128, 0),
    "serial": (255, 128, 0),
    "good": (0, 215, 54),
    "bad": (255, 0, 0),
    "misc": (128, 0, 0),
    "traffic": (0, 100, 0),
    "important": (255, 0, 0),
    "secret": (255, 128, 64),
    "acl_zero": (128, 128, 128),
    "acl_hit": (255, 128, 64),
    "acl": (255, 128, 0),
    "remark": (0, 100, 0),
    "progress_active": (0, 0, 255),
    "progress": (0, 159, 0),
    "services": (140, 78, 165),
    "syslog_header": (255, 128, 0),
    "syslog": (255, 128, 64),
    "default": None,
}



def _on_dark(colour: Colour, lightness: float = 0.62) -> Colour:
    """Lift a colour designed for a light background to be readable on black,
    keeping its hue and saturation (greys keep their grey)."""
    if not isinstance(colour, tuple):
        return colour
    hue, light, sat = colorsys.rgb_to_hls(*(c / 255 for c in colour))
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb(hue, max(light, lightness), sat))


# The SecureCRT colours with the lightness raised: "Cisco Words - DkBg" in spirit.
_SECURECRT_DARK: Dict[str, Colour] = {role: _on_dark(c) for role, c in _SECURECRT.items()}

_DARK: Dict[str, Colour] = {
    **_SECURECRT,
    "quoted": (180, 180, 180), "prompt_enable": (80, 170, 255),
    "prompt_user": (70, 210, 210), "interface": (80, 170, 255),
    "ip": (80, 230, 110), "netmask": (110, 255, 130), "mac": (80, 220, 150),
    "misc": (255, 150, 120), "traffic": (110, 220, 120), "remark": (110, 220, 120),
    "progress_active": (100, 160, 255), "progress": (80, 220, 110),
    "services": (190, 140, 230),
}
_LIGHT: Dict[str, Colour] = {
    **_SECURECRT,
    "quoted": (85, 85, 85), "prompt_enable": (0, 70, 150),
    "prompt_user": (0, 95, 105), "interface": (0, 70, 150),
    "ip": (0, 115, 25), "netmask": (0, 125, 20), "mac": (0, 95, 55),
    "good": (0, 125, 35), "bad": (180, 0, 0), "important": (180, 0, 0),
    "traffic": (0, 90, 15), "remark": (0, 90, 15), "progress_active": (0, 45, 180),
    "acl_zero": (85, 85, 85), "version": (165, 75, 0), "serial": (165, 75, 0),
    "acl": (165, 75, 0), "syslog_header": (165, 75, 0), "wwn": (170, 60, 20),
    "secret": (170, 60, 20), "acl_hit": (170, 60, 20), "syslog": (170, 60, 20),
}
_HIGH_CONTRAST: Dict[str, Colour] = {
    **_SECURECRT,
    "quoted": (255, 255, 255), "prompt_enable": (0, 180, 255),
    "prompt_user": (0, 255, 255), "interface": (0, 180, 255),
    "wwn": (255, 210, 0), "ip": (0, 255, 0), "netmask": (120, 255, 120),
    "mac": (0, 255, 180), "version": (255, 180, 0), "serial": (255, 180, 0),
    "good": (0, 255, 0), "bad": (255, 60, 60), "misc": (255, 160, 160),
    "traffic": (180, 255, 180), "important": (255, 60, 60),
    "secret": (255, 210, 0), "acl": (255, 180, 0), "remark": (180, 255, 180),
    "progress_active": (100, 180, 255), "progress": (0, 255, 0),
    "services": (255, 120, 255), "syslog_header": (255, 180, 0),
    "syslog": (255, 210, 0),
}
_PALETTE: Dict[str, Colour] = {
    "quoted": 90, "acl_zero": 90,
    "prompt_enable": 34, "prompt_user": 36, "interface": 36, "wwn": 93,
    "ip": 36, "netmask": 32, "mac": 35, "version": 33, "serial": 33,
    "good": 32, "bad": 31, "misc": 33, "traffic": 32, "important": 91,
    "secret": 93, "acl_hit": 33, "acl": 33, "remark": 32, "progress_active": 94,
    "progress": 32, "services": 35, "syslog_header": 33, "syslog": 33,
    "default": None,
}

THEME_COLOURS = {
    "securecrt": _SECURECRT,
    "securecrt-dark": _SECURECRT_DARK,
    "dark": _DARK,
    "light": _LIGHT,
    "high-contrast": _HIGH_CONTRAST,
    "terminal-palette": _PALETTE,
}


def normalize_theme(value: object) -> str:
    return value if isinstance(value, str) and value in THEMES else "securecrt"


Theme = Union[str, Mapping[str, Colour]]  # a theme id or its role colours


def style_bytes(role: str, theme: Theme, bold: bool = False) -> Tuple[bytes, bytes]:
    """Return opening/closing SGR for a semantic role or literal ``#RRGGBB``."""
    if role.startswith("#") and re.fullmatch(r"#[0-9A-Fa-f]{6}", role):
        colour: Colour = tuple(bytes.fromhex(role[1:]))  # type: ignore[assignment]
    else:
        colours = theme if isinstance(theme, Mapping) else THEME_COLOURS[normalize_theme(theme)]
        colour = colours.get(role)
    if colour is None:
        return b"", b""
    prefix = b"1;" if bold else b""
    if isinstance(colour, int):
        opening = b"\x1b[" + prefix + str(colour).encode("ascii") + b"m"
    else:
        opening = b"\x1b[" + prefix + b"38;2;%d;%d;%dm" % colour
    return opening, (b"\x1b[22;39m" if bold else b"\x1b[39m")


# Keep source order: the highlighter uses the list index as override priority.
SECURECRT_RULES = (
    _r(r".*'.*", "quoted"),
    _r(r"on-fail.*", "bad"),
    _r(r"^\w[^>]*#|hostname", "prompt_enable"),
    _r(r"^\w[^#]*>", "prompt_user"),
    _r(r"Embedded-Service-Engine\d\/\d", "interface"),
    _r(r".*thernet[0-9]+(?:[\/\.:][0-9]+)+[,:]?(?:\x20|$)", "interface"),
    _r(r".*thernet[0-9]+[,:]?(?:\x20|$)", "interface"),
    _r(r"\b[efgt][a-z]*[0-9]+(?:[\/.:][0-9]+)+[,:*]?(?:\x20|$)", "interface"),
    _r(r"[fgm][aeu][0-9]+[,:*]?(?:\x20|$)", "interface"),
    _r(r"(?:nvi|port-channel|Serial|Po|vfc)[0-9|\/|:|,]+[,:]?(?:\x20|$)", "interface"),
    _r(r"(?:multi|lo[^c]|tun|mgmt|null)[a-z]*[0-9]+,?", "interface"),
    _r(r"con[0-9]?|vty|line|aux|console", "interface"),
    _r(r"wwn|pwwn|(?:[a-f0-9]{2}:){7}[a-f0-9]{2}", "wwn"),
    _r(r"i?(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:\/[0-9]{1,2}|:[0-9]{1,5})?(?:,(?:[0-9]{1,5})?)?", "ip"),
    _r(r"(?:)(?:0|255)\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}", "netmask"),
    _r(r"(?:[a-f0-9]{2}[:-]){5}[a-f0-9]{2}", "mac"),
    _r(r"[a-f0-9]{4}\.[a-f0-9]{4}\.[a-f0-9]{4}", "mac"),
    _r(r"\d*\d*\.\d*\((?:\d|[a-z])*\)[^,]*|\d\d?\.\d\d?\.\d\d?\.\D\D?", "version"),
    _r(r"(?:[a-z]{2}.\d{4}.{4})", "serial"),
    _r(r"(?:yes|permit|\[OK\]|on|enabled).?", "good"),
    _r(r"down->up|running|.*SUCCESS.*|.*success.*|up.?(?:\x20|$)|passed.*|Complete", "good"),
    _r(r".*_ERR:|.*fail.*|invalid|.*reload.*", "bad"),
    _r(r"no|administratively|shut.*|never,?|deny|.*down.?", "bad"),
    _r(r"not|initializing.?|Off|1024|768|des|des56.*", "bad"),
    _r(r"telnet|half-duplex.?|\(err-disabled\)|disabled", "bad"),
    _r(r"up->down|trunk|Active|inhibit", "bad"),
    _r(r"(?:class|policy|service|parameter|match)(?:-map.?|-policy.?)?", "misc"),
    _r(r"(?:version|PN|SN|S\/N|ID|PID|VID|NAME|DESCR):?", "misc"),
    _r(r"Device|ID|Local|Intrfce|Holdtme|Capability|Platform|Port|ID", "misc"),
    _r(r"H|Address|Interface|Hold|Uptime|SRTT|RTO|Q|Seq", "misc"),
    _r(r"Neighbor|V|AS|MsgRcvd|MsgSent|TblVer|InQ|OutQ|Up/Down|State/Pf.*", "misc"),
    _r(r"interface|IP-Address|Method|OK\?|Status|Protocol", "misc"),
    _r(r"aaa|vlan\d*|description.?|MTU|BW|DLY|Vl[0-9]+", "misc"),
    _r(r"bits\/sec,|packets\/sec", "traffic"),
    _r(r"Building|configuration\.\.\.", "important"),
    _r(r"erase|remove|delete.", "important"),
    _r(r"\[confirm\]|\(yes/no\):.*|\[yes\/no\]:.*|.*-more-.*", "important"),
    _r(r".*-more-.*", "important"),
    _r(r"username.*|password.*|key|.*-key", "secret"),
    _r(r"\(hitcnt=0\)", "acl_zero"),
    _r(r"\(hitcnt=[1-9][0-9]*\)", "acl_hit"),
    _r(r"access-(?:lists?|class|group)|use-acl|prefix-list", "acl"),
    _r(r"time-range|object-group|route-map", "acl"),
    _r(r"remark|\*+|!+|###+|@+$", "remark"),
    _r(r"\[#+(?:\x20|$)|^\](?:\x20|$)", "progress_active", bold=True),
    _r(r"\[#+\]", "progress"),
    _r(r"ftp|tcp|udp|tftp|scp|ssh|ntp|snmp.*|inspect|icmp", "services"),
    _r(r"router|eigrp|bgp|ospf|rip|gre|hsrp", "services"),
    _r(r"Syslog_Messages", "syslog_header"),
    _r(r"%.+-[0-9]-.+:|\b.*\.(?:bin|tar)", "syslog"),
    _r(r".*|setasregextosetdefaultcolor", "default"),
)



_BUILTIN = {rule.source: rule for rule in SECURECRT_RULES}
_MAX_FILE_BYTES = 256 * 1024
_MAX_RULES = 500
_MAX_REPEAT = 32
_MATCH_CASE = re.compile(r'^\s*D:"Match Case"=([0-9A-Fa-f]{8})\s*$', re.MULTILINE)
_LIST_HEADER = re.compile(r'^Z:"Keyword List (V\d+)"=')
_SETTING = re.compile(r'^[A-Z]:"')
_V3_ENABLED, _V3_BOLD = 0x1, 0x8  # F1 bits; F2 is not interpreted (see docs)
_TOKEN = re.compile(
    r"""(?P<atom>\\x[0-9A-Fa-f]{2}|\\[dDwWsStnr]|\\[^0-9A-Za-z]
              |\[\^?\]?(?:\\.|[^\]\\])*\]|[^\\\[\](){}|^$*+?])
      |(?P<zero>\\[bB])
      |(?P<quant>[?*+]|\{\d{1,2}(?:,\d{1,2})?\})
      |(?P<open>\((?:\?:)?)
      |(?P<close>\))
      |(?P<bar>\|)
      |(?P<bol>\^)
      |(?P<eol>\$)""",
    re.VERBOSE,
)


def _safe_custom_pattern(pattern: str) -> str:
    """Rebuild a custom rule from the allowlisted subset, or raise ValueError.

    Allowed: literals, escaped characters, classes, ``.``, ``\\d \\w \\s``,
    ``\\b``, ``^``/``$`` (line anchors), groups and ``|``. At most two
    quantifiers, each on a single atom (never a group) and repeating at most
    32 times: ``*`` and ``+`` become ``{0,32}`` and ``{1,32}``.
    """
    if len(pattern) > 512:
        raise ValueError(_("The pattern is longer than 512 characters."))
    pattern.encode("ascii")
    parts: List[str] = []
    depth = quantified = pos = 0
    previous = None
    while pos < len(pattern):
        match = _TOKEN.match(pattern, pos)
        if match is None:
            raise ValueError(_("Unsupported regex syntax at character {position}.").format(
                position=pos + 1))
        kind, text = match.lastgroup, match.group()
        if kind == "quant":
            if previous != "atom":
                raise ValueError(
                    _("A quantifier (?, *, +, {n,m}) must follow a single character or class."))
            quantified += 1
            if quantified > 2:
                raise ValueError(_("At most two quantifiers are allowed."))
            if text.startswith("{"):
                bounds = [int(number) for number in text[1:-1].split(",")]
                if bounds[0] > bounds[-1] or bounds[-1] > _MAX_REPEAT:
                    raise ValueError(
                        _("Repetition above {count} is not allowed.").format(count=_MAX_REPEAT))
            text = {"*": f"{{0,{_MAX_REPEAT}}}", "+": f"{{1,{_MAX_REPEAT}}}"}.get(text, text)
        elif kind == "open":
            depth += 1
        elif kind == "close":
            depth -= 1
            if depth < 0:
                raise ValueError(_("Unbalanced parentheses."))
        parts.append(text)
        previous = kind
        pos = match.end()
    if depth:
        raise ValueError(_("Unbalanced parentheses."))
    result = "".join(parts)
    re.compile(result)
    return result


def load_rules(path: Optional[Path] = None) -> List[SecureCRTRule]:
    """Load simple or SecureCRT rules; invalid entries are warned and skipped.

    With no path, the first ``highlight_rules.*`` file in sshPilot's config
    directory is loaded. Simple rows are ``"regex",role-or-#RRGGBB[,bold]``.
    SecureCRT ``00BBGGRR`` rows are imported as literal RGB colours. In a
    SecureCRT export the ``Keyword List V3`` section is used (V2 is ignored):
    rows are ``"regex",COLORREF,F1,F2``; F1 bit 0 enables, bit 3 is bold. Rules
    are case-insensitive unless the file says ``D:"Match Case"=00000001``.
    Never raises: an unreadable, oversized or undecodable file gives ``[]``.
    """
    try:
        if path is None:
            candidates = sorted(get_config_dir().glob("highlight_rules.*"))
            if not candidates:
                return []
            path = candidates[0]
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError(f"file is larger than {_MAX_FILE_BYTES} bytes")
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as exc:  # ValueError includes UnicodeDecodeError
        logger.warning("Cannot load highlight rules from %s: %s", path, exc)
        return []
    flag = _MATCH_CASE.search(text)
    match_case = bool(flag and int(flag.group(1), 16))
    rules: List[SecureCRTRule] = []
    has_v3 = 'Z:"Keyword List V3"' in text
    version = None
    for line_number, line in enumerate(text.splitlines(), 1):
        header = _LIST_HEADER.match(line)
        if header:
            version = header.group(1)
        if header or _SETTING.match(line):
            continue
        if has_v3 and version != "V3":  # V2 is the legacy copy of the same list
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(rules) == _MAX_RULES:
            logger.warning("Ignoring highlight rules after the first %d in %s", _MAX_RULES, path)
            break
        try:
            row = next(csv.reader([line], skipinitialspace=True))
            source = row[0]
            if source.startswith("[*]"):
                continue
            target = row[1].strip().split()[0]
            if version == "V3":
                flags = int(row[2], 16)
                if not flags & _V3_ENABLED:
                    continue
                bold = bool(flags & _V3_BOLD)
            else:
                bold = any(value.strip().lower() in {"bold", "true", "1"} for value in row[2:])
            known = _BUILTIN.get(source)
            if known and known.role == "default":
                role = "default"
            elif re.fullmatch(r"[0-9A-Fa-f]{8}", target):
                rgb = colourref_to_rgb(target)
                role = "#%02x%02x%02x" % rgb
            elif re.fullmatch(r"#[0-9A-Fa-f]{6}", target):
                role = target
            elif target in _SECURECRT:
                role = target
            else:
                raise ValueError(f"unknown role or colour {target!r}")
            if known:
                rule = replace(known, role=role, bold=bold or known.bold, match_case=match_case)
            else:
                rule = SecureCRTRule(source, _safe_custom_pattern(source), role, bold,
                                     match_case=match_case)
            rules.append(rule)
        except (IndexError, ValueError, csv.Error, re.error) as exc:
            logger.warning("Skipping invalid highlight rule %s:%d: %s", path, line_number, exc)
    return rules


def compile_rules(theme: Theme, bold: bool = False) -> Tuple[CompiledRule, ...]:
    """Compile the built-in profile plus the optional user override file.

    ``bold`` paints every highlighted run bold (SecureCRT's global option).
    """
    try:
        custom = load_rules()
    except Exception:  # a user file must never break the terminal
        logger.exception("Cannot load highlight rules")
        custom = []
    compiled = []
    for rule in (*SECURECRT_RULES, *custom):
        rule_bold = bold or rule.bold
        opening, closing = style_bytes(rule.role, theme, rule_bold)
        compiled.append(CompiledRule(
            re.compile(whole_word(rule.pattern) if rule.source in _BUILTIN else rule.pattern,
                       0 if rule.match_case else re.IGNORECASE),
            opening, closing, rule_bold,
        ))
    return tuple(compiled)


_W = "0-9A-Za-z_"
# A match may not start/end inside a word: a word character at a match edge needs a
# non-word neighbour outside it. Edges that are symbols (``#``, ``.``) are free.
_WHOLE_WORD = (rf"(?:(?<![{_W}])|(?=[^{_W}]))(?:{{}})(?:(?![{_W}])|(?<=[^{_W}]))")


def whole_word(pattern: str) -> str:
    """Make a built-in SecureCRT ``pattern`` match whole words (``docs/highlighting.md``).

    Applied to the built-in rules only; user-authored rules match as written.

    A lookaround rather than a post-filter, so the regex still backtracks to
    an alternative that fits (``shut.*`` after a word start, not ``no`` in ``none``).
    Each ``.*`` stays inside one word (``\\S*``): ``snmp.*`` colours
    ``snmp-server``, ``.*down.?`` colours ``UPDOWN:``, not the rest of the line.
    """
    return _WHOLE_WORD.format(re.sub(r"(?<!\\)\.\*", r"\\S*", pattern))


def line_owners(line: str, rules: Tuple[CompiledRule, ...]) -> List[Optional[int]]:
    """Index of the rule colouring each character of one line (None: unstyled).

    Every rule paints all its matches in list order, so a later rule
    overrides an earlier one wherever they overlap.
    """
    owners: List[Optional[int]] = [None] * len(line)
    for priority, rule in enumerate(rules):
        if not rule.opening:  # final SecureCRT default rule is the unstyled baseline
            continue
        for match in rule.regex.finditer(line):
            if rule.validate is not None and not rule.validate(line, match):
                continue
            start, end = match.span()
            owners[start:end] = [priority] * (end - start)
    return owners
