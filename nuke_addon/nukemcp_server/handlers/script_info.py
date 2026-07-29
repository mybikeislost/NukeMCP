import nuke

from ..dispatch import register_handler
from ..nuke_compat import undo_group


@register_handler("get_script_info")
def get_script_info(params):
    root = nuke.root()
    fmt = root["format"].value()
    return {
        "script_path": root.name(),
        "modified": root.modified(),
        "first_frame": int(root["first_frame"].value()),
        "last_frame": int(root["last_frame"].value()),
        "fps": root["fps"].value(),
        "format": {
            "name": fmt.name(),
            "width": fmt.width(),
            "height": fmt.height(),
        },
    }


@register_handler("set_project_settings")
def set_project_settings(params):
    """Set top-level project settings on the Nuke Root node."""
    root = nuke.root()
    changed = {}

    with undo_group("NukeMCP: set_project_settings"):
        if "first_frame" in params:
            v = int(params["first_frame"])
            root["first_frame"].setValue(v)
            changed["first_frame"] = v

        if "last_frame" in params:
            v = int(params["last_frame"])
            root["last_frame"].setValue(v)
            changed["last_frame"] = v

        if "fps" in params:
            v = float(params["fps"])
            root["fps"].setValue(v)
            changed["fps"] = v

        if "format" in params:
            # Accepts a format name string (e.g. "HD_1080") or "WxH" or "WxH name"
            v = str(params["format"])
            root["format"].setValue(v)
            changed["format"] = v

        if "current_frame" in params:
            v = int(params["current_frame"])
            nuke.frame(v)
            changed["current_frame"] = v

    return {"changed": changed}
