"""User highlight profiles and colour themes: model, validation and storage.

A custom profile is an ordered rule list run by the line engine of
:mod:`terminal_highlight` (later rules win where they overlap). A custom theme
is a built-in theme plus per-role colour overrides. Both are saved in
``highlight_profiles.json`` in sshPilot's config directory::

    {"version": 1,
     "profiles": [{"name": "Lab", "rules": [{"pattern": "...", "colour": "ip",
                   "bold": false, "enabled": true, "match_case": false,
                   "description": ""}]}],
     "themes": [{"name": "Mine", "base": "dark", "colours": {"ip": "#00FF00"}}]}

Settings refer to them as ``custom:<name>``. Loading never raises: a bad file
gives an empty store with ``error`` set. Rule patterns are checked by
:func:`terminal_highlight.check_profile`, which owns the trusted built-in
patterns and the safety guard.

This module is pure Python and does not import ``gi``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from gettext import gettext as _
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from .i18n import N_
from .platform.paths import get_config_dir
from .terminal_highlight_securecrt import (
    _MAX_FILE_BYTES,
    _MAX_RULES,
    RGB,
    THEME_COLOURS,
    THEMES,
    Colour,
)

logger = logging.getLogger(__name__)

STORE_FILE = "highlight_profiles.json"
STORE_VERSION = 1
CUSTOM_PREFIX = "custom:"
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 200
MAX_ENTRIES = 100  # profiles, and themes, per file
# Every role a rule or theme can colour; "default" is the unstyled baseline.
ROLES = tuple(role for role in THEME_COLOURS["securecrt"] if role != "default")
# Built-in names, shown in the selectors; custom names may not repeat them.
PROFILE_LABELS = {
    "generic": N_("Generic"),
    "cisco-ios": N_("Cisco IOS"),
    "cisco-securecrt": N_("Cisco (SecureCRT-style)"),
    "junos": N_("Junos"),
    "huawei-vrp": N_("Huawei VRP"),
}
THEME_LABELS = {
    "securecrt": N_("SecureCRT"),
    "dark": N_("Dark"),
    "light": N_("Light"),
    "high-contrast": N_("High Contrast"),
    "terminal-palette": N_("Terminal Palette"),
}
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")


@dataclass(frozen=True)
class EditableRule:
    pattern: str
    colour: str  # a role from ROLES or "#RRGGBB"
    bold: bool = False
    enabled: bool = True
    match_case: bool = False
    description: str = ""


@dataclass(frozen=True)
class CustomProfile:
    name: str
    rules: Tuple[EditableRule, ...]


@dataclass(frozen=True)
class CustomTheme:
    name: str
    base: str  # a built-in theme
    colours: Tuple[Tuple[str, str], ...] = ()  # (role, "#RRGGBB") overrides


@dataclass
class Store:
    profiles: Dict[str, CustomProfile] = field(default_factory=dict)
    themes: Dict[str, CustomTheme] = field(default_factory=dict)
    error: Optional[str] = None


def custom_id(name: str) -> str:
    return CUSTOM_PREFIX + name


def custom_name(value: object) -> Optional[str]:
    """The name in a ``custom:<name>`` setting value, else None."""
    if isinstance(value, str) and value.startswith(CUSTOM_PREFIX):
        return value[len(CUSTOM_PREFIX):]
    return None


def hex_to_rgb(value: str) -> RGB:
    return tuple(bytes.fromhex(value[1:]))  # type: ignore[return-value]


def reserved_names(labels: Mapping[str, str]) -> List[str]:
    """Built-in ids and labels (also translated), which custom names avoid."""
    return [*labels, *labels.values(), *map(_, labels.values())]


def check_name(name: str, taken: Iterable[str]) -> str:
    """Return the stripped name, or raise ValueError with a message to show."""
    name = name.strip()
    if not name:
        raise ValueError(_("Enter a name."))
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(_("The name is longer than {count} characters.").format(
            count=MAX_NAME_LENGTH))
    if any(name.casefold() == other.casefold() for other in taken):
        raise ValueError(_("The name “{name}” is already used.").format(name=name))
    return name


def check_colour(colour: str) -> None:
    if colour not in ROLES and not _HEX.fullmatch(colour):
        raise ValueError(_("Unknown colour “{colour}”.").format(colour=colour))


def check_theme(theme: CustomTheme) -> None:
    if theme.base not in THEMES:
        raise ValueError(_("Unknown base theme “{theme}”.").format(theme=theme.base))
    for role, colour in theme.colours:
        if role not in ROLES:
            raise ValueError(_("Unknown role “{role}”.").format(role=role))
        if not isinstance(colour, str) or not _HEX.fullmatch(colour):
            raise ValueError(_("Colour for “{role}” must be #RRGGBB.").format(role=role))


def theme_colours(theme: Union[str, CustomTheme]) -> Mapping[str, Colour]:
    """Role colours of a built-in theme id or a custom theme."""
    if isinstance(theme, CustomTheme):
        overrides = {role: hex_to_rgb(colour) for role, colour in theme.colours}
        return {**THEME_COLOURS[theme.base], **overrides}
    return THEME_COLOURS[theme if theme in THEMES else "securecrt"]


# --- JSON ---------------------------------------------------------------


def _kind_name(kind: type) -> str:
    return {str: _("text"), bool: _("true or false"), list: _("a list"),
            dict: _("an object")}[kind]


def _field(data: Mapping[str, Any], key: str, kind: type, default: Any = None) -> Any:
    value = data.get(key, default)
    if not isinstance(value, kind):
        raise ValueError(_("“{key}” must be {kind}.").format(key=key, kind=_kind_name(kind)))
    return value


def _object(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(_("Each {what} must be an object.").format(what=what))
    return value


def _rule_from_json(data: Any) -> EditableRule:
    data = _object(data, _("rule"))
    rule = EditableRule(
        _field(data, "pattern", str),
        _field(data, "colour", str),
        _field(data, "bold", bool, False),
        _field(data, "enabled", bool, True),
        _field(data, "match_case", bool, False),
        _field(data, "description", str, "")[:MAX_DESCRIPTION_LENGTH],
    )
    check_colour(rule.colour)
    return rule


def parse_store(text: str) -> Store:
    """Parse the file contents; raise ValueError on any structural problem."""
    data = json.loads(text)
    # type() rather than ==, so that "version": true is not taken for 1.
    if not isinstance(data, dict) or type(data.get("version")) is not int \
            or data["version"] != STORE_VERSION:
        raise ValueError(_("Expected an object with \"version\": {version}.").format(
            version=STORE_VERSION))
    store = Store()
    profiles = _field(data, "profiles", list, [])
    themes = _field(data, "themes", list, [])
    if len(profiles) > MAX_ENTRIES or len(themes) > MAX_ENTRIES:
        raise ValueError(_("More than {count} profiles or themes.").format(count=MAX_ENTRIES))
    for entry in profiles:
        entry = _object(entry, _("profile"))
        name = check_name(_field(entry, "name", str), store.profiles)
        rules = _field(entry, "rules", list)
        if len(rules) > _MAX_RULES:
            raise ValueError(_("Profile “{name}” has more than {count} rules.").format(
                name=name, count=_MAX_RULES))
        store.profiles[name] = CustomProfile(name, tuple(map(_rule_from_json, rules)))
    for entry in themes:
        entry = _object(entry, _("theme"))
        name = check_name(_field(entry, "name", str), store.themes)
        colours = _field(entry, "colours", dict, {})
        theme = CustomTheme(name, _field(entry, "base", str), tuple(colours.items()))
        check_theme(theme)
        store.themes[name] = theme
    return store


def dump_store(store: Store) -> str:
    return json.dumps({
        "version": STORE_VERSION,
        "profiles": [
            {"name": profile.name, "rules": [vars(rule) for rule in profile.rules]}
            for profile in store.profiles.values()
        ],
        "themes": [
            {"name": theme.name, "base": theme.base, "colours": dict(theme.colours)}
            for theme in store.themes.values()
        ],
    }, indent=2, ensure_ascii=False) + "\n"


# --- File ---------------------------------------------------------------

_cache: Dict[Path, Tuple[Tuple[int, int], Store]] = {}


def store_path() -> Path:
    return get_config_dir() / STORE_FILE


def load_store(path: Optional[Path] = None) -> Store:
    """Read the store (cached while the file is unchanged); never raises."""
    path = store_path() if path is None else path
    try:
        stat = path.stat()
    except FileNotFoundError:
        return Store()
    except OSError as exc:
        return Store(error=str(exc))
    stamp = stat.st_mtime_ns, stat.st_size
    cached = _cache.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        if stat.st_size > _MAX_FILE_BYTES:
            raise ValueError(_("The file is larger than {count} bytes.").format(
                count=_MAX_FILE_BYTES))
        store = parse_store(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a user file must never break terminals
        logger.warning("Cannot load highlight profiles from %s: %s", path, exc)
        store = Store(error=_("Cannot read {path}: {error}").format(path=path, error=exc))
    _cache[path] = stamp, store
    return store


def _write(store: Store, path: Path) -> None:
    data = dump_store(store).encode("utf-8")
    if len(data) > _MAX_FILE_BYTES:  # the loader would reject it
        raise ValueError(_("Custom highlighting would exceed {count} KiB; remove some rules.")
                         .format(count=_MAX_FILE_BYTES // 1024))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and load_store(path).error:
        # Never overwrite a file the user may still want to repair.
        backup = path.with_name(path.name + ".bad")
        os.replace(path, backup)
        logger.warning("Moved unreadable highlight profiles to %s", backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".highlight_profiles.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    _cache.pop(path, None)


def _current(path: Optional[Path]) -> Tuple[Path, Store]:
    """The store as it is on disk now: every save re-reads it, so a save only
    loses a change another sshPilot process wrote in the same instant."""
    path = store_path() if path is None else path
    store = load_store(path)
    if store.error:
        store = Store()
    return path, Store(dict(store.profiles), dict(store.themes))


def save_profile(profile: CustomProfile, replacing: Optional[str] = None,
                 path: Optional[Path] = None) -> CustomProfile:
    """Validate and store ``profile``, replacing the profile named ``replacing``.

    ``replacing=None`` saves a new profile. Raises ValueError with a message to
    show; the file is unchanged then.
    """
    from .terminal_highlight import check_profile  # it imports this module

    path, store = _current(path)
    name = check_name(profile.name, [*(n for n in store.profiles if n != replacing),
                                     *reserved_names(PROFILE_LABELS)])
    rules = tuple(replace(rule, description=rule.description.strip()) for rule in profile.rules)
    profile = replace(profile, name=name, rules=rules)
    if len(profile.rules) > _MAX_RULES:
        raise ValueError(_("A profile can have at most {count} rules.").format(count=_MAX_RULES))
    if replacing is None and len(store.profiles) >= MAX_ENTRIES:
        raise ValueError(_("At most {count} custom profiles can be saved.").format(
            count=MAX_ENTRIES))
    check_profile(profile)
    store.profiles = _replaced(store.profiles, replacing, name, profile)
    _write(store, path)
    return profile


def save_theme(theme: CustomTheme, replacing: Optional[str] = None,
               path: Optional[Path] = None) -> CustomTheme:
    """Like :func:`save_profile`, for a colour theme."""
    path, store = _current(path)
    name = check_name(theme.name, [*(n for n in store.themes if n != replacing),
                                   *reserved_names(THEME_LABELS)])
    theme = replace(theme, name=name)
    if replacing is None and len(store.themes) >= MAX_ENTRIES:
        raise ValueError(_("At most {count} custom themes can be saved.").format(
            count=MAX_ENTRIES))
    check_theme(theme)
    store.themes = _replaced(store.themes, replacing, name, theme)
    _write(store, path)
    return theme


def delete_profile(name: str, path: Optional[Path] = None) -> None:
    path, store = _current(path)
    if store.profiles.pop(name, None) is not None:
        _write(store, path)


def delete_theme(name: str, path: Optional[Path] = None) -> None:
    path, store = _current(path)
    if store.themes.pop(name, None) is not None:
        _write(store, path)


def _replaced(entries: Dict[str, Any], old: Optional[str], name: str, value: Any) -> Dict[str, Any]:
    """``entries`` with ``old`` replaced in place by ``name``, or ``name`` appended."""
    if old not in entries:
        return {**entries, name: value}
    return {name if key == old else key: value if key == old else entry
            for key, entry in entries.items()}
