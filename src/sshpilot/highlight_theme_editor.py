"""Edit a highlight colour theme: a built-in base plus per-role colours.

See ``highlight_editor_common`` for the dialog shell.
"""

from __future__ import annotations

from dataclasses import replace
from gettext import gettext as _
from typing import Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from .custom_terminal_themes import normalize_color
from .highlight_editor_common import (
    ROLE_LABELS,
    EditorDialog,
    colour_hex,
    copy_name,
    current_profile,
    theme_label,
)
from .terminal_highlight import THEMES
from .terminal_highlight_custom import (
    THEME_LABELS,
    ROLES,
    CustomTheme,
    custom_id,
    custom_name,
    load_store,
    save_theme,
    theme_colours,
)
from .terminal_theme_editor import _hex, _rgba


class HighlightThemeEditor(EditorDialog):
    kind_title = _("Highlight Colour Theme")
    labels = THEME_LABELS

    def __init__(self, config, theme: CustomTheme, *, editing: Optional[str],
                 on_saved: Callable[[str], None], read_only_hint: bool = False) -> None:
        super().__init__(config, name=theme.name, editing=editing, on_saved=on_saved,
                         read_only_hint=read_only_hint)
        self.base = theme.base
        self.colours: Dict[str, str] = dict(theme.colours)
        base_colours = theme_colours(self.base)
        group = Adw.PreferencesGroup(title=_("Colours by Role"))
        group.set_description(_("Based on {theme}. Changed colours replace the base colour.")
                              .format(theme=_(THEME_LABELS.get(self.base, self.base))))
        for role in ROLES:
            label = ROLE_LABELS.get(role, role)
            row = Adw.ActionRow(title=label, use_markup=False)
            button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                           valign=Gtk.Align.CENTER)
            button.update_property([Gtk.AccessibleProperty.LABEL],
                                   [_("{role} colour").format(role=label)])
            entry = Gtk.Entry(max_length=7, width_chars=8, valign=Gtk.Align.CENTER,
                              placeholder_text="#RRGGBB", tooltip_text=_("Hex colour #RRGGBB"))
            entry.update_property([Gtk.AccessibleProperty.LABEL],
                                  [_("{role} colour as #RRGGBB").format(role=label)])
            reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text=_("Use the base theme colour"))
            reset.update_property([Gtk.AccessibleProperty.LABEL],
                                  [_("Use the base theme colour for {role}").format(role=label)])
            reset.add_css_class("flat")
            for widget in (entry, button, reset):
                row.add_suffix(widget)
            self._bind_role(role, colour_hex(config, base_colours.get(role)),
                            button, entry, reset)
            group.add(row)
        self.page.add(group)

    def _bind_role(self, role: str, base: Optional[str], button, entry, reset) -> None:
        state = {"syncing": False}

        def show() -> None:
            colour = self.colours.get(role) or base
            state["syncing"] = True
            try:
                if colour:
                    button.set_rgba(_rgba(colour))
                entry.set_text(colour or "")
                reset.set_sensitive(role in self.colours)
            finally:
                state["syncing"] = False

        def set_colour(colour: Optional[str]) -> None:
            if colour is None:
                self.colours.pop(role, None)
            else:
                self.colours[role] = colour
            show()
            self.validate()

        def on_entry(widget) -> None:
            text = widget.get_text().strip()
            colour = normalize_color(text)
            if not state["syncing"] and colour and len(text) == 7:
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

    def stored_names(self) -> List[str]:
        return list(load_store().themes)

    def store_value(self, value: CustomTheme, replacing: Optional[str]) -> str:
        return custom_id(save_theme(value, replacing).name)

    def preview_args(self):
        return current_profile(self.config), self.value()


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
        theme = replace(theme, name=copy_name(theme_label(theme_id), list(store.themes)))
    editor = HighlightThemeEditor(config, theme, editing=editing, on_saved=on_saved,
                                  read_only_hint=name is None)
    editor.present(parent)
    return editor
