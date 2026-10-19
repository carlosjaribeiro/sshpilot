"""SecureCRT-compatible Cisco keyword rules, themes, and custom-rule loading.

The 51 entries in :data:`SECURECRT_RULES` correspond one-for-one, in source
order, with the non-comment rules in SecureCRT's ``Cisco Words`` list.  Each
entry retains the original expression for auditing and uses a separate safe
expression for the streaming highlighter.  The complete adaptation table and
custom file format are documented in ``docs/highlighting.md``.

Built-in rules match case-insensitively, like the source list's
``Match Case`` off. They are vetted by adversarial timing tests. Custom rules
must fit a small tokenised allowlist (:func:`_safe_custom_pattern`) whose
repetitions are capped, so matching stays fast on terminal-sized chunks
without adding a regex-engine dependency.
"""

from __future__ import annotations

import csv
import itertools
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple, Union

from .platform.paths import get_config_dir

logger = logging.getLogger(__name__)

THEMES = ("securecrt", "dark", "light", "high-contrast", "terminal-palette")

RGB = Tuple[int, int, int]
Colour = Union[int, RGB, None]


@dataclass(frozen=True)
class SecureCRTRule:
    """One rule with source provenance and its stream-safe equivalent."""

    source: str
    pattern: str
    role: str
    bold: bool = False
    # A trigger that fully matches ``line_remainder`` is extended to the end
    # of the line (streamed); one that fully matches ``line_prefix`` is
    # extended back to the start of the line (as far as is still on hold).
    line_remainder: Optional[str] = None
    line_prefix: Optional[str] = None
    match_case: bool = False


class CompiledRule(NamedTuple):
    regex: "re.Pattern[bytes]"
    opening: bytes
    closing: bytes
    bold: bool
    line_remainder: Optional["re.Pattern[bytes]"]
    line_prefix: Optional["re.Pattern[bytes]"]


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
    "dark": _DARK,
    "light": _LIGHT,
    "high-contrast": _HIGH_CONTRAST,
    "terminal-palette": _PALETTE,
}


def normalize_theme(value: object) -> str:
    return value if isinstance(value, str) and value in THEMES else "securecrt"


def style_bytes(role: str, theme: str, bold: bool = False) -> Tuple[bytes, bytes]:
    """Return opening/closing SGR for a semantic role or literal ``#RRGGBB``."""
    if role.startswith("#") and re.fullmatch(r"#[0-9A-Fa-f]{6}", role):
        colour: Colour = tuple(bytes.fromhex(role[1:]))  # type: ignore[assignment]
    else:
        colour = THEME_COLOURS[normalize_theme(theme)].get(role)
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
    SecureCRTRule(r".*'.*", r"[^'\r\n]{0,256}'[^\r\n]{0,256}", "quoted"),
    SecureCRTRule(r"on-fail.*", r"(?<![A-Za-z0-9])on-fail(?![A-Za-z0-9])", "bad", line_remainder=r".*"),
    SecureCRTRule(r"^\w[^>]*#|hostname", r"(?<=[\r\n])[A-Za-z0-9_][^>\r\n]{0,127}#|(?<![A-Za-z0-9])hostname(?![A-Za-z0-9])", "prompt_enable"),
    SecureCRTRule(r"^\w[^#]*>", r"(?<=[\r\n])[A-Za-z0-9_][^#\r\n]{0,127}>", "prompt_user"),
    SecureCRTRule(r"Embedded-Service-Engine\d\/\d", r"(?<![A-Za-z0-9])Embedded-Service-Engine\d{1,3}/\d{1,3}(?![A-Za-z0-9])", "interface"),
    SecureCRTRule(r".*thernet[0-9]+(?:[\/\.:][0-9]+)+[,:]?(?:\x20|$)", r"(?<![A-Za-z0-9])[A-Za-z]*thernet\d{1,4}(?:[/.:]\d{1,5}){1,5}[,:]?(?=[ \r\n]|\Z)", "interface"),
    SecureCRTRule(r".*thernet[0-9]+[,:]?(?:\x20|$)", r"(?<![A-Za-z0-9])[A-Za-z]*thernet\d{1,4}[,:]?(?=[ \r\n]|\Z)", "interface"),
    SecureCRTRule(r"\b[efgt][a-z]*[0-9]+(?:[\/.:][0-9]+)+[,:*]?(?:\x20|$)", r"(?<![A-Za-z0-9])[efgt][a-z]*\d{1,4}(?:[/.:]\d{1,5}){1,5}[,:*]?(?=[ \r\n]|\Z)", "interface"),
    SecureCRTRule(r"[fgm][aeu][0-9]+[,:*]?(?:\x20|$)", r"(?<![A-Za-z0-9])[fgm][aeu]\d{1,5}[,:*]?(?=[ \r\n]|\Z)", "interface"),
    SecureCRTRule(r"(?:nvi|port-channel|Serial|Po|vfc)[0-9|\/|:|,]+[,:]?(?:\x20|$)", r"(?<![A-Za-z0-9])(?:nvi|port-channel|Serial|Po|vfc)[0-9/:,]{1,32}[,:]?(?=[ \r\n]|\Z)", "interface"),
    SecureCRTRule(r"(?:multi|lo[^c]|tun|mgmt|null)[a-z]*[0-9]+,?", r"(?<![A-Za-z0-9])(?:multi|lo[^c]|tun|mgmt|null)[a-z]*\d{1,5},?(?![A-Za-z0-9])", "interface"),
    SecureCRTRule(r"con[0-9]?|vty|line|aux|console", r"(?<![A-Za-z0-9])(?:con\d?|vty|line|aux|console)(?![A-Za-z0-9])", "interface"),
    SecureCRTRule(r"wwn|pwwn|(?:[a-f0-9]{2}:){7}[a-f0-9]{2}", r"(?<![A-Za-z0-9])(?:p?wwn|(?:[a-f0-9]{2}:){7}[a-f0-9]{2})(?![A-Za-z0-9])", "wwn"),
    SecureCRTRule(r"i?(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:\/[0-9]{1,2}|:[0-9]{1,5})?(?:,(?:[0-9]{1,5})?)?", r"(?<![A-Za-z0-9.])i?(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2}|:\d{1,5})?(?:,\d{0,5})?(?![A-Za-z0-9.])", "ip"),
    SecureCRTRule(r"(?:)(?:0|255)\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}", r"(?<![A-Za-z0-9.])(?:0|255)\.(?:\d{1,3}\.){2}\d{1,3}(?![A-Za-z0-9.])", "netmask"),
    SecureCRTRule(r"(?:[a-f0-9]{2}[:-]){5}[a-f0-9]{2}", r"(?<![a-f0-9])(?:[a-f0-9]{2}[:-]){5}[a-f0-9]{2}(?![a-f0-9])", "mac"),
    SecureCRTRule(r"[a-f0-9]{4}\.[a-f0-9]{4}\.[a-f0-9]{4}", r"(?<![a-f0-9])[a-f0-9]{4}(?:\.[a-f0-9]{4}){2}(?![a-f0-9])", "mac"),
    SecureCRTRule(r"\d*\d*\.\d*\((?:\d|[a-z])*\)[^,]*|\d\d?\.\d\d?\.\d\d?\.\D\D?", r"(?<![A-Za-z0-9])(?:\d{0,16}\.\d{0,16}\([0-9a-z]{0,32}\)[^,\r\n]{0,128}|\d{1,2}(?:\.\d{1,2}){2}\.[^\d\r\n][^\d\r\n]?)(?![A-Za-z0-9])", "version"),
    SecureCRTRule(r"(?:[a-z]{2}.\d{4}.{4})", r"(?<![A-Za-z0-9])[a-z]{2}[^\r\n]\d{4}[^\r\n]{4}(?![A-Za-z0-9])", "serial"),
    SecureCRTRule(r"(?:yes|permit|\[OK\]|on|enabled).?", r"(?<![A-Za-z0-9])(?:yes|permit|\[OK\]|on|enabled)(?![A-Za-z0-9])[^\r\n]?", "good"),
    SecureCRTRule(r"down->up|running|.*SUCCESS.*|.*success.*|up.?(?:\x20|$)|passed.*|Complete", r"(?<![A-Za-z0-9])(?:down->up|running|SUCCESS|success|up[^\r\n]?(?=[ \r\n]|\Z)|passed|Complete)(?![A-Za-z0-9])", "good", line_remainder=r"^(?:SUCCESS|success|passed)$"),
    SecureCRTRule(r".*_ERR:|.*fail.*|invalid|.*reload.*", r"(?:_ERR:|(?<![A-Za-z0-9])(?:fail|invalid|reload)(?![A-Za-z0-9]))", "bad", line_remainder=r"^(?:fail|reload)$", line_prefix=r"^_ERR:$"),
    SecureCRTRule(r"no|administratively|shut.*|never,?|deny|.*down.?", r"(?<![A-Za-z0-9])(?:no|administratively|shut[A-Za-z0-9_-]{0,64}|never,?|deny|down[^\r\n]?)(?![A-Za-z0-9])", "bad", line_remainder=r"^shut[A-Za-z0-9_-]{0,64}$", line_prefix=r"^down.?$"),
    SecureCRTRule(r"not|initializing.?|Off|1024|768|des|des56.*", r"(?<![A-Za-z0-9])(?:not|initializing[^\r\n]?|Off|1024|768|des|des56)(?![A-Za-z0-9])", "bad", line_remainder=r"^des56$"),
    SecureCRTRule(r"telnet|half-duplex.?|\(err-disabled\)|disabled", r"(?<![A-Za-z0-9])(?:telnet|half-duplex[^\r\n]?|\(err-disabled\)|disabled)(?![A-Za-z0-9])", "bad"),
    SecureCRTRule(r"up->down|trunk|Active|inhibit", r"(?<![A-Za-z0-9])(?:up->down|trunk|Active|inhibit)(?![A-Za-z0-9])", "bad"),
    SecureCRTRule(r"(?:class|policy|service|parameter|match)(?:-map.?|-policy.?)?", r"(?<![A-Za-z0-9])(?:class|policy|service|parameter|match)(?:-(?:map|policy)[^\r\n]?)?(?![A-Za-z0-9])", "misc"),
    SecureCRTRule(r"(?:version|PN|SN|S\/N|ID|PID|VID|NAME|DESCR):?", r"(?<![A-Za-z0-9])(?:version|PN|SN|S/N|ID|PID|VID|NAME|DESCR):?(?![A-Za-z0-9])", "misc"),
    SecureCRTRule(r"Device|ID|Local|Intrfce|Holdtme|Capability|Platform|Port|ID", r"(?<![A-Za-z0-9])(?:Device|ID|Local|Intrfce|Holdtme|Capability|Platform|Port)(?![A-Za-z0-9])", "misc"),
    SecureCRTRule(r"H|Address|Interface|Hold|Uptime|SRTT|RTO|Q|Seq", r"(?<![A-Za-z0-9])(?:H|Address|Interface|Hold|Uptime|SRTT|RTO|Q|Seq)(?![A-Za-z0-9])", "misc"),
    SecureCRTRule(r"Neighbor|V|AS|MsgRcvd|MsgSent|TblVer|InQ|OutQ|Up/Down|State/Pf.*", r"(?<![A-Za-z0-9])(?:Neighbor|V|AS|MsgRcvd|MsgSent|TblVer|InQ|OutQ|Up/Down|State/Pf)(?![A-Za-z0-9])", "misc", line_remainder=r"^State/Pf$"),
    SecureCRTRule(r"interface|IP-Address|Method|OK\?|Status|Protocol", r"(?<![A-Za-z0-9])(?:interface|IP-Address|Method|OK\?|Status|Protocol)(?![A-Za-z0-9])", "misc"),
    SecureCRTRule(r"aaa|vlan\d*|description.?|MTU|BW|DLY|Vl[0-9]+", r"(?<![A-Za-z0-9])(?:aaa|vlan\d{0,4}|description[^\r\n]?|MTU|BW|DLY|Vl\d{1,4})(?![A-Za-z0-9])", "misc"),
    SecureCRTRule(r"bits\/sec,|packets\/sec", r"(?<![A-Za-z0-9])(?:bits/sec,|packets/sec)(?![A-Za-z0-9])", "traffic"),
    SecureCRTRule(r"Building|configuration\.\.\.", r"(?<![A-Za-z0-9])(?:Building|configuration\.\.\.)(?![A-Za-z0-9])", "important"),
    SecureCRTRule(r"erase|remove|delete.", r"(?<![A-Za-z0-9])(?:erase|remove|delete[^\r\n]?)(?![A-Za-z0-9])", "important"),
    SecureCRTRule(r"\[confirm\]|\(yes/no\):.*|\[yes\/no\]:.*|.*-more-.*", r"(?:\[confirm\]|\(yes/no\):|\[yes/no\]:|-{1,8}more-)", "important", line_remainder=r"^(?:\(yes/no\):|\[yes/no\]:|-{1,8}more-)$"),
    SecureCRTRule(r".*-more-.*", r"-{1,8}more-", "important", line_remainder=r".*"),
    SecureCRTRule(r"username.*|password.*|key|.*-key", r"(?<![A-Za-z0-9])(?:username|password|key)(?![A-Za-z0-9])|-key(?![A-Za-z0-9])", "secret", line_remainder=r"^(?:username|password)$", line_prefix=r"^-key$"),
    SecureCRTRule(r"\(hitcnt=0\)", r"\(hitcnt=0\)", "acl_zero"),
    SecureCRTRule(r"\(hitcnt=[1-9][0-9]*\)", r"\(hitcnt=[1-9]\d{0,9}\)", "acl_hit"),
    SecureCRTRule(r"access-(?:lists?|class|group)|use-acl|prefix-list", r"(?<![A-Za-z0-9])(?:access-(?:lists?|class|group)|use-acl|prefix-list)(?![A-Za-z0-9])", "acl"),
    SecureCRTRule(r"time-range|object-group|route-map", r"(?<![A-Za-z0-9])(?:time-range|object-group|route-map)(?![A-Za-z0-9])", "acl"),
    SecureCRTRule(r"remark|\*+|!+|###+|@+$", r"(?<![A-Za-z0-9])remark(?![A-Za-z0-9])|\*{1,128}|!{1,128}|#{3,128}|@{1,128}(?=[\r\n]|\Z)", "remark"),
    SecureCRTRule(r"\[#+(?:\x20|$)|^\](?:\x20|$)", r"\[#{1,128}(?=[ \r\n]|\Z)|(?<=[\r\n])\](?=[ \r\n]|\Z)", "progress_active", bold=True),
    SecureCRTRule(r"\[#+\]", r"\[#{1,128}\]", "progress"),
    SecureCRTRule(r"ftp|tcp|udp|tftp|scp|ssh|ntp|snmp.*|inspect|icmp", r"(?<![A-Za-z0-9])(?:ftp|tcp|udp|tftp|scp|ssh|ntp|snmp|inspect|icmp)(?![A-Za-z0-9])", "services", line_remainder=r"^snmp$"),
    SecureCRTRule(r"router|eigrp|bgp|ospf|rip|gre|hsrp", r"(?<![A-Za-z0-9])(?:router|eigrp|bgp|ospf|rip|gre|hsrp)(?![A-Za-z0-9])", "services"),
    SecureCRTRule(r"Syslog_Messages", r"(?<![A-Za-z0-9])Syslog_Messages(?![A-Za-z0-9])", "syslog_header"),
    SecureCRTRule(r"%.+-[0-9]-.+:|\b.*\.(?:bin|tar)", r"%[^\r\n]{1,128}?-\d-[^:\r\n]{1,128}:|(?<![A-Za-z0-9])[^\s\r\n]{1,128}\.(?:bin|tar)(?![A-Za-z0-9])", "syslog"),
    SecureCRTRule(r".*|setasregextosetdefaultcolor", r"[^\r\n]{0,512}|(?<![A-Za-z0-9])setasregextosetdefaultcolor(?![A-Za-z0-9])", "default"),
)

_ADAPTED = {rule.source: rule for rule in SECURECRT_RULES}
_MAX_FILE_BYTES = 256 * 1024
_MAX_RULES = 500
_MAX_REPEAT = 32
_MATCH_CASE = re.compile(r'^\s*D:"Match Case"=([0-9A-Fa-f]{8})\s*$', re.MULTILINE)
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
        raise ValueError("pattern exceeds 512 characters")
    pattern.encode("ascii")
    parts: List[str] = []
    depth = quantified = pos = 0
    previous = None
    while pos < len(pattern):
        match = _TOKEN.match(pattern, pos)
        if match is None:
            raise ValueError(f"unsupported regex syntax at offset {pos}")
        kind, text = match.lastgroup, match.group()
        if kind == "quant":
            if previous != "atom":
                raise ValueError("a quantifier must follow a single character or class")
            quantified += 1
            if quantified > 2:
                raise ValueError("more than two quantifiers")
            if text.startswith("{"):
                bounds = [int(number) for number in text[1:-1].split(",")]
                if bounds[0] > bounds[-1] or bounds[-1] > _MAX_REPEAT:
                    raise ValueError(f"repetition above {_MAX_REPEAT}")
            text = {"*": f"{{0,{_MAX_REPEAT}}}", "+": f"{{1,{_MAX_REPEAT}}}"}.get(text, text)
        elif kind == "open":
            depth += 1
        elif kind == "close":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses")
        elif kind == "bol":
            text = r"(?<=[\r\n])"
        elif kind == "eol":
            text = r"(?=[\r\n]|\Z)"
        parts.append(text)
        previous = kind
        pos = match.end()
    if depth:
        raise ValueError("unbalanced parentheses")
    result = "".join(parts)
    re.compile(result.encode("ascii"))
    return result


def load_rules(path: Optional[Path] = None) -> List[SecureCRTRule]:
    """Load simple or SecureCRT rules; invalid entries are warned and skipped.

    With no path, the first ``highlight_rules.*`` file in sshPilot's config
    directory is loaded. Simple rows are ``"regex",role-or-#RRGGBB[,bold]``.
    SecureCRT ``00BBGGRR`` rows are imported as literal RGB colours. Rules
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
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#") or _MATCH_CASE.fullmatch(line):
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
            bold = any(value.strip().lower() in {"bold", "true", "1"} for value in row[2:])
            known = _ADAPTED.get(source)
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


def _compile_optional(pattern: Optional[str], flags: int) -> Optional["re.Pattern[bytes]"]:
    return re.compile(pattern.encode("ascii"), flags) if pattern else None


def compile_rules(theme: str) -> Tuple[CompiledRule, ...]:
    """Compile the built-in profile plus the optional user override file."""
    try:
        custom = load_rules()
    except Exception:  # a user file must never break the terminal
        logger.exception("Cannot load highlight rules")
        custom = []
    compiled = []
    for rule in (*SECURECRT_RULES, *custom):
        flags = 0 if rule.match_case else re.IGNORECASE
        opening, closing = style_bytes(rule.role, theme, rule.bold)
        compiled.append(CompiledRule(
            re.compile(rule.pattern.encode("ascii"), flags), opening, closing, rule.bold,
            _compile_optional(rule.line_remainder, flags),
            _compile_optional(rule.line_prefix, flags),
        ))
    return tuple(compiled)


_EOL = re.compile(rb"[\r\n]")


def _line_end(full: bytes, pos: int) -> int:
    match = _EOL.search(full, pos)
    return len(full) if match is None else match.start()


def highlight_priority(
    full: bytes,
    skip: int,
    rules: Tuple[CompiledRule, ...],
    continuing: Optional[Tuple[int, CompiledRule]],
    remote_bold: bool,
    remote_faint: bool = False,
    line_start: bool = True,
) -> Tuple[bytes, Optional[Tuple[int, CompiledRule]]]:
    """Resolve later-rule overlaps, then inject SGR in one output pass.

    ``continuing`` is the rule whose line remainder ran off the previous chunk;
    it is painted at its own priority, so later rules still override it.
    ``line_start`` says whether ``full`` begins a line: a sentinel byte makes
    ``^`` (written ``(?<=[\\r\\n])``) see the stream, not the window. Each
    rule writes each byte at most once, so the work is linear in ``full``.
    """
    full = (b"\n" if line_start else b"\x00") + full
    skip += 1
    owners: List[Optional[int]] = [None] * (len(full) - skip)
    carried, continuing = continuing, None
    for priority, rule in enumerate(rules):
        if not rule.opening:  # final SecureCRT default rule is the unstyled baseline
            continue
        covered = scanned = line = skip  # painted / newline-scanned up to; line start
        if carried and carried[0] == priority:
            covered = _line_end(full, skip)
            owners[:covered - skip] = [priority] * (covered - skip)
            if covered == len(full):
                continuing = (priority, rule)
        for match in rule.regex.finditer(full):
            start, end = match.span()
            if end <= covered or end == start:
                continue
            if rule.line_prefix and rule.line_prefix.fullmatch(match.group()):
                found = max(full.rfind(b"\r", scanned, start), full.rfind(b"\n", scanned, start))
                if found >= 0:
                    line = found + 1
                scanned = max(scanned, start)
                start = line
            if rule.line_remainder and rule.line_remainder.fullmatch(match.group()):
                end = _line_end(full, end)
                if end == len(full):
                    continuing = (priority, rule)
            start = max(start, covered)
            owners[start - skip:end - skip] = [priority] * (end - start)
            covered = end

    parts: List[bytes] = []
    position = skip
    for owner, run in itertools.groupby(owners):
        end = position + sum(1 for _ in run)
        chunk = full[position:end]
        position = end
        if owner is None:
            parts.append(chunk)
            continue
        rule = rules[owner]
        opening, closing = rule.opening, rule.closing
        if rule.bold and remote_bold:
            opening, closing = opening.replace(b"[1;", b"[", 1), b"\x1b[39m"
        elif rule.bold and remote_faint:
            closing = b"\x1b[22;2;39m"  # 22 also ends the remote's faint: restore it
        parts.extend((opening, chunk, closing))
    return b"".join(parts), continuing
