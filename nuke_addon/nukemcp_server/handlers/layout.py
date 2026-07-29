"""organize_node_graph — sort all (or selected) nodes into labelled, coloured
backdrop lanes: INPUTS / PREP / KEY / COLOR / FX / MERGE / OUTPUT / MISC."""

import ctypes

import nuke

from ..dispatch import register_handler
from ..nuke_compat import undo_group

# ---------------------------------------------------------------------------
# Category definitions
# ---------------------------------------------------------------------------

_CATEGORY_CLASSES = {
    "INPUTS": frozenset([
        "Read", "ReadGeo", "Camera", "Camera2", "Camera3", "Camera4",
        "Light", "Light2", "Light3", "Axis", "Constant", "CheckerBoard",
        "ColorBars", "Input",
    ]),
    "PREP": frozenset([
        "Reformat", "Crop", "Transform", "CornerPin2D", "Shuffle", "ShuffleCopy",
        "Copy", "Unpremult", "Premult", "Remove", "AddChannels", "ChannelMerge",
        "Invert", "Mirror2", "Flip", "Flop",
    ]),
    "KEY": frozenset([
        "Keyer", "Primatte", "Keylight", "IBKColour", "IBKGizmo",
        "Difference", "Ultimatte",
    ]),
    "COLOR": frozenset([
        "Grade", "ColorCorrect", "HueCorrect", "ColorLookup", "Saturation",
        "Gamma", "Exposure", "Clamp", "Colorspace", "OCIOColorSpace",
        "OCIODisplay", "OCIOFileTransform", "SoftClip",
    ]),
    "FX": frozenset([
        "Blur", "Defocus", "Sharpen", "Median", "EdgeBlur", "ZBlur",
        "Glow", "Flare", "Noise", "Roto", "RotoPaint", "Paint",
        "Text", "Text2", "GridWarp", "SplineWarp", "STMap",
        "LensDistortion", "IDistort",
    ]),
    "MERGE": frozenset([
        "Merge", "Merge2", "Plus", "Screen", "Min", "Max", "Multiply",
        "Exclusion", "From", "Switch", "Dissolve",
        "Matte", "Stencil", "Mask", "Over", "Under", "Atop", "Out",
        "GeomMerge", "DeepMerge",
    ]),
    "OUTPUT": frozenset([
        "Write", "WriteGeo", "Viewer", "DiskCache",
    ]),
}

# Inverted lookup: node class -> category name
_CLASS_TO_CATEGORY = {}
for _cat, _classes in _CATEGORY_CLASSES.items():
    for _cls in _classes:
        _CLASS_TO_CATEGORY[_cls] = _cat

_CATEGORY_ORDER = ["INPUTS", "PREP", "KEY", "COLOR", "FX", "MERGE", "OUTPUT", "MISC"]


def _tile_color(r, g, b):
    """Pack RGB into a Nuke tile_color integer (RGBA with a=255, signed 32-bit)."""
    packed = (r << 24) | (g << 16) | (b << 8) | 0xFF
    return ctypes.c_int32(packed).value


_CATEGORY_COLORS = {
    "INPUTS": _tile_color(52,  105, 178),   # blue
    "PREP":   _tile_color(78,  130,  40),   # green
    "KEY":    _tile_color(107, 107,   0),   # olive
    "COLOR":  _tile_color(115,  60,   0),   # brown
    "FX":     _tile_color(78,   54, 128),   # purple
    "MERGE":  _tile_color(92,   53, 102),   # dark magenta
    "OUTPUT": _tile_color(115,  97,   0),   # dark gold
    "MISC":   _tile_color(46,   52,  54),   # dark gray
}

# Layout geometry
_NODE_W  = 80    # approximate Nuke node tile width in graph coordinates
_NODE_H  = 17    # approximate Nuke node tile height
_COL_GAP = 150   # horizontal gap between category columns
_ROW_GAP = 80    # vertical spacing between nodes in a column
_PAD     = 40    # backdrop padding around the contained nodes
_LABEL_H = 50    # extra space at the top of each backdrop for the label text


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_handler("organize_node_graph")
def organize_node_graph(params):
    """Sort nodes into labelled backdrop lanes by category.

    Nodes are repositioned into parallel vertical columns, one column per
    populated category, left-to-right in the order:
    INPUTS → PREP → KEY → COLOR → FX → MERGE → OUTPUT → MISC.

    A coloured BackdropNode is placed behind each column. BackdropNodes and
    Dot nodes already in the script are left untouched.
    """
    selected_only = bool(params.get("selected_only", False))
    skip_classes = frozenset(["BackdropNode", "Dot", "StickyNote"])

    if selected_only:
        nodes = [n for n in nuke.selectedNodes() if n.Class() not in skip_classes]
    else:
        nodes = [n for n in nuke.allNodes() if n.Class() not in skip_classes]

    if not nodes:
        return {"categories": {}, "backdrops_created": []}

    # Bucket into categories
    buckets = {cat: [] for cat in _CATEGORY_ORDER}
    for node in nodes:
        cat = _CLASS_TO_CATEGORY.get(node.Class(), "MISC")
        buckets[cat].append(node)

    # Only keep populated categories, preserving canonical order
    active_cats = [c for c in _CATEGORY_ORDER if buckets[c]]

    backdrops = []

    with undo_group("NukeMCP: organize_node_graph"):
        x_cursor = 0

        for cat in active_cats:
            cat_nodes = buckets[cat]
            n = len(cat_nodes)

            # Position nodes in a vertical column
            for i, node in enumerate(cat_nodes):
                node.setXpos(x_cursor)
                node.setYpos(i * _ROW_GAP)

            # Column height (top of first node to bottom of last node)
            col_h = (n - 1) * _ROW_GAP + _NODE_H

            # Create a backdrop that encompasses this column
            bd = nuke.createNode("BackdropNode", inpanel=False)
            bd.setXpos(x_cursor - _PAD)
            bd.setYpos(-_PAD - _LABEL_H)
            bd["bdwidth"].setValue(_NODE_W + _PAD * 2)
            bd["bdheight"].setValue(col_h + _PAD * 2 + _LABEL_H)
            bd["label"].setValue(cat)
            bd["note_font_size"].setValue(42)
            bd["tile_color"].setValue(_CATEGORY_COLORS.get(cat, _CATEGORY_COLORS["MISC"]))
            backdrops.append(bd.name())

            x_cursor += _NODE_W + _COL_GAP

    return {
        "categories": {cat: [n.name() for n in buckets[cat]] for cat in active_cats},
        "backdrops_created": backdrops,
        "columns_laid_out": len(active_cats),
    }
