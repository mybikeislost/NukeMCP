"""Small shims for Nuke Python API differences across versions.

`nuke.UndoGroup` -- the context-manager convenience wrapper around
`nuke.Undo.begin()`/`nuke.Undo.end()` -- does not exist on Nuke 17.0v1
(confirmed live: `hasattr(nuke, "UndoGroup")` is False there), even though
every handler in this package was written against it. `nuke.Undo` itself
(the lower-level begin/end API `UndoGroup` used to wrap) is present on
every version that matters here, so `undo_group()` below reimplements the
same context-manager behavior directly against that instead of depending
on whichever convenience wrapper a given Nuke version happens to ship.
"""

import contextlib


@contextlib.contextmanager
def undo_group(name):
    """Drop-in replacement for `with nuke.UndoGroup(name):` -- groups
    everything inside into one undo/redo step, without depending on
    `nuke.UndoGroup` actually existing.
    """
    import nuke

    nuke.Undo.begin(name)
    try:
        yield
    finally:
        nuke.Undo.end()
