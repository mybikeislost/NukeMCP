import os

import nuke

from ..dispatch import register_handler
from ..nuke_compat import undo_group


def _require_absolute(path):
    if not os.path.isabs(path):
        raise ValueError("path must be absolute: {!r}".format(path))


@register_handler("open_script")
def open_script(params):
    path = params["path"]
    _require_absolute(path)
    dry_run = bool(params.get("dry_run", False))

    if dry_run:
        root = nuke.root()
        modified = bool(root.modified())
        return {
            "dry_run": True,
            "would_open": path,
            "target_exists": os.path.exists(path),
            "current_script": root.name() or None,
            "current_script_modified": modified,
            "current_node_count": len(nuke.allNodes()),
            "warning": (
                "opening will discard the current session -- unsaved changes will be lost"
                if modified else None
            ),
        }

    # scriptOpen() does not raise on a missing file. It logs "Can't read ...:
    # No such file or directory" and then SETS root().name() TO THAT PATH,
    # leaving an empty session named after a file that does not exist. So this
    # returned {"opened": path} for a typo'd path -- and the verification below
    # cannot catch it, because the name it checks is the one it wanted.
    if not os.path.exists(path):
        raise LookupError("no such file: {}".format(path))

    # nuke.scriptOpen() replaces the session only when the session is UNMODIFIED.
    # Over a modified one it SPAWNS A SECOND NUKE INSTANCE and loads the file
    # there, leaving this session untouched -- and the new instance cannot bind
    # the addon's port, so nothing can reach it. The call returns normally
    # either way, so this reported success while nothing had changed.
    #
    # Clearing first gives scriptOpen nothing to preserve, so it loads in place.
    # setModified(False) is NOT enough: measured, it still spawned a second
    # instance. Discarding here matches what dry_run already warns about.
    if nuke.root().modified():
        nuke.scriptClear()

    nuke.scriptOpen(path)

    # Verify rather than assume: root().name() returns exactly the path passed
    # to scriptOpen -- measured against symlinked, dotted and case-altered
    # paths, none of which Nuke normalises -- so an inequality here means the
    # open genuinely did not take effect on THIS session.
    loaded = nuke.root().name() or ""
    if os.path.normpath(loaded) != os.path.normpath(path):
        raise RuntimeError(
            "open did not take effect: this session is still {!r}, not {!r}. "
            "Nuke may have opened the file in a separate instance, which the "
            "addon cannot reach.".format(loaded or "untitled", path)
        )

    return {"opened": path, "node_count": len(nuke.allNodes())}


@register_handler("save_script")
def save_script(params):
    path = params["path"]
    _require_absolute(path)
    overwrote_existing = os.path.exists(path)
    nuke.scriptSaveAs(path, overwrite=1)
    return {"saved": path, "overwrote_existing": overwrote_existing}


@register_handler("merge_script")
def merge_script(params):
    """Import nodes from another .nk file into the current script without replacing it."""
    path = params["path"]
    _require_absolute(path)
    if not os.path.exists(path):
        raise FileNotFoundError("script not found: {!r}".format(path))

    nodes_before = {n.name() for n in nuke.allNodes()}

    with undo_group("NukeMCP: merge_script"):
        nuke.scriptReadFile(path)

    nodes_after = {n.name() for n in nuke.allNodes()}
    new_node_names = sorted(nodes_after - nodes_before)

    return {
        "merged": path,
        "new_nodes": new_node_names,
        "count": len(new_node_names),
    }
