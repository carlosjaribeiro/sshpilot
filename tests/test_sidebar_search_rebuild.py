"""Sidebar search only rebuilds the list when the effective query changes.

The rebuild tears down and recreates every row, so with hundreds of
connections an edit that leaves the filter unchanged (case, surrounding
spaces) must not trigger one. GtkSearchEntry's own search-delay already
debounces the signal itself.
"""

from types import SimpleNamespace

from sshpilot.window import MainWindow


def _window():
    window = MainWindow.__new__(MainWindow)
    window.rebuilds = 0

    def rebuild():
        window.rebuilds += 1

    window.rebuild_connection_list = rebuild
    window.connection_list = SimpleNamespace(get_row_at_index=lambda _i: None)
    return window


def _entry(text):
    return SimpleNamespace(get_text=lambda: text)


def test_unchanged_effective_query_skips_the_rebuild():
    window = _window()

    for text in ("web", "web ", " WEB", "Web"):
        MainWindow.on_search_changed(window, _entry(text))

    assert window.rebuilds == 1


def test_every_real_query_change_rebuilds():
    window = _window()

    for text in ("w", "we", "web", "we", ""):
        MainWindow.on_search_changed(window, _entry(text))

    assert window.rebuilds == 5
