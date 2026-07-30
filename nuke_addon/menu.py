"""NukeMCP addon entry point.

Nuke auto-loads this file from any directory on NUKE_PATH. Install by
adding this addon's directory (nuke_addon/) to NUKE_PATH -- see
docs/INSTALL.md.
"""

import os

import nuke

from nukemcp_server import listener

_nuke_menu = nuke.menu("Nuke")

# Insert NukeMCP just before "Help" so Help stays the last item on the menu
# bar, matching Nuke's own convention. Falls back to appending at the end
# (index=None) if "Help" isn't found for some reason.
_help_index = next(
    (i for i, item in enumerate(_nuke_menu.items()) if item.name() == "Help"), None
)
_menu = _nuke_menu.addMenu("NukeMCP", index=_help_index)
_menu.addCommand("Start Server", listener.start_listener)
_menu.addCommand("Stop Server", listener.stop_listener)
_menu.addCommand("Status", listener.show_status)

if os.environ.get("NUKEMCP_AUTOSTART", "1") != "0":
    try:
        listener.start_listener()
    except Exception as exc:
        nuke.tprint("[NukeMCP] auto-start failed: {}".format(exc))
