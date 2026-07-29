import nuke

from ..dispatch import register_handler
from ..nuke_compat import undo_group


def _active_viewer_node():
    """Return the active Viewer node, or the first one found, or None."""
    try:
        av = nuke.activeViewer()
        if av:
            return av.node()
    except Exception:
        pass
    viewers = nuke.allNodes("Viewer")
    return viewers[0] if viewers else None


@register_handler("create_viewer")
def create_viewer(params):
    """Create a Viewer node, optionally connected to an existing node."""
    connect_name = params.get("connect_to")
    xpos = params.get("xpos")
    ypos = params.get("ypos")

    with undo_group("NukeMCP: create_viewer"):
        viewer = nuke.createNode("Viewer", inpanel=False)
        if xpos is not None:
            viewer.setXpos(int(xpos))
        if ypos is not None:
            viewer.setYpos(int(ypos))

        if connect_name:
            connect_node = nuke.toNode(connect_name)
            if connect_node is None:
                raise LookupError("no such node: {!r}".format(connect_name))
            viewer.setInput(0, connect_node)

    return {
        "name": viewer.name(),
        "connected_to": connect_name,
        "xpos": viewer.xpos(),
        "ypos": viewer.ypos(),
    }


@register_handler("get_viewer_node")
def get_viewer_node(params):
    viewer = _active_viewer_node()
    if viewer is None:
        return {"viewer": None, "message": "no Viewer node in the script"}

    result = {
        "name": viewer.name(),
        "current_input": None,
        "frame": None,
        "gain": None,
        "gamma": None,
    }

    input_node = viewer.input(0)
    if input_node:
        result["current_input"] = input_node.name()

    try:
        result["frame"] = int(nuke.frame())
    except Exception:
        pass
    for knob_name in ("gain", "gamma"):
        try:
            result[knob_name] = viewer[knob_name].value()
        except Exception:
            pass

    return result


@register_handler("set_viewer_input")
def set_viewer_input(params):
    node_name = params["node_name"]
    input_index = int(params.get("input_index", 0))

    node = nuke.toNode(node_name)
    if node is None:
        raise LookupError("no such node: {!r}".format(node_name))

    viewer = _active_viewer_node()
    if viewer is None:
        raise RuntimeError("no Viewer node exists in the script")

    with undo_group("NukeMCP: set_viewer_input"):
        viewer.setInput(input_index, node)

    return {
        "viewer": viewer.name(),
        "input_index": input_index,
        "connected_to": node.name(),
    }


@register_handler("zoom_to_node")
def zoom_to_node(params):
    node_name = params["node_name"]
    node = nuke.toNode(node_name)
    if node is None:
        raise LookupError("no such node: {!r}".format(node_name))

    # Centre on the node; 80×17 is the approximate size of a standard node tile.
    x = node.xpos() + 40
    y = node.ypos() + 8

    try:
        nuke.zoom(1.0, [x, y])
    except Exception:
        pass  # zoom() may not work outside an interactive session; not fatal

    return {"node": node_name, "centered_at": [x, y]}


@register_handler("viewer_playback")
def viewer_playback(params):
    """Control Viewer playback: play forward, stop, step one frame, or jump to a frame.

    Continuous play/stop uses nuke.activeViewer() methods (Nuke 13+).
    Frame stepping and goto are always reliable via nuke.frame().
    """
    action = str(params.get("action", "play")).lower()
    first = int(nuke.root()["first_frame"].value())
    last = int(nuke.root()["last_frame"].value())
    viewer = nuke.activeViewer()

    if action in ("play", "forward"):
        if viewer is not None:
            try:
                viewer.play(1)
            except (AttributeError, TypeError):
                try:
                    viewer.playForwards()
                except (AttributeError, TypeError):
                    pass

    elif action in ("play_backward", "backward", "reverse"):
        if viewer is not None:
            try:
                viewer.play(-1)
            except (AttributeError, TypeError):
                try:
                    viewer.playBackwards()
                except (AttributeError, TypeError):
                    pass

    elif action == "stop":
        if viewer is not None:
            try:
                viewer.stop()
            except AttributeError:
                try:
                    viewer.play(0)
                except (AttributeError, TypeError):
                    pass

    elif action == "next":
        current = min(int(nuke.frame()) + 1, last)
        nuke.frame(current)

    elif action == "prev":
        current = max(int(nuke.frame()) - 1, first)
        nuke.frame(current)

    elif action == "goto":
        frame = params.get("frame")
        if frame is None:
            raise ValueError("goto action requires a 'frame' parameter")
        nuke.frame(int(frame))

    else:
        raise ValueError(
            "unknown action {!r} -- use: play, stop, next, prev, goto, "
            "forward, backward".format(action)
        )

    return {"action": action, "frame": int(nuke.frame())}
