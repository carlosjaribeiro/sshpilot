"""Shared parts of the highlight profile and theme editors.

The editors are opened from Settings → Terminal → Highlighting (see
``highlight_profile_editor`` and ``highlight_theme_editor``). Built-in profiles
and themes are read-only: editing one starts a copy saved under a new name.
The model, validation and storage live in the GTK-free ``terminal_highlight*``
modules; after a save the caller selects the result through the settings, so
terminals pick it up by the usual ``setting-changed`` path.
"""

from __future__ import annotations

import logging
from gettext import gettext as _
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from .custom_terminal_themes import normalize_color
from .terminal_highlight import PROFILES, THEMES, preview_lines, profile_rules
from .terminal_highlight_custom import (
    MAX_NAME_LENGTH,
    PROFILE_LABELS,
    THEME_LABELS,
    CustomProfile,
    check_name,
    custom_name,
    delete_profile,
    delete_theme,
    load_store,
    reserved_names,
    theme_colours,
)

logger = logging.getLogger(__name__)

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
    return custom_name(profile_id) or _(PROFILE_LABELS.get(profile_id, profile_id))


def theme_label(theme_id: str) -> str:
    return custom_name(theme_id) or _(THEME_LABELS.get(theme_id, theme_id))


def plain_toast(message: str, timeout: int = 5) -> Adw.Toast:
    """A toast showing ``message`` literally (Adw.Toast parses markup by default)."""
    toast = Adw.Toast.new(message)
    toast.set_use_markup(False)
    toast.set_timeout(timeout)
    return toast


def set_max_length(row: Adw.EntryRow, length: int) -> None:
    row.get_delegate().set_max_length(length)


def colour_hex(config, colour) -> Optional[str]:
    """``#RRGGBB`` for a theme colour (RGB or palette SGR code), if known."""
    if isinstance(colour, tuple):
        return "#%02X%02X%02X" % colour
    if isinstance(colour, int):  # terminal-palette SGR code
        try:
            palette = config.get_terminal_profile().get("palette") or []
            return normalize_color(palette[colour - 30 if colour < 90 else colour - 82])
        except Exception:
            return None
    return None


def current_theme(config):
    value = config.get_setting("terminal.highlight_theme", "securecrt")
    name = custom_name(value)
    if name is not None:
        return load_store().themes.get(name, "securecrt")
    return value if value in THEMES else "securecrt"


def role_hex(config, role: str) -> Optional[str]:
    return colour_hex(config, theme_colours(current_theme(config)).get(role))


def current_profile(config) -> CustomProfile:
    """The selected profile as rules, for previewing a theme."""
    value = config.get_setting("terminal.highlight_profile", "generic")
    name = custom_name(value)
    profile = load_store().profiles.get(name) if name else None
    if profile is not None:
        return profile
    return CustomProfile("", tuple(profile_rules(value if value in PROFILES else "generic")))


def copy_name(label: str, taken: List[str]) -> str:
    name = _("{name} (copy)").format(name=label)
    number = 2
    while any(name.casefold() == other.casefold() for other in taken):
        name = _("{name} (copy {number})").format(name=label, number=number)
        number += 1
    return name


_preview_css: Optional[Gtk.CssProvider] = None


class Preview:
    """Read-only text view painting sample lines with a profile and theme,
    on the colours of the current terminal theme."""

    def __init__(self, config) -> None:
        global _preview_css
        try:
            terminal = config.get_terminal_profile()
        except Exception:  # a test double or broken theme: fall back to black
            terminal = {}
        self._palette = list(terminal.get("palette") or [])
        background = normalize_color(terminal.get("background")) or "#000000"
        foreground = normalize_color(terminal.get("foreground")) or "#FFFFFF"
        self.buffer = Gtk.TextBuffer()
        self._tags: Dict[Tuple[object, bool], Gtk.TextTag] = {}
        self.view = Gtk.TextView(buffer=self.buffer, editable=False, cursor_visible=False,
                                 monospace=True, wrap_mode=Gtk.WrapMode.NONE,
                                 top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        self.view.add_css_class("highlight-preview")
        self.view.update_property([Gtk.AccessibleProperty.LABEL], [_("Live Preview")])
        if _preview_css is None:  # one provider; reloaded with the current colours
            _preview_css = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                self.view.get_display(), _preview_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _preview_css.load_from_string(
            "textview.highlight-preview, textview.highlight-preview text "
            f"{{ background-color: {background}; color: {foreground}; }}")
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
        painted = preview_lines(profile, theme, lines)  # raises before clearing
        self.buffer.set_text("")
        end = self.buffer.get_end_iter()
        for number, runs in enumerate(painted):
            if number:
                self.buffer.insert(end, "\n")
            for chunk, colour, bold in runs:
                tag = self._tag(colour, bold)
                if tag is None:
                    self.buffer.insert(end, chunk)
                else:
                    self.buffer.insert_with_tags(end, chunk, tag)


class EditorDialog:
    """Shared shell: name, error banner, Save / Save as New, live preview,
    and a confirmation before unsaved changes are discarded."""

    kind_title = ""
    labels: Dict[str, str] = {}

    def __init__(self, config, *, name: str, editing: Optional[str],
                 on_saved: Callable[[str], None], read_only_hint: bool) -> None:
        self.config = config
        self.editing = editing
        self.on_saved = on_saved
        self._preview_source: Optional[int] = None
        self._initial = None

        self.dialog = Adw.Dialog(can_close=False)
        self.dialog.set_title(self.kind_title)
        self.dialog.connect("close-attempt", self._on_close_attempt)
        self.dialog.connect("closed", self._on_closed)

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
        self.save_as_button.set_tooltip_text(
            _("Keep the original and save a copy under a new name"))
        self.save_as_button.connect("clicked", lambda *_a: self._save(None))
        self.save_as_button.set_visible(editing is not None)
        header.pack_end(self.save_as_button)
        toolbar.add_top_bar(header)
        self.banner = Adw.Banner(use_markup=False)
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
        self.preview = Preview(config)
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
        set_max_length(self.name_row, MAX_NAME_LENGTH)
        self.name_row.set_text(name)
        self.name_row.connect("changed", lambda *_a: self.validate())
        group.add(self.name_row)
        self.page.add(group)

    # Subclasses: the edited value, its stored names, and how to save it.
    def value(self):
        raise NotImplementedError

    def stored_names(self) -> List[str]:
        raise NotImplementedError

    def store_value(self, value, replacing: Optional[str]) -> str:
        raise NotImplementedError

    def preview_args(self):
        raise NotImplementedError

    def problem(self) -> Optional[str]:
        """The first reason the value cannot be saved, or None."""
        try:
            check_name(self.name_row.get_text(),
                       [*(n for n in self.stored_names() if n != self.editing),
                        *reserved_names(self.labels)])
        except ValueError as exc:
            return str(exc)
        return None

    def validate(self) -> None:
        problem = self.problem()
        self.banner.set_title(problem or "")
        self.banner.set_revealed(problem is not None)
        self.save_button.set_sensitive(problem is None)
        # Save as New needs a name no saved entry has, the edited one included.
        name = self.name_row.get_text().strip().casefold()
        self.save_as_button.set_sensitive(
            problem is None and all(name != n.casefold() for n in self.stored_names()))
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
        self.toasts.add_toast(plain_toast(message))

    def _save(self, replacing: Optional[str]) -> None:
        try:
            saved_id = self.store_value(self.value(), replacing)
        except (OSError, ValueError) as exc:
            self.banner.set_title(str(exc))
            self.banner.set_revealed(True)
            return
        self.dialog.force_close()
        self.on_saved(saved_id)

    def _on_close_attempt(self, _dialog) -> None:
        if self.value() == self._initial:
            self.dialog.force_close()
            return
        alert = Adw.AlertDialog(heading=_("Discard Changes?"),
                                body=_("Your changes have not been saved."))
        alert.add_response("keep", _("Keep Editing"))
        alert.add_response("discard", _("Discard"))
        alert.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("keep")
        alert.set_close_response("keep")
        alert.connect("response", lambda _a, response: response == "discard"
                      and self.dialog.force_close())
        alert.present(self.dialog)

    def _on_closed(self, _dialog) -> None:
        if self._preview_source is not None:
            GLib.source_remove(self._preview_source)
            self._preview_source = None

    def present(self, parent: Gtk.Widget) -> None:
        root = parent.get_root() or parent
        width, height = root.get_width(), root.get_height()
        # Fill most of a small window, at most 1100x760.
        self.dialog.set_content_width(min(1100, max(360, width - 48)) if width else 1100)
        self.dialog.set_content_height(min(760, max(360, height - 48)) if height else 760)
        self._initial = self.value()
        self.validate()
        self.dialog.present(parent)


def confirm_delete(parent: Gtk.Widget, kind: str, name: str,
                   on_deleted: Callable[[], None], on_error: Callable[[str], None]) -> None:
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
        except (OSError, ValueError) as exc:
            logger.error("Cannot delete highlight %s %r: %s", kind, name, exc)
            on_error(_("Cannot delete “{name}”: {error}").format(name=name, error=exc))
            return
        on_deleted()

    dialog.connect("response", on_response)
    dialog.present(parent)
