"""Edit, duplicate and delete custom highlight profiles and colour themes.

Opened from Settings → Terminal → Highlighting. Built-in profiles and themes
are read-only: editing one starts a copy that is saved under a new name. The
model, validation and storage live in the GTK-free ``terminal_highlight*``
modules; after a save the caller selects the result through the settings, so
terminals pick it up by the usual ``setting-changed`` path.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from gettext import gettext as _
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from .custom_terminal_themes import normalize_color
from .terminal_highlight import (
    PROFILES,
    THEMES,
    check_rule,
    preview_runs,
    profile_rules,
)
from .terminal_highlight_custom import (
    ROLES,
    CustomProfile,
    CustomTheme,
    EditableRule,
    check_name,
    custom_id,
    custom_name,
    delete_profile,
    delete_theme,
    load_store,
    save_profile,
    save_theme,
    theme_colours,
)
from .terminal_highlight_securecrt import load_rules
from .terminal_theme_editor import _hex, _rgba

logger = logging.getLogger(__name__)

PROFILE_LABELS = {
    "generic": _("Generic"),
    "cisco-ios": _("Cisco IOS"),
    "cisco-securecrt": _("Cisco (SecureCRT-style)"),
    "junos": _("Junos"),
    "huawei-vrp": _("Huawei VRP"),
}
THEME_LABELS = {
    "securecrt": _("SecureCRT"),
    "dark": _("Dark"),
    "light": _("Light"),
    "high-contrast": _("High Contrast"),
    "terminal-palette": _("Terminal Palette"),
}
ROLE_LABELS = {
    "quoted": _("Quoted text"), "prompt_enable": _("Enable prompt"),
    "prompt_user": _("User prompt"), "interface": _("Interface"), "wwn": _("WWN"),
    "ip": _("IP address"), "netmask": _("Netmask"), "mac": _("MAC address"),
    "version": _("Version"), "serial": _("Serial number"), "good": _("Good"),
    "bad": _("Bad"), "misc": _("Miscellaneous"), "traffic": _("Traffic"),
    "important": _("Important"), "secret": _("Secret"), "acl_zero": _("ACL, no hits"),
    "acl_hit": _("ACL, hits"), "acl": _("ACL"), "remark": _("Remark"),
    "progress_active": _("Progress, active"), "progress": _("Progress"),
    "services": _("Services"), "syslog_header": _("Syslog header"), "syslog": _("Syslog"),
}

SAMPLE_TEXT = """\
SW1#show ip interface brief
Interface              IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0     10.0.0.1        YES NVRAM  up                    up
GigabitEthernet0/1     unassigned      YES unset  administratively down down
Vlan10                 192.168.10.1    YES manual up                    up
SW1#show running-config | section interface
interface GigabitEthernet0/0
 description Uplink to core
 ip address 10.0.0.1 255.255.255.252
 switchport trunk allowed vlan 10,20
 ip access-group MGMT in
!
 Hardware is Gigabit Ethernet, address is 0011.2233.4455 (bia 0011.2233.4455)
access-list 101 permit tcp any host 10.0.0.5 eq 22 (hitcnt=0)
*Mar  1 00:01:02.123: %LINK-3-UPDOWN: Interface GigabitEthernet0/1, changed state to down
*Mar  1 00:01:05.456: %SYS-5-CONFIG_I: Configured from console by admin on vty0 (10.1.1.5)
SW1>"""


def profile_label(profile_id: str) -> str:
    return custom_name(profile_id) or PROFILE_LABELS.get(profile_id, profile_id)


def theme_label(theme_id: str) -> str:
    return custom_name(theme_id) or THEME_LABELS.get(theme_id, theme_id)


_preview_css = Gtk.CssProvider()


class _Preview:
    """Read-only text view painting sample lines with a profile and theme,
    on the colours of the current terminal theme."""

    def __init__(self, config) -> None:
        try:
            terminal = config.get_terminal_profile()
        except Exception:  # a test double or broken theme: fall back to black
            terminal = {}
        self._palette = list(terminal.get("palette") or [])
        background = normalize_color(terminal.get("background")) or "#000000"
        foreground = normalize_color(terminal.get("foreground")) or "#FFFFFF"
        _preview_css.load_from_string(
            "textview.highlight-preview, textview.highlight-preview text "
            f"{{ background-color: {background}; color: {foreground}; }}")
        self.buffer = Gtk.TextBuffer()
        self._tags: Dict[Tuple[object, bool], Gtk.TextTag] = {}
        self.view = Gtk.TextView(buffer=self.buffer, editable=False, cursor_visible=False,
                                 monospace=True, wrap_mode=Gtk.WrapMode.NONE,
                                 top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        self.view.add_css_class("highlight-preview")
        Gtk.StyleContext.add_provider_for_display(
            self.view.get_display(), _preview_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.widget = Gtk.ScrolledWindow(child=self.view, vexpand=True, hexpand=True)
        self.widget.add_css_class("card")
        self.widget.set_overflow(Gtk.Overflow.HIDDEN)

    def _tag(self, colour, bold: bool) -> Optional[Gtk.TextTag]:
        if colour is None and not bold:
            return None
        key = (colour, bold)
        if key not in self._tags:
            tag = self.buffer.create_tag(None)
            if isinstance(colour, int):  # terminal-palette theme: an SGR code
                index = colour - 30 if colour < 90 else colour - 82
                if index < len(self._palette):
                    tag.set_property("foreground", self._palette[index])
            elif colour is not None:
                tag.set_property("foreground", "#%02X%02X%02X" % colour)
            if bold:
                tag.set_property("weight", Pango.Weight.BOLD)
            self._tags[key] = tag
        return self._tags[key]

    def render(self, profile: CustomProfile, theme, lines: List[str]) -> None:
        self.buffer.set_text("")
        end = self.buffer.get_end_iter()
        for number, line in enumerate(lines):
            if number:
                self.buffer.insert(end, "\n")
            for chunk, colour, bold in preview_runs(profile, theme, line):
                tag = self._tag(colour, bold)
                if tag is None:
                    self.buffer.insert(end, chunk)
                else:
                    self.buffer.insert_with_tags(end, chunk, tag)


class _EditorDialog:
    """Shared shell: name, banner for errors, Save / Save as New, preview."""

    kind_title = ""

    def __init__(self, config, *, name: str, editing: Optional[str],
                 on_saved: Callable[[str], None], read_only_hint: bool) -> None:
        self.config = config
        self.editing = editing
        self.on_saved = on_saved
        self._preview_source: Optional[int] = None

        self.dialog = Adw.Dialog()
        self.dialog.set_title(self.kind_title)
        self.dialog.set_content_width(1100)
        self.dialog.set_content_height(760)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_a: self.dialog.close())
        header.pack_start(cancel)
        self.save_button = Gtk.Button(label=_("Save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda *_a: self._save(self.editing))
        header.pack_end(self.save_button)
        self.save_as_button = Gtk.Button(label=_("Save as New"))
        self.save_as_button.set_tooltip_text(_("Keep the original and save a copy under the name above"))
        self.save_as_button.connect("clicked", lambda *_a: self._save(None))
        self.save_as_button.set_visible(editing is not None)
        header.pack_end(self.save_as_button)
        toolbar.add_top_bar(header)
        self.banner = Adw.Banner()
        toolbar.add_top_bar(self.banner)

        self.toasts = Adw.ToastOverlay()
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=520,
                          shrink_start_child=False, shrink_end_child=False)
        self.page = Adw.PreferencesPage()
        paned.set_start_child(self.page)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        heading = Gtk.Label(label=_("Live Preview"), xalign=0)
        heading.add_css_class("heading")
        side.append(heading)
        tests = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        tests.add_css_class("boxed-list")
        self.test_row = Adw.EntryRow(title=_("Test text (paste a line)"))
        self.test_row.connect("changed", lambda *_a: self.schedule_preview())
        tests.append(self.test_row)
        side.append(tests)
        self.preview = _Preview(config)
        side.append(self.preview.widget)
        paned.set_end_child(side)
        self.toasts.set_child(paned)
        toolbar.set_content(self.toasts)
        self.dialog.set_child(toolbar)

        group = Adw.PreferencesGroup()
        if read_only_hint:
            group.set_description(
                _("Built-in entries are read-only: your changes are saved as a new one."))
        self.name_row = Adw.EntryRow(title=_("Name"))
        self.name_row.set_text(name)
        self.name_row.connect("changed", lambda *_a: self.validate())
        group.add(self.name_row)
        self.page.add(group)

    # Subclasses: the edited value, its stored names, and how to save it.
    def value(self):
        raise NotImplementedError

    def taken_names(self) -> List[str]:
        raise NotImplementedError

    def store_value(self, value, replacing: Optional[str]) -> str:
        raise NotImplementedError

    def preview_args(self):
        raise NotImplementedError

    def problem(self) -> Optional[str]:
        """The first reason the value cannot be saved, or None."""
        try:
            check_name(self.name_row.get_text(),
                       [n for n in self.taken_names() if n != self.editing])
        except ValueError as exc:
            return str(exc)
        return None

    def validate(self) -> None:
        problem = self.problem()
        self.banner.set_title(problem or "")
        self.banner.set_revealed(problem is not None)
        self.save_button.set_sensitive(problem is None)
        self.save_as_button.set_sensitive(problem is None)
        self.schedule_preview()

    def schedule_preview(self) -> None:
        if self._preview_source is None:
            self._preview_source = GLib.timeout_add(120, self._update_preview)

    def _update_preview(self) -> bool:
        self._preview_source = None
        lines = SAMPLE_TEXT.splitlines()
        test = self.test_row.get_text()
        if test:
            lines = [test, ""] + lines
        profile, theme = self.preview_args()
        try:
            self.preview.render(profile, theme, lines)
        except ValueError as exc:  # validate() reports it; keep the old preview
            logger.debug("Highlight preview not updated: %s", exc)
        return False

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(message))

    def _save(self, replacing: Optional[str]) -> None:
        try:
            saved_id = self.store_value(self.value(), replacing)
        except (OSError, ValueError) as exc:
            self.banner.set_title(str(exc))
            self.banner.set_revealed(True)
            return
        self.dialog.close()
        self.on_saved(saved_id)

    def present(self, parent: Gtk.Widget) -> None:
        self.validate()
        self.dialog.present(parent)


class HighlightProfileEditor(_EditorDialog):
    kind_title = _("Highlight Profile")

    def __init__(self, config, rules: List[EditableRule], *, name: str,
                 editing: Optional[str], on_saved: Callable[[str], None],
                 read_only_hint: bool = False) -> None:
        super().__init__(config, name=name, editing=editing, on_saved=on_saved,
                         read_only_hint=read_only_hint)
        self.rules = list(rules)
        self.errors: List[Optional[str]] = []
        self._expanded: Optional[int] = None
        self._syncing = False

        group = Adw.PreferencesGroup(title=_("Rules"))
        group.set_description(
            _("Rules apply in order: where two rules match the same text, the later one wins."))
        buttons = Gtk.Box(spacing=6)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add Rule"))
        add.add_css_class("flat")
        add.connect("clicked", self._on_add)
        imp = Gtk.Button(label=_("Import…"),
                         tooltip_text=_("Append rules from a SecureCRT keyword list"))
        imp.add_css_class("flat")
        imp.connect("clicked", self._on_import)
        buttons.append(imp)
        buttons.append(add)
        group.set_header_suffix(buttons)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        group.add(self.list)
        self.page.add(group)
        self._rebuild()

    def value(self) -> CustomProfile:
        return CustomProfile(self.name_row.get_text(), tuple(self.rules))

    def taken_names(self) -> List[str]:
        return list(load_store().profiles)

    def store_value(self, value: CustomProfile, replacing: Optional[str]) -> str:
        return custom_id(save_profile(value, replacing).name)

    def preview_args(self):
        # Preview what is valid so far; invalid rules are flagged on their row.
        rules = tuple(rule if error is None else replace(rule, enabled=False)
                      for rule, error in zip(self.rules, self.errors))
        return CustomProfile("", rules), _current_theme(self.config)

    def problem(self) -> Optional[str]:
        for number, error in enumerate(self.errors, 1):
            if error:
                return _("Rule {number}: {error}").format(number=number, error=error)
        return super().problem()

    # --- rows ---------------------------------------------------------------

    def _rebuild(self) -> None:
        self.list.remove_all()
        self.errors = [self._rule_error(rule) for rule in self.rules]
        for index in range(len(self.rules)):
            self.list.append(self._rule_row(index))
        if not self.rules:
            empty = Adw.ActionRow(title=_("No rules yet: add one or import a keyword list."))
            self.list.append(empty)
        self.validate()

    @staticmethod
    def _rule_error(rule: EditableRule) -> Optional[str]:
        try:
            check_rule(rule)
        except ValueError as exc:
            return str(exc)
        return None

    def _set_row_text(self, row: Adw.ExpanderRow, index: int) -> None:
        rule, error = self.rules[index], self.errors[index]
        row.set_title(f"{index + 1}. {rule.description or rule.pattern or _('(empty)')}")
        row.set_subtitle(error or (rule.pattern if rule.description else ""))
        if error:
            row.add_css_class("error")
        else:
            row.remove_css_class("error")

    def _update(self, index: int, row: Adw.ExpanderRow, **changes) -> None:
        if self._syncing:
            return
        self.rules[index] = replace(self.rules[index], **changes)
        self.errors[index] = self._rule_error(self.rules[index])
        self._set_row_text(row, index)
        self.validate()

    def _rule_row(self, index: int) -> Adw.ExpanderRow:
        rule = self.rules[index]
        row = Adw.ExpanderRow(use_markup=False)
        self._set_row_text(row, index)
        row.set_expanded(index == self._expanded)

        enabled = Gtk.Switch(active=rule.enabled, valign=Gtk.Align.CENTER,
                             tooltip_text=_("Enable rule"))
        enabled.connect("notify::active",
                        lambda sw, _p: self._update(index, row, enabled=sw.get_active()))
        row.add_suffix(enabled)

        pattern = Adw.EntryRow(title=_("Regular expression"), text=rule.pattern)
        pattern.connect("changed", lambda e: self._update(index, row, pattern=e.get_text()))
        row.add_row(pattern)

        description = Adw.EntryRow(title=_("Description (optional)"), text=rule.description)
        description.connect(
            "changed", lambda e: self._update(index, row, description=e.get_text().strip()))
        row.add_row(description)

        row.add_row(self._colour_rows(index, row))

        bold = Adw.SwitchRow(title=_("Bold"), active=rule.bold)
        bold.connect("notify::active", lambda r, _p: self._update(index, row, bold=r.get_active()))
        row.add_row(bold)
        case = Adw.SwitchRow(title=_("Match case"), active=rule.match_case)
        case.connect("notify::active",
                     lambda r, _p: self._update(index, row, match_case=r.get_active()))
        row.add_row(case)

        actions = Adw.ActionRow(title=_("Order"))
        for icon, tip, delta in (("go-up-symbolic", _("Move Up"), -1),
                                 ("go-down-symbolic", _("Move Down"), 1)):
            button = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            button.add_css_class("flat")
            button.set_sensitive(0 <= index + delta < len(self.rules))
            button.connect("clicked", lambda _b, d=delta: self._move(index, d))
            actions.add_suffix(button)
        remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=_("Remove Rule"),
                            valign=Gtk.Align.CENTER)
        remove.add_css_class("flat")
        remove.add_css_class("error")
        remove.connect("clicked", lambda _b: self._remove(index))
        actions.add_suffix(remove)
        row.add_row(actions)
        return row

    def _colour_rows(self, index: int, row: Adw.ExpanderRow) -> Gtk.Widget:
        rule = self.rules[index]
        combo = Adw.ComboRow(title=_("Colour"))
        combo.set_model(Gtk.StringList.new(
            [_("Custom colour")] + [ROLE_LABELS.get(role, role) for role in ROLES]))
        combo.set_subtitle(_("A role follows the colour theme; a custom colour is fixed"))
        dialog = Gtk.ColorDialog(with_alpha=False)
        button = Gtk.ColorDialogButton(dialog=dialog, valign=Gtk.Align.CENTER)
        hex_entry = Gtk.Entry(max_length=7, width_chars=8, valign=Gtk.Align.CENTER,
                              placeholder_text="#RRGGBB", tooltip_text=_("Hex colour #RRGGBB"))
        combo.add_suffix(hex_entry)
        combo.add_suffix(button)

        def show(colour: str) -> None:
            self._syncing = True
            try:
                if colour in ROLES:
                    combo.set_selected(ROLES.index(colour) + 1)
                    shown = _role_hex(self.config, colour)
                else:
                    combo.set_selected(0)
                    shown = colour.upper()
                if shown:
                    button.set_rgba(_rgba(shown))
                    hex_entry.set_text(shown)
            finally:
                self._syncing = False

        def set_colour(colour: str) -> None:
            self._update(index, row, colour=colour)
            show(colour)

        def on_role(*_a) -> None:
            selected = combo.get_selected()
            if not self._syncing and selected > 0:
                set_colour(ROLES[selected - 1])

        def on_hex(entry) -> None:
            colour = normalize_color(entry.get_text())
            if not self._syncing and colour and len(entry.get_text().strip()) == 7:
                set_colour(colour)

        def on_button(*_a) -> None:
            if not self._syncing:
                set_colour(_hex(button.get_rgba()))

        show(rule.colour)
        combo.connect("notify::selected", on_role)
        hex_entry.connect("changed", on_hex)
        button.connect("notify::rgba", on_button)
        return combo

    # --- actions ------------------------------------------------------------

    def _move(self, index: int, delta: int) -> None:
        other = index + delta
        self.rules[index], self.rules[other] = self.rules[other], self.rules[index]
        self._expanded = other
        self._rebuild()

    def _remove(self, index: int) -> None:
        del self.rules[index]
        self._expanded = None
        self._rebuild()

    def _on_add(self, *_a) -> None:
        self.rules.append(EditableRule("", "important"))
        self._expanded = len(self.rules) - 1
        self._rebuild()

    def _on_import(self, *_a) -> None:
        dialog = Gtk.FileDialog(title=_("Import SecureCRT Keyword List"))

        def done(source, result) -> None:
            try:
                path = Path(source.open_finish(result).get_path())
            except GLib.Error:
                return  # cancelled
            imported = [EditableRule(rule.source, rule.role, rule.bold, match_case=rule.match_case)
                        for rule in load_rules(path) if rule.role != "default"]
            self.rules.extend(imported)
            self._rebuild()
            self.toast(_("Imported {count} rules").format(count=len(imported)) if imported
                       else _("No usable rules found in {name}").format(name=path.name))

        dialog.open(self.dialog.get_root(), None, done)


class HighlightThemeEditor(_EditorDialog):
    kind_title = _("Highlight Colour Theme")

    def __init__(self, config, theme: CustomTheme, *, editing: Optional[str],
                 on_saved: Callable[[str], None], read_only_hint: bool = False) -> None:
        super().__init__(config, name=theme.name, editing=editing, on_saved=on_saved,
                         read_only_hint=read_only_hint)
        self.base = theme.base
        self.colours: Dict[str, str] = dict(theme.colours)
        base = CustomTheme("", self.base)
        group = Adw.PreferencesGroup(title=_("Colours by Role"))
        group.set_description(_("Based on {theme}. Changed colours replace the base colour.")
                              .format(theme=THEME_LABELS.get(self.base, self.base)))
        for role in ROLES:
            row = Adw.ActionRow(title=ROLE_LABELS.get(role, role), use_markup=False)
            button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                           valign=Gtk.Align.CENTER)
            entry = Gtk.Entry(max_length=7, width_chars=8, valign=Gtk.Align.CENTER,
                              placeholder_text="#RRGGBB", tooltip_text=_("Hex colour #RRGGBB"))
            reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text=_("Use the base theme colour"))
            reset.add_css_class("flat")
            for widget in (entry, button, reset):
                row.add_suffix(widget)
            self._bind_role(role, base, button, entry, reset)
            group.add(row)
        self.page.add(group)

    def _bind_role(self, role, base, button, entry, reset) -> None:
        state = {"syncing": False}

        def show() -> None:
            colour = self.colours.get(role) or _colour_hex(self.config, theme_colours(base).get(role))
            state["syncing"] = True
            try:
                if colour:
                    button.set_rgba(_rgba(colour))
                    entry.set_text(colour)
                reset.set_sensitive(role in self.colours)
            finally:
                state["syncing"] = False

        def set_colour(colour: Optional[str]) -> None:
            if colour is None:
                self.colours.pop(role, None)
            else:
                self.colours[role] = colour
            show()
            self.schedule_preview()

        def on_entry(widget) -> None:
            colour = normalize_color(widget.get_text())
            if not state["syncing"] and colour and len(widget.get_text().strip()) == 7:
                set_colour(colour)

        button.connect("notify::rgba",
                       lambda *_a: state["syncing"] or set_colour(_hex(button.get_rgba())))
        entry.connect("changed", on_entry)
        reset.connect("clicked", lambda *_a: set_colour(None))
        show()

    def value(self) -> CustomTheme:
        return CustomTheme(self.name_row.get_text(), self.base,
                           tuple((role, self.colours[role]) for role in ROLES
                                 if role in self.colours))

    def taken_names(self) -> List[str]:
        return list(load_store().themes)

    def store_value(self, value: CustomTheme, replacing: Optional[str]) -> str:
        return custom_id(save_theme(value, replacing).name)

    def preview_args(self):
        return _current_profile(self.config), self.value()


# --- helpers ------------------------------------------------------------------


def _colour_hex(config, colour) -> Optional[str]:
    if isinstance(colour, tuple):
        return "#%02X%02X%02X" % colour
    if isinstance(colour, int):  # terminal-palette SGR code
        try:
            palette = config.get_terminal_profile().get("palette") or []
            return str(palette[colour - 30 if colour < 90 else colour - 82]).upper()
        except Exception:
            return None
    return None


def _role_hex(config, role: str) -> Optional[str]:
    return _colour_hex(config, theme_colours(_current_theme(config)).get(role))


def _current_theme(config):
    value = config.get_setting("terminal.highlight_theme", "securecrt")
    name = custom_name(value)
    if name is not None:
        return load_store().themes.get(name, "securecrt")
    return value if value in THEMES else "securecrt"


def _current_profile(config) -> CustomProfile:
    """The selected profile as rules (themes are previewed on SecureCRT rules
    when a legacy profile is selected, since only line-engine profiles use them)."""
    value = config.get_setting("terminal.highlight_profile", "generic")
    name = custom_name(value)
    profile = load_store().profiles.get(name) if name else None
    if profile is not None:
        return profile
    return CustomProfile("", tuple(profile_rules("cisco-securecrt")))


def _copy_name(label: str, taken: List[str]) -> str:
    name = _("{name} (copy)").format(name=label)
    number = 2
    while any(name.casefold() == other.casefold() for other in taken):
        name = _("{name} (copy {number})").format(name=label, number=number)
        number += 1
    return name


def open_profile_editor(parent: Gtk.Widget, config, profile_id: str,
                        on_saved: Callable[[str], None],
                        duplicate: bool = False) -> HighlightProfileEditor:
    """Edit a custom profile, or start a copy of a built-in (or of any, with
    ``duplicate``)."""
    store = load_store()
    name = custom_name(profile_id)
    if name is not None and name in store.profiles:
        rules = list(store.profiles[name].rules)
    else:
        name = None
        rules = profile_rules(profile_id if profile_id in PROFILES else "generic")
    editing = None if duplicate else name
    shown = name if editing else _copy_name(profile_label(profile_id), list(store.profiles))
    editor = HighlightProfileEditor(config, rules, name=shown, editing=editing,
                                    on_saved=on_saved, read_only_hint=name is None)
    editor.present(parent)
    return editor


def open_theme_editor(parent: Gtk.Widget, config, theme_id: str,
                      on_saved: Callable[[str], None],
                      duplicate: bool = False) -> HighlightThemeEditor:
    """Edit a custom theme, or start a copy of a built-in (or of any)."""
    store = load_store()
    name = custom_name(theme_id)
    if name is not None and name in store.themes:
        theme = store.themes[name]
    else:
        name = None
        theme = CustomTheme("", theme_id if theme_id in THEMES else "securecrt")
    editing = None if duplicate else name
    if not editing:
        theme = replace(theme, name=_copy_name(theme_label(theme_id), list(store.themes)))
    editor = HighlightThemeEditor(config, theme, editing=editing, on_saved=on_saved,
                                  read_only_hint=name is None)
    editor.present(parent)
    return editor


def confirm_delete(parent: Gtk.Widget, kind: str, name: str,
                   on_deleted: Callable[[], None]) -> None:
    """Ask, then delete the custom profile (``kind="profile"``) or theme."""
    dialog = Adw.AlertDialog(
        heading=_("Delete “{name}”?").format(name=name),
        body=_("Terminals using it switch to the built-in default."),
    )
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("delete", _("Delete"))
    dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_dialog, response: str) -> None:
        if response != "delete":
            return
        try:
            (delete_profile if kind == "profile" else delete_theme)(name)
        except OSError as exc:
            logger.error("Cannot delete highlight %s %r: %s", kind, name, exc)
            return
        on_deleted()

    dialog.connect("response", on_response)
    dialog.present(parent)
