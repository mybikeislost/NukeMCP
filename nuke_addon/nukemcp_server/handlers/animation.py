import nuke

from ..dispatch import register_handler
from ..nuke_compat import undo_group


def _require_node_and_knob(node_name, knob_name):
    node = nuke.toNode(node_name)
    if node is None:
        raise LookupError("no such node: {!r}".format(node_name))
    knob = node.knob(knob_name)
    if knob is None:
        raise LookupError("no such knob {!r} on node {!r}".format(knob_name, node_name))
    return node, knob


@register_handler("set_knob_expression")
def set_knob_expression(params):
    """Set or clear a Nuke expression on a knob."""
    node_name = params["node_name"]
    knob_name = params["knob_name"]
    expression = params.get("expression", "")
    field_index = params.get("field_index")  # for multi-field knobs (e.g. Color)

    _, knob = _require_node_and_knob(node_name, knob_name)

    with undo_group("NukeMCP: set_knob_expression"):
        if expression:
            if field_index is not None:
                knob.setExpression(expression, int(field_index))
            else:
                knob.setExpression(expression)
            cleared = False
        else:
            knob.clearAnimated()
            cleared = True

    return {
        "node": node_name,
        "knob": knob_name,
        "expression": expression,
        "cleared": cleared,
        "field_index": field_index,
    }


@register_handler("set_knob_keyframe")
def set_knob_keyframe(params):
    """Set an animation keyframe on a knob at a specific frame."""
    node_name = params["node_name"]
    knob_name = params["knob_name"]
    frame = float(params["frame"])
    value = float(params["value"])
    field_index = params.get("field_index")  # for multi-field knobs

    _, knob = _require_node_and_knob(node_name, knob_name)

    with undo_group("NukeMCP: set_knob_keyframe"):
        knob.setAnimated() if field_index is None else knob.setAnimated(int(field_index))
        if field_index is not None:
            knob.setValueAt(value, frame, int(field_index))
        else:
            knob.setValueAt(value, frame)

    return {
        "node": node_name,
        "knob": knob_name,
        "frame": frame,
        "value": value,
        "field_index": field_index,
    }


@register_handler("remove_knob_animation")
def remove_knob_animation(params):
    """Remove all animation/expression from a knob, leaving the current value static."""
    node_name = params["node_name"]
    knob_name = params["knob_name"]
    field_index = params.get("field_index")

    _, knob = _require_node_and_knob(node_name, knob_name)

    with undo_group("NukeMCP: remove_knob_animation"):
        if field_index is not None:
            knob.clearAnimated(int(field_index))
        else:
            knob.clearAnimated()

    return {"node": node_name, "knob": knob_name, "animation_cleared": True}
