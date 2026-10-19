"""Custom highlight profiles and themes: model, storage, validation, engine."""

import json
import re

import pytest

from sshpilot.terminal_highlight import (
    PROFILES,
    Highlighter,
    apply_config,
    check_profile,
    preview_runs,
    profile_rules,
)
from sshpilot.terminal_highlight_custom import (
    STORE_FILE,
    CustomProfile,
    CustomTheme,
    EditableRule,
    Store,
    custom_id,
    delete_profile,
    delete_theme,
    dump_store,
    load_store,
    parse_store,
    save_profile,
    save_theme,
)

SGR = re.compile(rb"\x1b\[[0-9;]*m")
RED, GREEN = "#FF0000", "#00FF00"


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    """Hermetic config dir for the store and the SecureCRT rules file."""
    import sshpilot.terminal_highlight as highlight
    import sshpilot.terminal_highlight_custom as custom
    import sshpilot.terminal_highlight_securecrt as securecrt

    monkeypatch.setattr(custom, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(securecrt, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(custom, "_cache", {})
    monkeypatch.setattr(highlight, "_COMPILED", {})
    return tmp_path


class _Config:
    def __init__(self, **values):
        self.values = {"terminal.highlight_enabled": True, **values}

    def get_setting(self, key, default=None):
        return self.values.get(key, default)


def fg(rgb, text, bold=False):
    r, g, b = bytes.fromhex(rgb[1:])
    opening = b"\x1b[%s38;2;%d;%d;%dm" % (b"1;" if bold else b"", r, g, b)
    return opening + text + (b"\x1b[22;39m" if bold else b"\x1b[39m")


def profile(*rules, name="Lab"):
    return CustomProfile(name, tuple(rules))


def feed(target, data, theme="securecrt"):
    h = Highlighter(target, theme=theme)
    return h.feed(data) + h.flush()


# --- storage ------------------------------------------------------------------


def test_profile_round_trips_through_the_versioned_file(config_dir):
    lab = profile(EditableRule(r"\bcore\b", "important", bold=True, description="Core"),
                  EditableRule(r"lab\d", "#12AB34", enabled=False, match_case=True))
    save_profile(lab)

    data = json.loads((config_dir / STORE_FILE).read_text())
    assert data["version"] == 1
    assert load_store().profiles == {"Lab": lab}


def test_duplicate_builtin_then_save_keeps_builtin_and_matches_it(config_dir):
    sample = (b"Gi0/1 is up, Internet address is 10.0.0.1/24 vlan 10 vlan 5000\r\n"
              b"Cisco IOS Software, Version 15.2.4.1\r\n")
    for builtin in PROFILES:
        before = profile_rules(builtin)
        copy = profile(*before, EditableRule(r"\bInternet\b", "#ABCDEF"), name=f"My {builtin}")
        save_profile(copy)
        assert profile_rules(builtin) == before  # the built-in is untouched

        saved = load_store().profiles[f"My {builtin}"]
        out = feed(saved, sample)
        assert SGR.sub(b"", out) == sample
        assert fg("#ABCDEF", b"Internet") in out
        # Built-in checks travel with the copied patterns: a version string is
        # not an address and VLAN 5000 is out of range.
        if builtin != "cisco-securecrt":
            assert b"m15.2.4.1" not in out
            assert b"m10.0.0.1/24" in out
            assert b"mvlan 5000" not in out
    assert len(load_store().profiles) == len(PROFILES)


def test_duplicated_legacy_profile_colours_the_same_tokens():
    sample = (b"GigabitEthernet0/1 is up, line protocol is up\r\n"
              b"Gi0/2  unassigned  YES unset  administratively down down\r\n"
              b"aa:bb:cc:dd:ee:ff 2001:db8::1 % Invalid input\r\n"
              b"% Invalid input 10.0.0.1\r\nerror: interface ge-0/0/0 is down\r\n")

    def tokens(out):
        return [m.group(1) for m in re.finditer(rb"\x1b\[[0-9;]+m([^\x1b]*)\x1b\[39m", out)]

    for builtin in ("generic", "cisco-ios", "junos", "huawei-vrp"):
        copy = profile(*profile_rules(builtin))
        assert tokens(feed(copy, sample)) == tokens(feed(builtin, sample)), builtin


def test_store_cache_never_serves_a_replaced_file(config_dir):
    """Same size, same timestamp (coarse file-system clocks): still reloaded."""
    import os

    save_profile(profile(EditableRule("core", RED)))
    stamp = (config_dir / STORE_FILE).stat().st_mtime_ns
    load_store()
    save_profile(profile(EditableRule("core", GREEN)), replacing="Lab")
    os.utime(config_dir / STORE_FILE, ns=(stamp, stamp))
    assert load_store().profiles["Lab"].rules[0].colour == GREEN


def test_name_collisions_are_rejected(config_dir):
    save_profile(profile(name="Lab"))
    for clash in ("Lab", "lab", "  LAB "):
        with pytest.raises(ValueError, match="already used"):
            save_profile(profile(name=clash))
    with pytest.raises(ValueError, match="Enter a name"):
        save_profile(profile(name="   "))
    save_profile(profile(name="Other"))
    with pytest.raises(ValueError, match="already used"):
        save_profile(profile(name="Lab"), replacing="Other")  # rename onto Lab
    # Saving under its own name edits in place; renaming keeps the position.
    save_profile(profile(EditableRule("x", "ip"), name="Lab"), replacing="Lab")
    save_profile(profile(name="Lab 2"), replacing="Lab")
    assert list(load_store().profiles) == ["Lab 2", "Other"]


@pytest.mark.parametrize("pattern, message", [
    ("(a+)+$", "quantifier"),
    ("a{1,99}", "repetition above 32"),
    ("a{1,100000}", "unsupported"),
    ("(?=x)", "must follow a single"),
    ("[z-a]", "Invalid pattern"),
    ("é", "ASCII"),
    ("", "Enter a pattern"),
])
def test_unsafe_or_invalid_regex_is_rejected_and_nothing_is_written(config_dir, pattern, message):
    with pytest.raises(ValueError, match=f"(?i)Rule 2: .*{message}"):
        save_profile(profile(EditableRule("ok", "ip"), EditableRule(pattern, "ip")))
    assert not (config_dir / STORE_FILE).exists()


def test_unknown_colour_is_rejected():
    with pytest.raises(ValueError, match="Unknown colour"):
        check_profile(profile(EditableRule("x", "chartreuse")))


def test_rule_count_cap(config_dir):
    with pytest.raises(ValueError, match="at most 500"):
        save_profile(profile(*[EditableRule("x", "ip")] * 501))


def test_delete_only_touches_custom_entries(config_dir):
    save_profile(profile(name="Lab"))
    save_theme(CustomTheme("Mine", "dark"))
    delete_profile("Lab")
    delete_theme("Mine")
    delete_profile("generic")  # not a custom name: nothing to do
    assert load_store() == Store()


# --- corrupt files --------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "{not json",
    '{"version": 2, "profiles": []}',
    '{"version": 1, "profiles": [{"name": "Lab", "rules": [{"pattern": 1, "colour": "ip"}]}]}',
    '{"version": 1, "profiles": [{"name": "Lab", "rules": []}, {"name": "lab", "rules": []}]}',
    '{"version": 1, "themes": [{"name": "T", "base": "neon"}]}',
    '[]',
])
def test_corrupt_file_loads_empty_with_an_error(config_dir, text):
    (config_dir / STORE_FILE).write_text(text)
    store = load_store()
    assert (store.profiles, store.themes) == ({}, {})
    assert store.error


def test_corrupt_file_falls_back_to_previous_profile_with_a_warning(config_dir):
    save_profile(profile(EditableRule(r"10\.0\.0\.1", RED)))
    h = Highlighter(enabled=False)
    apply_config(h, _Config(**{"terminal.highlight_profile": custom_id("Lab")}))
    assert h.profile.name == "Lab" and h.warning is None

    (config_dir / STORE_FILE).write_text("{oops")
    apply_config(h, _Config(**{"terminal.highlight_profile": custom_id("Lab")}))
    assert h.profile.name == "Lab"  # previous one kept
    assert "Lab" in h.warning and "Cannot read" in h.warning
    assert h.feed(b"10.0.0.1\n") == fg(RED, b"10.0.0.1") + b"\n"

    fresh = Highlighter(enabled=False)  # nothing previous: the default
    apply_config(fresh, _Config(**{"terminal.highlight_profile": custom_id("Lab")}))
    assert fresh.profile == "generic" and fresh.warning


def test_hand_edited_unsafe_rule_falls_back_instead_of_running(config_dir):
    bad = {"version": 1, "profiles": [{"name": "Lab", "rules": [
        {"pattern": "(a+)+$", "colour": "ip"}]}]}
    (config_dir / STORE_FILE).write_text(json.dumps(bad))
    h = Highlighter("junos", enabled=False)
    apply_config(h, _Config(**{"terminal.highlight_profile": custom_id("Lab")}))
    assert h.profile == "junos"
    assert "cannot be used" in h.warning and "Rule 1" in h.warning


def test_saving_over_a_corrupt_file_keeps_a_backup(config_dir):
    (config_dir / STORE_FILE).write_text("{oops")
    save_profile(profile())
    assert (config_dir / (STORE_FILE + ".bad")).read_text() == "{oops"
    assert list(load_store().profiles) == ["Lab"]


# --- engine semantics -----------------------------------------------------------


def test_later_rule_wins_and_reordering_changes_the_result():
    red = EditableRule(r"10\.0\.0\.1", RED)
    green = EditableRule(r"[0-9.]+", GREEN)
    line = b"via 10.0.0.1\n"
    assert feed(profile(red, green), line) == b"via " + fg(GREEN, b"10.0.0.1") + b"\n"
    assert feed(profile(green, red), line) == b"via " + fg(RED, b"10.0.0.1") + b"\n"


def test_bold_flag_and_disabled_rules():
    rules = (EditableRule("core", RED, bold=True), EditableRule("edge", GREEN, enabled=False))
    assert feed(profile(*rules), b"core edge\n") == fg(RED, b"core", bold=True) + b" edge\n"


def test_match_case_flag():
    assert feed(profile(EditableRule("Up", RED)), b"up\n") == fg(RED, b"up") + b"\n"
    assert feed(profile(EditableRule("Up", RED, match_case=True)), b"up\n") == b"up\n"


def test_profile_without_enabled_rules_passes_text_through():
    assert feed(profile(), b"10.0.0.1 up\n") == b"10.0.0.1 up\n"
    assert feed(profile(EditableRule("up", RED, enabled=False)), b"up\n") == b"up\n"


def test_roles_follow_the_theme():
    rule = EditableRule("up", "good")
    assert feed(profile(rule), b"up\n", theme="terminal-palette") == b"\x1b[32mup\x1b[39m\n"


def test_preview_runs_match_the_engine():
    lab = profile(EditableRule(r"[0-9.]+", "ip"),
                  EditableRule("core", "#123456", bold=True))
    assert preview_runs(lab, "securecrt", "core 10.0.0.1 x") == [
        ("core", (0x12, 0x34, 0x56), True), (" ", None, False),
        ("10.0.0.1", (0, 174, 0), False), (" x", None, False),
    ]


# --- themes ---------------------------------------------------------------------


def test_custom_theme_persists_and_recolours_its_roles(config_dir):
    save_theme(CustomTheme("Mine", "dark", (("ip", "#123456"),)))
    theme = load_store().themes["Mine"]
    assert theme == CustomTheme("Mine", "dark", (("ip", "#123456"),))
    with pytest.raises(ValueError, match="already used"):
        save_theme(CustomTheme("mine", "light"))
    with pytest.raises(ValueError, match="Unknown role"):
        save_theme(CustomTheme("Bad", "dark", (("nope", "#000000"),)))

    h = Highlighter(enabled=False)
    apply_config(h, _Config(**{"terminal.highlight_profile": "cisco-securecrt",
                               "terminal.highlight_theme": custom_id("Mine")}))
    assert h.theme == theme
    out = h.feed(b"192.168.1.1 up\n")
    assert b"\x1b[38;2;18;52;86m192.168.1.1\x1b[39m" in out  # overridden role
    assert b"\x1b[38;2;80;230;110m" not in out  # dark's own ip colour is gone


def test_missing_custom_theme_keeps_the_previous_theme(config_dir):
    h = Highlighter("cisco-securecrt", enabled=False, theme="light")
    apply_config(h, _Config(**{"terminal.highlight_profile": "cisco-securecrt",
                               "terminal.highlight_theme": custom_id("Gone")}))
    assert h.theme == "light" and "Gone" in h.warning


# --- live settings ----------------------------------------------------------------


def test_saved_edit_applies_to_the_live_filter_on_the_next_settings_change(config_dir):
    """The editor saves, then sets the setting; terminals re-run apply_config."""
    save_profile(profile(EditableRule("core", RED)))
    config = _Config(**{"terminal.highlight_profile": custom_id("Lab")})
    h = Highlighter(enabled=False)
    apply_config(h, config)
    assert h.feed(b"core\n") == fg(RED, b"core") + b"\n"

    h.feed(b"core")  # an unfinished line is held when the edit lands
    save_profile(profile(EditableRule("core", GREEN), name="Lab"), replacing="Lab")
    held = apply_config(h, config)
    assert held == b"core"  # shown as it was, never lost
    assert h.feed(b"\ncore\n") == b"\n" + fg(GREEN, b"core") + b"\n"


def test_store_serialisation_is_stable():
    store = Store({"Lab": profile(EditableRule("x", "ip"))},
                  {"T": CustomTheme("T", "light", (("bad", "#000000"),))})
    assert parse_store(dump_store(store)) == store


def test_terminals_toast_a_fallback_once(config_dir, monkeypatch):
    import sshpilot.terminal as terminal
    from sshpilot.terminal import TerminalWidget

    class Toast:
        def __init__(self, title):
            self.title = title

        new = classmethod(lambda cls, title: cls(title))
        set_timeout = set_use_markup = get_title = lambda self, *_args: self.title

    monkeypatch.setattr(terminal.Adw, "Toast", Toast)
    monkeypatch.setattr(TerminalWidget, "_highlight_warning_shown", None)
    (config_dir / STORE_FILE).write_text("{oops")
    config = _Config(**{"terminal.highlight_profile": custom_id("Lab")})
    shown = []
    widgets = []
    for _ in range(2):
        widget = TerminalWidget.__new__(TerminalWidget)
        widget.config = config
        widget._present_toast = lambda toast: shown.append(toast.get_title()) or True
        widgets.append(widget)

    for widget in widgets:
        TerminalWidget._apply_highlight_settings(widget)
    assert len(shown) == 1 and "Lab" in shown[0]
    assert widgets[1]._highlighter.profile == "generic"

    config.values["terminal.highlight_profile"] = "junos"  # fixed: no warning
    TerminalWidget._apply_highlight_settings(widgets[0])
    config.values["terminal.highlight_profile"] = custom_id("Lab")  # broken again
    TerminalWidget._apply_highlight_settings(widgets[0])
    assert len(shown) == 2


# --- review round 2 -----------------------------------------------------------------


@pytest.mark.parametrize("text", [
    '{"version": 1, "themes": [{"name": "T", "base": "dark", "colours": {"ip": 123}}]}',
    '{"version": 1, "themes": [{"name": "T", "base": "dark", "colours": {"ip": null}}]}',
    '{"version": 1, "themes": [{"name": "T", "base": "dark", "colours": []}]}',
    '{"version": 1, "themes": [{"name": "T", "base": 5}]}',
    '{"version": 1, "themes": ["T"]}',
    '{"version": 1, "profiles": [{"name": 5, "rules": []}]}',
    '{"version": 1, "profiles": [{"name": "P", "rules": {}}]}',
    '{"version": 1, "profiles": ["P"]}',
    '{"version": 1, "profiles": [{"name": "P", "rules": [7]}]}',
    '{"version": 1, "profiles": [{"name": "P", "rules": [{"pattern": "x", "colour": 1}]}]}',
    '{"version": 1, "profiles": [{"name": "P", "rules": [{"pattern": "x", "colour": "ip",'
    ' "bold": 1}]}]}',
    '{"version": 1, "profiles": [{"name": "P", "rules": [{"pattern": "x", "colour": "ip",'
    ' "description": []}]}]}',
    '{"version": true}',
    '{"version": 1, "profiles": null}',
])
def test_wrong_types_are_rejected_with_value_error_and_filter_falls_back(config_dir, text):
    with pytest.raises(ValueError):
        parse_store(text)
    (config_dir / STORE_FILE).write_text(text)
    h = Highlighter("junos", enabled=False)
    apply_config(h, _Config(**{"terminal.highlight_profile": custom_id("P"),
                               "terminal.highlight_theme": custom_id("T")}))
    assert h.profile == "junos" and h.warning


def test_description_length_is_enforced_at_save(config_dir):
    with pytest.raises(ValueError, match="description is longer than 200"):
        save_profile(profile(EditableRule("x", "ip", description="d" * 201)))
    assert not (config_dir / STORE_FILE).exists()
    save_profile(profile(EditableRule("x", "ip", description="  core  ")))
    assert load_store().profiles["Lab"].rules[0].description == "core"


def test_never_writes_a_file_the_loader_rejects(config_dir):
    big = profile(*[EditableRule("x" * 500, "ip", description="d" * 200)] * 400)
    with pytest.raises(ValueError, match="exceed 256 KiB"):
        save_profile(big)
    assert not (config_dir / STORE_FILE).exists()


@pytest.mark.parametrize("name", ["generic", "Generic", "CISCO IOS", "cisco-securecrt",
                                  "Cisco (SecureCRT-style)", "huawei vrp"])
def test_custom_profile_names_avoid_builtins(config_dir, name):
    with pytest.raises(ValueError, match="already used"):
        save_profile(profile(name=name))


@pytest.mark.parametrize("name", ["SecureCRT", "dark", "High Contrast", "terminal-palette"])
def test_custom_theme_names_avoid_builtins(config_dir, name):
    with pytest.raises(ValueError, match="already used"):
        save_theme(CustomTheme(name, "dark"))


def test_custom_rules_are_chunk_split_invariant():
    lab = profile(EditableRule(r"\bcore\b", RED, bold=True), EditableRule(r"[0-9.]+", "ip"),
                  EditableRule(r"^\w+#", GREEN), EditableRule("caf.", "bad"))
    data = "SW1#show core 10.0.0.1 café\r\nlink core down 192.168.1.1\n".encode()
    whole = feed(lab, data)
    for size in (1, 2, 3, 5, 7):
        h = Highlighter(lab)
        parts = b"".join(h.feed(data[i:i + size]) for i in range(0, len(data), size))
        assert parts + h.flush() == whole, size


SECURECRT_SAMPLE = [
    "GigabitEthernet0/1 is up, line protocol is up", "Hardware is Gigabit Ethernet",
    "SW1#show ip interface brief", "unassigned", "  Internet address is 10.0.0.1/24",
    "*Mar  1 00:01:02.123: %LINK-3-UPDOWN: Interface Gi0/1, changed state to down",
    "access-list 101 permit tcp any host 10.0.0.5 eq 22 (hitcnt=0)",
]


def test_editable_securecrt_copy_paints_exactly_like_the_builtin(config_dir):
    """Guard for the copy's rule order: duplicate, import and preview paths."""
    from sshpilot.terminal_highlight_securecrt import load_rules

    rules_file = config_dir / "highlight_rules.txt"
    rules_file.write_text('"\\bGigabit\\b",#FF8800\n')
    copy = profile(*profile_rules("cisco-securecrt"))
    assert copy.rules[-1].pattern == r"\bGigabit\b"  # the file's rules come last
    for line in SECURECRT_SAMPLE:
        data = (line + "\n").encode()
        builtin = feed("cisco-securecrt", data)
        assert feed(copy, data) == builtin, line
        # The preview paints the same characters with the same rules.
        painted = b"".join(
            (fg("#%02X%02X%02X" % colour, text.encode(), bold) if colour else text.encode())
            for text, colour, bold in preview_runs(copy, "securecrt", line))
        assert SGR.sub(b"", painted) == line.encode()
        assert re.findall(rb"38;2;[0-9;]+m", painted) == re.findall(rb"38;2;[0-9;]+m", builtin)
    # Import (as the editor does it) appends in file order, after the built-ins.
    imported = [EditableRule(rule.source, rule.role, rule.bold, match_case=rule.match_case)
                for rule in load_rules(rules_file)]
    assert list(copy.rules[-len(imported):]) == imported


def test_preview_skips_an_invalid_disabled_rule_but_saving_still_rejects_it(config_dir):
    """The editor previews with invalid rules switched off."""
    lab = profile(EditableRule("core", RED), EditableRule("(a+)+$", "ip", enabled=False))
    assert preview_runs(lab, "securecrt", "core")[0][1] == (255, 0, 0)
    assert feed(lab, b"core\n") == fg(RED, b"core") + b"\n"
    with pytest.raises(ValueError, match="Rule 2"):
        save_profile(lab)
