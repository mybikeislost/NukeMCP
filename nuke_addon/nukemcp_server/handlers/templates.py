"""create_workflow_template — instantiate a fully-wired compositing subgraph
from a named recipe in a single undoable operation.

Available templates
-------------------
keying          Keyer -> Unpremult -> Grade (despill) -> Premult -> EdgeBlur
color_correction  Unpremult -> Grade (overall) -> Grade (shadows) ->
                  Grade (highlights) -> Premult
lens_distortion LensDistortion (undistort) -> NoOp -> LensDistortion (redistort)
3d_simple       Camera + Card -> Scene -> ScanlineRender
"""

import ctypes

import nuke

from ..dispatch import register_handler
from ..nuke_compat import undo_group

# ---------------------------------------------------------------------------
# Shared layout helpers
# ---------------------------------------------------------------------------

_NODE_W  = 80
_NODE_H  = 17
_ROW_GAP = 80
_PAD     = 40
_LABEL_H = 50


def _tile_color(r, g, b):
    packed = (r << 24) | (g << 16) | (b << 8) | 0xFF
    return ctypes.c_int32(packed).value


def _make_node(node_class, label=None, knobs=None, xpos=0, ypos=0):
    node = nuke.createNode(node_class, inpanel=False)
    if label:
        try:
            node["label"].setValue(label)
        except Exception:
            pass
    for k, v in (knobs or {}).items():
        try:
            node[k].setValue(v)
        except Exception:
            pass
    node.setXpos(xpos)
    node.setYpos(ypos)
    return node


def _make_backdrop(nodes, label, color):
    if not nodes:
        return None
    min_x = min(n.xpos() for n in nodes)
    max_x = max(n.xpos() for n in nodes)
    min_y = min(n.ypos() for n in nodes)
    max_y = max(n.ypos() for n in nodes)
    bd = nuke.createNode("BackdropNode", inpanel=False)
    bd.setXpos(min_x - _PAD)
    bd.setYpos(min_y - _PAD - _LABEL_H)
    bd["bdwidth"].setValue(max_x - min_x + _NODE_W + _PAD * 2)
    bd["bdheight"].setValue(max_y - min_y + _NODE_H + _PAD * 2 + _LABEL_H)
    bd["label"].setValue(label)
    bd["note_font_size"].setValue(42)
    bd["tile_color"].setValue(color)
    return bd


# ---------------------------------------------------------------------------
# Template implementations
# ---------------------------------------------------------------------------

def _build_keying(root_x, root_y, connect_to, add_backdrop):
    """Keyer -> Unpremult -> Grade (despill) -> Premult -> EdgeBlur"""
    specs = [
        ("Keyer",    "KEY: Primary",          {}),
        ("Unpremult","PREP: Unpremult",        {}),
        ("Grade",    "COLOR: Despill",         {}),
        ("Premult",  "PREP: Premult",          {}),
        ("EdgeBlur", "FX: Edge Refinement",    {"size": 2.0}),
    ]
    nodes = []
    for i, (cls, label, knobs) in enumerate(specs):
        nodes.append(_make_node(cls, label=label, knobs=knobs,
                                xpos=root_x, ypos=root_y + i * _ROW_GAP))

    # Wire linear chain
    for i in range(1, len(nodes)):
        nodes[i].setInput(0, nodes[i - 1])

    if connect_to is not None:
        nodes[0].setInput(0, connect_to)

    bd = None
    if add_backdrop:
        bd = _make_backdrop(nodes, "Keying Rig",
                            _tile_color(107, 107, 0))  # olive
    return nodes, bd


def _build_color_correction(root_x, root_y, connect_to, add_backdrop):
    """Unpremult -> Grade (overall) -> Grade (shadows) -> Grade (highlights) -> Premult"""
    specs = [
        ("Unpremult", "PREP: Unpremult",         {}),
        ("Grade",     "COLOR: Overall",           {}),
        ("Grade",     "COLOR: Shadows",           {}),
        ("Grade",     "COLOR: Highlights",        {}),
        ("Premult",   "PREP: Premult",            {}),
    ]
    nodes = []
    for i, (cls, label, knobs) in enumerate(specs):
        nodes.append(_make_node(cls, label=label, knobs=knobs,
                                xpos=root_x, ypos=root_y + i * _ROW_GAP))

    for i in range(1, len(nodes)):
        nodes[i].setInput(0, nodes[i - 1])

    if connect_to is not None:
        nodes[0].setInput(0, connect_to)

    bd = None
    if add_backdrop:
        bd = _make_backdrop(nodes, "Color Correction Rig",
                            _tile_color(115, 60, 0))   # brown
    return nodes, bd


def _build_lens_distortion(root_x, root_y, connect_to, add_backdrop):
    """LensDistortion (undistort) -> NoOp (processing) -> LensDistortion (redistort)"""
    specs = [
        ("LensDistortion", "LENS: Undistort",       {"direction": "undistort"}),
        ("NoOp",           "PROCESSING",             {}),
        ("LensDistortion", "LENS: Redistort",        {"direction": "distort"}),
    ]
    nodes = []
    for i, (cls, label, knobs) in enumerate(specs):
        nodes.append(_make_node(cls, label=label, knobs=knobs,
                                xpos=root_x, ypos=root_y + i * _ROW_GAP))

    for i in range(1, len(nodes)):
        nodes[i].setInput(0, nodes[i - 1])

    if connect_to is not None:
        nodes[0].setInput(0, connect_to)

    bd = None
    if add_backdrop:
        bd = _make_backdrop(nodes, "Lens Distortion Rig",
                            _tile_color(78, 54, 128))  # purple
    return nodes, bd


def _build_3d_simple(root_x, root_y, connect_to, add_backdrop):
    """Camera + Card -> Scene -> ScanlineRender

    Layout:
      Camera (left)   Card (right)
            Scene (centre)
          ScanlineRender
    """
    col_offset = 120  # horizontal gap between Camera and Card

    camera = _make_node("Camera",        xpos=root_x,              ypos=root_y)
    card   = _make_node("Card",          xpos=root_x + col_offset, ypos=root_y)
    scene  = _make_node("Scene",         xpos=root_x + col_offset // 2,
                                         ypos=root_y + _ROW_GAP)
    render = _make_node("ScanlineRender",xpos=root_x + col_offset // 2,
                                         ypos=root_y + _ROW_GAP * 2)

    # Camera -> Scene input 0, Card -> Scene input 1
    scene.setInput(0, camera)
    scene.setInput(1, card)
    render.setInput(0, scene)

    # Connect external input to Card (the image being projected)
    if connect_to is not None:
        card.setInput(0, connect_to)

    nodes = [camera, card, scene, render]
    bd = None
    if add_backdrop:
        bd = _make_backdrop(nodes, "3D Simple Rig",
                            _tile_color(52, 105, 178))  # blue
    return nodes, bd


# ---------------------------------------------------------------------------
# Dispatch table and handler
# ---------------------------------------------------------------------------

_BUILDERS = {
    "keying":           _build_keying,
    "color_correction": _build_color_correction,
    "lens_distortion":  _build_lens_distortion,
    "3d_simple":        _build_3d_simple,
}

AVAILABLE_TEMPLATES = sorted(_BUILDERS.keys())


@register_handler("create_workflow_template")
def create_workflow_template(params):
    """Instantiate a pre-wired compositing subgraph from a named recipe."""
    template_type = str(params.get("template_type", "")).lower()
    builder = _BUILDERS.get(template_type)
    if builder is None:
        raise ValueError(
            "unknown template {!r} -- available: {}".format(
                template_type, ", ".join(AVAILABLE_TEMPLATES)
            )
        )

    root_x    = int(params.get("xpos", 0))
    root_y    = int(params.get("ypos", 0))
    add_bd    = bool(params.get("add_backdrop", True))

    # Resolve optional connect_to node
    connect_to = None
    connect_name = params.get("connect_to")
    if connect_name:
        connect_to = nuke.toNode(connect_name)
        if connect_to is None:
            raise LookupError("no such node: {!r}".format(connect_name))

    with undo_group("NukeMCP: create_workflow_template ({})".format(template_type)):
        nodes, bd = builder(root_x, root_y, connect_to, add_bd)

    return {
        "template": template_type,
        "nodes_created": [n.name() for n in nodes],
        "backdrop": bd.name() if bd else None,
        "connect_to": connect_name,
    }
