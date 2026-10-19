"""SecureCRT-compatible Cisco keyword rules, themes, and custom-rule loading.

The 51 entries in :data:`SECURECRT_RULES` correspond one-for-one, in source
order, with the non-comment rules in SecureCRT's ``Cisco Words`` list.  Each
entry retains the original expression for auditing and uses a separate safe
expression for the streaming highlighter.  The complete adaptation table and
custom file format are documented in ``docs/highlighting.md``.

The safe subset rejects lookarounds, backreferences, nested repetition, and
overlong expressions.  ``.*`` is converted to a bounded, single-line match.
This keeps matching time linear enough for terminal-sized (32 KiB) chunks
without adding a regex-engine dependency.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
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
    line_remainder: Optional[str] = None


class CompiledRule(NamedTuple):
    regex: "re.Pattern[bytes]"
    opening: bytes
    closing: bytes
    bold: bool
    line_remainder: Optional["re.Pattern[bytes]"]


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
}
_LIGHT: Dict[str, Colour] = {
    **_SECURECRT,
    "quoted": (85, 85, 85), "prompt_enable": (0, 70, 150),
    "prompt_user": (0, 95, 105), "interface": (0, 70, 150),
    "ip": (0, 115, 25), "netmask": (0, 125, 20), "mac": (0, 95, 55),
    "good": (0, 125, 35), "bad": (180, 0, 0), "important": (180, 0, 0),
    "traffic": (0, 90, 15), "remark": (0, 90, 15), "progress_active": (0, 45, 180),
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
    **{role: 37 for role in _SECURECRT},
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


def _r(source: str, pattern: str, role: str, *, bold: bool = False,
       line: Optional[str] = None) -> SecureCRTRule:
    return SecureCRTRule(source, pattern, role, bold, line)


# Keep source order: the highlighter uses the list index as override priority.
SECURECRT_RULES = (
    _r(r".*'.*", r"[^'\r\n]{0,256}'[^\r\n]{0,256}", "quoted"),
    _r(r"on-fail.*", r"(?<![A-Za-z0-9])on-fail(?![A-Za-z0-9])", "bad", line=r".*"),
    _r(r"^\w[^>]*#|hostname", r"(?m:^[A-Za-z0-9_][^>\r\n]{0,127}#)|(?<![A-Za-z0-9])hostname(?![A-Za-z0-9])", "prompt_enable"),
    _r(r"^\w[^#]*>", r"(?m:^[A-Za-z0-9_][^#\r\n]{0,127}>)", "prompt_user"),
    _r(r"Embedded-Service-Engine\d\/\d", r"(?<![A-Za-z0-9])Embedded-Service-Engine\d{1,3}/\d{1,3}(?![A-Za-z0-9])", "interface"),
    _r(r".*thernet[0-9]+(?:[\/\.:][0-9]+)+[,:]?(?:\x20|$)", r"(?<![A-Za-z0-9])[A-Za-z]*thernet\d{1,4}(?:[/.:]\d{1,5}){1,5}[,:]?(?= |$)", "interface"),
    _r(r".*thernet[0-9]+[,:]?(?:\x20|$)", r"(?<![A-Za-z0-9])[A-Za-z]*thernet\d{1,4}[,:]?(?= |$)", "interface"),
    _r(r"\b[efgt][a-z]*[0-9]+(?:[\/.:][0-9]+)+[,:*]?(?:\x20|$)", r"(?<![A-Za-z0-9])[efgt][a-z]*\d{1,4}(?:[/.:]\d{1,5}){1,5}[,:*]?(?= |$)", "interface"),
    _r(r"[fgm][aeu][0-9]+[,:*]?(?:\x20|$)", r"(?<![A-Za-z0-9])[fgm][aeu]\d{1,5}[,:*]?(?= |$)", "interface"),
    _r(r"(?:nvi|port-channel|Serial|Po|vfc)[0-9|\/|:|,]+[,:]?(?:\x20|$)", r"(?<![A-Za-z0-9])(?:nvi|port-channel|Serial|Po|vfc)[0-9/:,]{1,32}[,:]?(?= |$)", "interface"),
    _r(r"(?:multi|lo[^c]|tun|mgmt|null)[a-z]*[0-9]+,?", r"(?<![A-Za-z0-9])(?:multi|lo[^c]|tun|mgmt|null)[a-z]*\d{1,5},?(?![A-Za-z0-9])", "interface"),
    _r(r"con[0-9]?|vty|line|aux|console", r"(?<![A-Za-z0-9])(?:con\d?|vty|line|aux|console)(?![A-Za-z0-9])", "interface"),
    _r(r"wwn|pwwn|(?:[a-f0-9]{2}:){7}[a-f0-9]{2}", r"(?<![A-Za-z0-9])(?:p?wwn|(?:[a-f0-9]{2}:){7}[a-f0-9]{2})(?![A-Za-z0-9])", "wwn"),
    _r(r"i?(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:\/[0-9]{1,2}|:[0-9]{1,5})?(?:,(?:[0-9]{1,5})?)?", r"(?<![A-Za-z0-9.])i?(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2}|:\d{1,5})?(?:,\d{0,5})?(?![A-Za-z0-9.])", "ip"),
    _r(r"(?:)(?:0|255)\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}", r"(?<![A-Za-z0-9.])(?:0|255)\.(?:\d{1,3}\.){2}\d{1,3}(?![A-Za-z0-9.])", "netmask"),
    _r(r"(?:[a-f0-9]{2}[:-]){5}[a-f0-9]{2}", r"(?<![a-f0-9])(?:[a-f0-9]{2}[:-]){5}[a-f0-9]{2}(?![a-f0-9])", "mac"),
    _r(r"[a-f0-9]{4}\.[a-f0-9]{4}\.[a-f0-9]{4}", r"(?<![a-f0-9])[a-f0-9]{4}(?:\.[a-f0-9]{4}){2}(?![a-f0-9])", "mac"),
    _r(r"\d*\d*\.\d*\((?:\d|[a-z])*\)[^,]*|\d\d?\.\d\d?\.\d\d?\.\D\D?", r"(?<![A-Za-z0-9])(?:\d{0,16}\.\d{0,16}\([0-9a-z]{0,32}\)[^,\r\n]{0,128}|\d{1,2}(?:\.\d{1,2}){2}\.[^\d\r\n][^\d\r\n]?)(?![A-Za-z0-9])", "version"),
    _r(r"(?:[a-z]{2}.\d{4}.{4})", r"(?<![A-Za-z0-9])[a-z]{2}[^\r\n]\d{4}[^\r\n]{4}(?![A-Za-z0-9])", "serial"),
    _r(r"(?:yes|permit|\[OK\]|on|enabled).?", r"(?<![A-Za-z0-9])(?:yes|permit|\[OK\]|on|enabled)(?![A-Za-z0-9])[^\r\n]?", "good"),
    _r(r"down->up|running|.*SUCCESS.*|.*success.*|up.?(?:\x20|$)|passed.*|Complete", r"(?<![A-Za-z0-9])(?:down->up|running|SUCCESS|success|up[^\r\n]?(?= |$)|passed|Complete)(?![A-Za-z0-9])", "good", line=r"^(?:SUCCESS|success|passed)$"),
    _r(r".*_ERR:|.*fail.*|invalid|.*reload.*", r"(?:_ERR:|(?<![A-Za-z0-9])(?:fail|[Ii]nvalid|reload)(?![A-Za-z0-9]))", "bad", line=r"^(?:_ERR:|fail|reload)$"),
    _r(r"no|administratively|shut.*|never,?|deny|.*down.?", r"(?<![A-Za-z0-9])(?:no|administratively|shut[A-Za-z0-9_-]{0,64}|never,?|deny|down[^\r\n]?)(?![A-Za-z0-9])", "bad", line=r"^(?:shut[A-Za-z0-9_-]{0,64}|down.?)$"),
    _r(r"not|initializing.?|Off|1024|768|des|des56.*", r"(?<![A-Za-z0-9])(?:not|initializing[^\r\n]?|Off|1024|768|des|des56)(?![A-Za-z0-9])", "bad", line=r"^des56$"),
    _r(r"telnet|half-duplex.?|\(err-disabled\)|disabled", r"(?<![A-Za-z0-9])(?:telnet|half-duplex[^\r\n]?|\(err-disabled\)|disabled)(?![A-Za-z0-9])", "bad"),
    _r(r"up->down|trunk|Active|inhibit", r"(?<![A-Za-z0-9])(?:up->down|trunk|Active|inhibit)(?![A-Za-z0-9])", "bad"),
    _r(r"(?:class|policy|service|parameter|match)(?:-map.?|-policy.?)?", r"(?<![A-Za-z0-9])(?:class|policy|service|parameter|match)(?:-(?:map|policy)[^\r\n]?)?(?![A-Za-z0-9])", "misc"),
    _r(r"(?:version|PN|SN|S\/N|ID|PID|VID|NAME|DESCR):?", r"(?<![A-Za-z0-9])(?:version|PN|SN|S/N|ID|PID|VID|NAME|DESCR):?(?![A-Za-z0-9])", "misc"),
    _r(r"Device|ID|Local|Intrfce|Holdtme|Capability|Platform|Port|ID", r"(?<![A-Za-z0-9])(?:Device|ID|Local|Intrfce|Holdtme|Capability|Platform|Port)(?![A-Za-z0-9])", "misc"),
    _r(r"H|Address|Interface|Hold|Uptime|SRTT|RTO|Q|Seq", r"(?<![A-Za-z0-9])(?:H|Address|Interface|Hold|Uptime|SRTT|RTO|Q|Seq)(?![A-Za-z0-9])", "misc"),
    _r(r"Neighbor|V|AS|MsgRcvd|MsgSent|TblVer|InQ|OutQ|Up/Down|State/Pf.*", r"(?<![A-Za-z0-9])(?:Neighbor|V|AS|MsgRcvd|MsgSent|TblVer|InQ|OutQ|Up/Down|State/Pf)(?![A-Za-z0-9])", "misc", line=r"^State/Pf$"),
    _r(r"interface|IP-Address|Method|OK\?|Status|Protocol", r"(?<![A-Za-z0-9])(?:interface|IP-Address|Method|OK\?|Status|Protocol)(?![A-Za-z0-9])", "misc"),
    _r(r"aaa|vlan\d*|description.?|MTU|BW|DLY|Vl[0-9]+", r"(?<![A-Za-z0-9])(?:aaa|vlan\d{0,4}|description[^\r\n]?|MTU|BW|DLY|Vl\d{1,4})(?![A-Za-z0-9])", "misc"),
    _r(r"bits\/sec,|packets\/sec", r"(?<![A-Za-z0-9])(?:bits/sec,|packets/sec)(?![A-Za-z0-9])", "traffic"),
    _r(r"Building|configuration\.\.\.", r"(?<![A-Za-z0-9])(?:Building|configuration\.\.\.)(?![A-Za-z0-9])", "important"),
    _r(r"erase|remove|delete.", r"(?<![A-Za-z0-9])(?:erase|remove|delete[^\r\n]?)(?![A-Za-z0-9])", "important"),
    _r(r"\[confirm\]|\(yes/no\):.*|\[yes\/no\]:.*|.*-more-.*", r"(?:\[confirm\]|\(yes/no\):|\[yes/no\]:|-more-)", "important", line=r"^(?:\(yes/no\):|\[yes/no\]:|-more-)$"),
    _r(r".*-more-.*", r"-more-", "important", line=r".*"),
    _r(r"username.*|password.*|key|.*-key", r"(?<![A-Za-z0-9])(?:username|password|key)(?![A-Za-z0-9])|(?<![A-Za-z0-9])[A-Za-z0-9_-]{1,64}-key(?![A-Za-z0-9])", "secret", line=r"^(?:username|password|[A-Za-z0-9_-]{1,64}-key)$"),
    _r(r"\(hitcnt=0\)", r"\(hitcnt=0\)", "acl_zero"),
    _r(r"\(hitcnt=[1-9][0-9]*\)", r"\(hitcnt=[1-9]\d{0,9}\)", "acl_hit"),
    _r(r"access-(?:lists?|class|group)|use-acl|prefix-list", r"(?<![A-Za-z0-9])(?:access-(?:lists?|class|group)|use-acl|prefix-list)(?![A-Za-z0-9])", "acl"),
    _r(r"time-range|object-group|route-map", r"(?<![A-Za-z0-9])(?:time-range|object-group|route-map)(?![A-Za-z0-9])", "acl"),
    _r(r"remark|\*+|!+|###+|@+$", r"(?<![A-Za-z0-9])remark(?![A-Za-z0-9])|\*{1,128}|!{1,128}|#{3,128}|@{1,128}(?=$)", "remark"),
    _r(r"\[#+(?:\x20|$)|^\](?:\x20|$)", r"\[#{1,128}(?= |$)|(?m:^\])(?= |$)", "progress_active", bold=True),
    _r(r"\[#+\]", r"\[#{1,128}\]", "progress"),
    _r(r"ftp|tcp|udp|tftp|scp|ssh|ntp|snmp.*|inspect|icmp", r"(?<![A-Za-z0-9])(?:ftp|tcp|udp|tftp|scp|ssh|ntp|snmp|inspect|icmp)(?![A-Za-z0-9])", "services", line=r"^snmp$"),
    _r(r"router|eigrp|bgp|ospf|rip|gre|hsrp", r"(?<![A-Za-z0-9])(?:router|eigrp|bgp|ospf|rip|gre|hsrp)(?![A-Za-z0-9])", "services"),
    _r(r"Syslog_Messages", r"(?<![A-Za-z0-9])Syslog_Messages(?![A-Za-z0-9])", "syslog_header"),
    _r(r"%.+-[0-9]-.+:|\b.*\.(?:bin|tar)", r"%[^\r\n]{1,128}?-\d-[^:\r\n]{1,128}:|(?<![A-Za-z0-9])[^\s\r\n]{1,128}\.(?:bin|tar)(?![A-Za-z0-9])", "syslog"),
    _r(r".*|setasregextosetdefaultcolor", r"[^\r\n]{0,512}|(?<![A-Za-z0-9])setasregextosetdefaultcolor(?![A-Za-z0-9])", "default"),
)

_ADAPTED = {rule.source: rule for rule in SECURECRT_RULES}
_UNSAFE_GROUP = re.compile(r"\(\?(?!:|m:)")
_BACKREF = re.compile(r"\\[1-9]")
_NESTED_REPEAT = re.compile(r"\([^)]*(?:[?*+{]|\|)[^)]*\)(?:[?*+]|\{\d*,)")


def _safe_custom_pattern(pattern: str) -> str:
    if len(pattern) > 512:
        raise ValueError("pattern exceeds 512 characters")
    if _UNSAFE_GROUP.search(pattern) or _BACKREF.search(pattern) or _NESTED_REPEAT.search(pattern):
        raise ValueError("pattern uses unsupported high-complexity regex constructs")
    pattern = pattern.replace(".*", r"[^\r\n]{0,512}")
    re.compile(pattern.encode("ascii"))
    return pattern


def load_rules(path: Optional[Path] = None) -> List[SecureCRTRule]:
    """Load simple or SecureCRT rules; invalid entries are warned and skipped.

    With no path, the first ``highlight_rules.*`` file in sshPilot's config
    directory is loaded. Simple rows are ``"regex",role-or-#RRGGBB[,bold]``.
    SecureCRT ``00BBGGRR`` rows are imported as literal RGB colours.
    """
    if path is None:
        candidates = sorted(get_config_dir().glob("highlight_rules.*"))
        if not candidates:
            return []
        path = candidates[0]
    rules: List[SecureCRTRule] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        logger.warning("Cannot load highlight rules from %s: %s", path, exc)
        return []
    for line_number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            row = next(csv.reader([line], skipinitialspace=True))
            source = row[0]
            if source.startswith("[*]"):
                continue
            target = row[1].strip().split()[0]
            bold = any(value.strip().lower() in {"bold", "true", "1"} for value in row[2:])
            known = _ADAPTED.get(source)
            pattern = known.pattern if known else _safe_custom_pattern(source)
            line_remainder = known.line_remainder if known else (r".*" if ".*" in source else None)
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
            if source == r"\[#+(?:\x20|$)|^\](?:\x20|$)":
                bold = True
            rules.append(SecureCRTRule(source, pattern, role, bold, line_remainder))
        except (IndexError, UnicodeEncodeError, ValueError, re.error) as exc:
            logger.warning("Skipping invalid highlight rule %s:%d: %s", path, line_number, exc)
    return rules


def compile_rules(theme: str) -> Tuple[CompiledRule, ...]:
    """Compile the built-in profile plus the optional user override file."""
    compiled = []
    for rule in (*SECURECRT_RULES, *load_rules()):
        opening, closing = style_bytes(rule.role, theme, rule.bold)
        remainder = re.compile(rule.line_remainder.encode("ascii")) if rule.line_remainder else None
        compiled.append(CompiledRule(
            re.compile(rule.pattern.encode("ascii")), opening, closing, rule.bold, remainder,
        ))
    return tuple(compiled)


def highlight_priority(
    full: bytes,
    skip: int,
    rules: Tuple[CompiledRule, ...],
    continuing: Optional[Tuple[int, CompiledRule]],
    remote_bold: bool,
) -> Tuple[bytes, Optional[Tuple[int, CompiledRule]]]:
    """Resolve later-rule overlaps, then inject SGR in one output pass."""
    owners: List[Optional[int]] = [None] * (len(full) - skip)
    if continuing:
        end = min((p for p in (full.find(b"\r", skip), full.find(b"\n", skip)) if p >= 0),
                  default=len(full))
        for index in range(skip, end):
            owners[index - skip] = continuing[0]
        if end < len(full):
            continuing = None

    for priority, rule in enumerate(rules):
        if not rule.opening:  # final SecureCRT default rule is the unstyled baseline
            continue
        for match in rule.regex.finditer(full):
            if match.end() <= skip or match.end() == match.start():
                continue
            start = max(match.start(), skip)
            end = match.end()
            if rule.line_remainder and rule.line_remainder.fullmatch(match.group()):
                end = min(
                    (p for p in (full.find(b"\r", start), full.find(b"\n", start)) if p >= 0),
                    default=len(full),
                )
                if end == len(full):
                    continuing = (priority, rule)
            for index in range(start, end):
                owners[index - skip] = priority

    if full.endswith((b"\r", b"\n")):
        continuing = None
    visible = full[skip:]
    parts: List[bytes] = []
    position = 0
    while position < len(visible):
        owner = owners[position]
        end = position + 1
        while end < len(visible) and owners[end] == owner:
            end += 1
        chunk = visible[position:end]
        if owner is None:
            parts.append(chunk)
        else:
            rule = rules[owner]
            opening, closing = rule.opening, rule.closing
            if rule.bold and remote_bold:
                opening, closing = opening.replace(b"[1;", b"[", 1), b"\x1b[39m"
            parts.extend((opening, chunk, closing))
        position = end
    return b"".join(parts), continuing
