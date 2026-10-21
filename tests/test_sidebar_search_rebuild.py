"""Sidebar search only rebuilds the list when the effective query changes.

The rebuild tears down and recreates every row, so with hundreds of
connections an edit that leaves the filter unchanged (case, surrounding
spaces) must not trigger one. GtkSearchEntry's own search-delay already
debounces the signal itself. The query is recorded by the rebuild, so a
rebuild started elsewhere (Esc, closing or toggling search) is honoured.
"""

from types import SimpleNamespace

from sshpilot.window import MainWindow


class _Entry:
    def __init__(self):
        self.text = ""

    def get_text(self):
        return self.text


def _window():
    window = MainWindow.__new__(MainWindow)
    window.search_entry = _Entry()
    window.shown = []

    def steps():
        window.shown.append(window.search_entry.get_text().strip().lower())
        return iter(())

    window._rebuild_connection_list_steps = steps
    window._cancel_pending_connection_rows = lambda: None
    window._on_connection_rows_completed = lambda: None
    window.connection_list = SimpleNamespace(get_row_at_index=lambda _i: None)
    return window


def _type(window, text):
    """The user edits the entry; GTK then emits search-changed."""
    window.search_entry.text = text
    MainWindow.on_search_changed(window, window.search_entry)


def _rebuild_with_cleared_entry(window):
    """What on_search_stopped / _close_search_if_open / focus_* do."""
    window.search_entry.text = ""
    window.rebuild_connection_list()


def test_unchanged_effective_query_skips_the_rebuild():
    window = _window()

    for text in ("web", "web ", " WEB", "Web"):
        _type(window, text)

    assert window.shown == ["web"]


def test_every_real_query_change_rebuilds():
    window = _window()

    for text in ("w", "we", "web", "we", ""):
        _type(window, text)

    assert window.shown == ["w", "we", "web", "we", ""]


def test_search_changed_after_a_programmatic_clear_is_not_rebuilt_twice():
    window = _window()
    _type(window, "web")

    _rebuild_with_cleared_entry(window)
    _type(window, "")  # the search-changed that set_text('') emits

    assert window.shown == ["web", ""]


def test_same_query_after_a_programmatic_clear_rebuilds():
    """The list was rebuilt unfiltered behind the guard's back; re-entering the
    previous query must filter it again rather than be skipped."""
    window = _window()
    _type(window, "web")
    _rebuild_with_cleared_entry(window)

    _type(window, "web")

    assert window.shown == ["web", "", "web"]
