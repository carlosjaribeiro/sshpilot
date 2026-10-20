"""Edit a highlight profile: an ordered list of rules (later rules win).

Rows are added, moved and removed in place, so expanded rows, scroll position
and focus survive. See ``highlight_editor_common`` for the dialog shell.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from gettext import gettext as _
from pathlib import Path
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from .custom_terminal_themes import normalize_color
from .highlight_editor_common import (
    ROLE_LABELS,
    EditorDialog,
    copy_name,
    current_theme,
    profile_label,
    role_hex,
    set_max_length,
)
from .terminal_highlight import PROFILES, check_rule, profile_rules
from .terminal_highlight_custom import (
    MAX_DESCRIPTION_LENGTH,
    PROFILE_LABELS,
    ROLES,
    CustomProfile,
    EditableRule,
    custom_id,
    custom_name,
    load_store,
    save_profile,
)
from .terminal_highlight_securecrt import load_rules
from .terminal_theme_editor import _hex, _rgba

logger = logging.getLogger(__name__)

_MAX_PATTERN_LENGTH = 512  # the safety guard's limit
_FALLBACK_COLOUR = "#FFFFFF"  # a custom colour for a role without a known RGB


class _RuleRow:
    """One expandable rule row; its position is its index in ``editor.rows``."""

    def __init__(self, editor: "HighlightProfileEditor", rule: EditableRule,
                 expanded: bool = False) -> None:
        self.editor = editor
        self.rule = rule
        self.error = _rule_error(rule)
        self._syncing = False

        self.row = Adw.ExpanderRow(use_markup=False, expanded=expanded)
        self.enabled = Gtk.Switch(active=rule.enabled, valign=Gtk.Align.CENTER,
                                  tooltip_text=_("Enable rule"))
        self.enabled.update_property([Gtk.AccessibleProperty.LABEL], [_("Enable rule")])
        self.enabled.connect("notify::active",
                             lambda sw, _p: self.update(enabled=sw.get_active()))
        self.row.add_suffix(self.enabled)

        self.pattern = Adw.EntryRow(title=_("Regular expression"), text=rule.pattern)
        set_max_length(self.pattern, _MAX_PATTERN_LENGTH)
        self.pattern.connect("changed", lambda e: self.update(pattern=e.get_text()))
        self.row.add_row(self.pattern)

        description = Adw.EntryRow(title=_("Description (optional)"), text=rule.description)
        set_max_length(description, MAX_DESCRIPTION_LENGTH)
        # Stored as typed; saving strips the ends.
        description.connect("changed", lambda e: self.update(description=e.get_text()))
        self.row.add_row(description)

        self.row.add_row(self._colour_row())

        bold = Adw.SwitchRow(title=_("Bold"), active=rule.bold)
        bold.connect("notify::active", lambda r, _p: self.update(bold=r.get_active()))
        self.row.add_row(bold)
        case = Adw.SwitchRow(title=_("Match case"), active=rule.match_case)
        case.connect("notify::active", lambda r, _p: self.update(match_case=r.get_active()))
        self.row.add_row(case)

        actions = Adw.ActionRow(title=_("Order"))
        self.up, self.down = (self._button(icon, tip, lambda b, d=delta: editor.move(self, d, b))
                              for icon, tip, delta in (("go-up-symbolic", _("Move Up"), -1),
                                                       ("go-down-symbolic", _("Move Down"), 1)))
        remove = self._button("user-trash-symbolic", _("Remove Rule"),
                              lambda _b: editor.remove(self))
        remove.add_css_class("error")
        for button in (self.up, self.down, remove):
            actions.add_suffix(button)
        self.row.add_row(actions)

    @staticmethod
    def _button(icon: str, tip: str, callback) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
        button.update_property([Gtk.AccessibleProperty.LABEL], [tip])
        button.add_css_class("flat")
        button.connect("clicked", callback)
        return button

    def _colour_row(self) -> Adw.ComboRow:
        self.combo = Adw.ComboRow(title=_("Colour"))
        self.combo.set_model(Gtk.StringList.new(
            [_("Custom colour")] + [ROLE_LABELS.get(role, role) for role in ROLES]))
        self.combo.set_subtitle(_("A role follows the colour theme; a custom colour is fixed"))
        self.colour_button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                                   valign=Gtk.Align.CENTER)
        self.colour_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Rule colour")])
        self.hex_entry = Gtk.Entry(max_length=7, width_chars=8, valign=Gtk.Align.CENTER,
                                   placeholder_text="#RRGGBB",
                                   tooltip_text=_("Hex colour #RRGGBB"))
        self.hex_entry.update_property([Gtk.AccessibleProperty.LABEL],
                                       [_("Rule colour as #RRGGBB")])
        self.combo.add_suffix(self.hex_entry)
        self.combo.add_suffix(self.colour_button)
        self._show_colour()
        self.combo.connect("notify::selected", self._on_role)
        self.hex_entry.connect("changed", self._on_hex)
        self.colour_button.connect("notify::rgba", self._on_button)
        return self.combo

    def _show_colour(self) -> None:
        """Mirror the rule's colour in the drop-down, entry and button."""
        colour = self.rule.colour
        shown = colour.upper() if colour not in ROLES else role_hex(self.editor.config, colour)
        self._syncing = True
        try:
            self.combo.set_selected(ROLES.index(colour) + 1 if colour in ROLES else 0)
            if shown:
                self.colour_button.set_rgba(_rgba(shown))
            self.hex_entry.set_text(shown or "")
            # A palette role has no RGB to start a custom colour from.
            self.colour_button.set_sensitive(shown is not None)
            self.hex_entry.set_sensitive(shown is not None)
        finally:
            self._syncing = False

    def _set_colour(self, colour: str) -> None:
        self.update(colour=colour)
        self._show_colour()

    def _on_role(self, *_a) -> None:
        if self._syncing:
            return
        selected = self.combo.get_selected()
        if selected > 0:
            self._set_colour(ROLES[selected - 1])
        elif self.rule.colour in ROLES:  # "Custom colour": start from the role's colour
            self._set_colour(role_hex(self.editor.config, self.rule.colour) or _FALLBACK_COLOUR)

    def _on_hex(self, entry: Gtk.Entry) -> None:
        text = entry.get_text().strip()
        colour = normalize_color(text)
        if not self._syncing and colour and len(text) == 7:
            self._set_colour(colour)

    def _on_button(self, *_a) -> None:
        if not self._syncing:
            self._set_colour(_hex(self.colour_button.get_rgba()))

    def update(self, **changes) -> None:
        if self._syncing:
            return
        self.rule = replace(self.rule, **changes)
        self.error = _rule_error(self.rule)
        self.refresh()
        self.editor.validate()

    def refresh(self) -> None:
        """Title, error and arrows for the row's current position."""
        index, count = self.editor.rows.index(self), len(self.editor.rows)
        rule = self.rule
        self.row.set_title(f"{index + 1}. {rule.description or rule.pattern or _('(empty)')}")
        self.row.set_subtitle(self.error or (rule.pattern if rule.description else ""))
        (self.row.add_css_class if self.error else self.row.remove_css_class)("error")
        self.up.set_sensitive(index > 0)
        self.down.set_sensitive(index < count - 1)


def _rule_error(rule: EditableRule) -> Optional[str]:
    try:
        check_rule(rule)
    except ValueError as exc:
        return str(exc)
    return None


class HighlightProfileEditor(EditorDialog):
    kind_title = _("Highlight Profile")
    labels = PROFILE_LABELS

    def __init__(self, config, rules: List[EditableRule], *, name: str,
                 editing: Optional[str], on_saved: Callable[[str], None],
                 read_only_hint: bool = False) -> None:
        super().__init__(config, name=name, editing=editing, on_saved=on_saved,
                         read_only_hint=read_only_hint)
        self.rows: List[_RuleRow] = []

        group = Adw.PreferencesGroup(title=_("Rules"))
        group.set_description(
            _("Rules apply in order: where two rules match the same text, the later one wins."))
        buttons = Gtk.Box(spacing=6)
        imp = Gtk.Button(label=_("Import…"),
                         tooltip_text=_("Append rules from a SecureCRT keyword list"))
        imp.add_css_class("flat")
        imp.connect("clicked", self._on_import)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add Rule"))
        add.update_property([Gtk.AccessibleProperty.LABEL], [_("Add Rule")])
        add.add_css_class("flat")
        add.connect("clicked", self._on_add)
        buttons.append(imp)
        buttons.append(add)
        group.set_header_suffix(buttons)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        placeholder = Gtk.Label(label=_("No rules yet: add one or import a keyword list."),
                                margin_top=12, margin_bottom=12, wrap=True)
        placeholder.add_css_class("dim-label")
        self.list.set_placeholder(placeholder)
        group.add(self.list)
        self.page.add(group)
        self.append(rules)

    @property
    def rules(self) -> List[EditableRule]:
        return [row.rule for row in self.rows]

    def value(self) -> CustomProfile:
        return CustomProfile(self.name_row.get_text(), tuple(self.rules))

    def stored_names(self) -> List[str]:
        return list(load_store().profiles)

    def store_value(self, value: CustomProfile, replacing: Optional[str]) -> str:
        return custom_id(save_profile(value, replacing).name)

    def preview_args(self):
        # Preview what is valid so far; invalid rules are flagged on their row.
        rules = tuple(row.rule if row.error is None else replace(row.rule, enabled=False)
                      for row in self.rows)
        return CustomProfile("", rules), current_theme(self.config)

    def problem(self) -> Optional[str]:
        for number, row in enumerate(self.rows, 1):
            if row.error:
                return _("Rule {number}: {error}").format(number=number, error=row.error)
        return super().problem()

    # --- row operations, in place ---------------------------------------------

    def _refresh_all(self) -> None:
        for row in self.rows:
            row.refresh()
        self.validate()

    def append(self, rules: List[EditableRule], expanded: bool = False) -> List[_RuleRow]:
        added = [_RuleRow(self, rule, expanded) for rule in rules]
        for row in added:
            self.rows.append(row)
            self.list.append(row.row)
        self._refresh_all()
        return added

    def move(self, row: _RuleRow, delta: int, button: Gtk.Widget) -> None:
        index = self.rows.index(row)
        other = index + delta
        if not 0 <= other < len(self.rows):
            return
        self.rows[index], self.rows[other] = self.rows[other], self.rows[index]
        self.list.remove(row.row)
        self.list.insert(row.row, other)
        self._refresh_all()
        # Keep the keyboard on the arrow, or on its twin at the end of the list.
        target = button if button.get_sensitive() else (row.down if delta < 0 else row.up)
        GLib.idle_add(target.grab_focus)

    def remove(self, row: _RuleRow) -> None:
        index = self.rows.index(row)
        self.rows.remove(row)
        self.list.remove(row.row)
        self._refresh_all()
        if self.rows:
            GLib.idle_add(self.rows[min(index, len(self.rows) - 1)].row.grab_focus)

    def _on_add(self, *_a) -> None:
        (row,) = self.append([EditableRule("", "important")], expanded=True)
        GLib.idle_add(row.pattern.grab_focus)

    def _on_import(self, *_a) -> None:
        dialog = Gtk.FileDialog(title=_("Import SecureCRT Keyword List"))

        def done(source, result) -> None:
            try:
                file = source.open_finish(result)
            except GLib.Error:
                return  # cancelled
            local = file.get_path()
            if local is None:
                self.toast(_("Only local files can be imported."))
                return
            path = Path(local)
            try:
                loaded = load_rules(path)
            except Exception as exc:  # load_rules never raises; be safe anyway
                logger.exception("Cannot import highlight rules from %s", path)
                self.toast(_("Cannot import {name}: {error}").format(name=path.name, error=exc))
                return
            # File order, after the current rules: later rules win, as in the file.
            imported = [EditableRule(rule.source, rule.role, rule.bold, match_case=rule.match_case)
                        for rule in loaded if rule.role != "default"]
            self.append(imported)
            self.toast(_("Imported {count} rules").format(count=len(imported)) if imported
                       else _("No usable rules found in {name}").format(name=path.name))

        dialog.open(self.dialog.get_root(), None, done)


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
    shown = name if editing else copy_name(profile_label(profile_id), list(store.profiles))
    editor = HighlightProfileEditor(config, rules, name=shown, editing=editing,
                                    on_saved=on_saved, read_only_hint=name is None)
    editor.present(parent)
    return editor
