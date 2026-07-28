import os
import tempfile

import nuke

from ..dispatch import register_handler
from ..nuke_compat import undo_group
from .graph import _node_summary

# ---------------------------------------------------------------------------
# Node-class normalisation: map common aliases / misspellings to real names.
# Keys are lower-cased so the lookup is always case-insensitive.
# ---------------------------------------------------------------------------
_NODE_CLASS_ALIASES = {
    "merge":           "Merge2",
    "colorcorrection": "ColorCorrect",
    "color":           "Grade",
    "gaussian":        "Blur",
    "gaussianblur":    "Blur",
    "premultiply":     "Premult",
    "unpremultiply":   "Unpremult",
    "move":            "Transform",
    "position":        "Transform",
    "rectangle":       "Crop",
    "cropnode":        "Crop",
    "output":          "Write",
    "input":           "Read",
    "3dmerge":         "GeomMerge",
    "geometrymerge":   "GeomMerge",
    "scanline":        "ScanlineRender",
    "lensdistort":     "LensDistortion",
    "text":            "Text2",
    # Solid colour sources — "Constant" is the real Nuke class name
    "solidcolor":      "Constant",
    "solid":           "Constant",
    "constantclip":    "Constant",
    "solidcolour":     "Constant",
}


def _normalize_node_class(node_class):
    """Return the canonical Nuke class name, resolving common aliases."""
    return _NODE_CLASS_ALIASES.get(node_class.lower(), node_class)


def _require_node(node_name):
    node = nuke.toNode(node_name)
    if node is None:
        raise LookupError("no such node: {!r}".format(node_name))
    return node


def _apply_knobs(node, knobs):
    applied = []
    errors = {}
    for knob_name, value in (knobs or {}).items():
        knob = node.knob(knob_name)
        if knob is None:
            errors[knob_name] = "no such knob"
            continue
        try:
            if isinstance(value, (list, tuple)):
                # Multi-component knob (AColor, XY, UV, Scale, etc.):
                # setValue(v, i) sets one component at a time.
                for i, v in enumerate(value):
                    knob.setValue(float(v), i)
            else:
                knob.setValue(value)
            applied.append(knob_name)
        except Exception as exc:
            errors[knob_name] = str(exc)
    return applied, errors


# Write-like nodes for which create_directories should default to True
_WRITE_CLASSES = frozenset(["Write", "WriteGeo", "DeepWrite"])

# Merge-type nodes where input 1 is the B (background) pipe
_MERGE_CLASSES = frozenset([
    "Merge", "Merge2", "Plus", "Screen", "Min", "Max", "Multiply",
    "Exclusion", "From", "Dissolve",
    "Matte", "Stencil", "Mask", "Over", "Under", "Atop", "Out",
    "GeomMerge", "DeepMerge",
])


@register_handler("create_node")
def create_node(params):
    node_class = _normalize_node_class(params["node_class"])
    user_knobs = params.get("knobs") or {}

    with undo_group("NukeMCP: create_node"):
        try:
            node = nuke.createNode(node_class, inpanel=False)
        except Exception as exc:
            raise RuntimeError(
                "could not create node class {!r}: {} -- "
                "use list_node_classes to discover available classes".format(node_class, exc)
            )

        xpos = params.get("xpos")
        ypos = params.get("ypos")
        if xpos is not None:
            node.setXpos(int(xpos))
        if ypos is not None:
            node.setYpos(int(ypos))

        applied, knob_errors = _apply_knobs(node, user_knobs)

        # Guardrail: auto-enable create_directories on Write nodes so renders
        # never fail with a "directory does not exist" error.
        if node.Class() in _WRITE_CLASSES and "create_directories" not in user_knobs:
            cd = node.knob("create_directories")
            if cd is not None:
                try:
                    cd.setValue(True)
                    applied.append("create_directories")
                except Exception:
                    pass

        input_errors = {}
        for index, input_name in enumerate(params.get("inputs") or []):
            input_node = nuke.toNode(input_name)
            if input_node is None:
                input_errors[str(index)] = "no such node: {!r}".format(input_name)
                continue
            node.setInput(index, input_node)

    result = _node_summary(node)
    result["knobs_applied"] = applied
    result["knob_errors"] = knob_errors
    result["input_errors"] = input_errors
    # Report when an alias was resolved to a different class name
    if node_class != params["node_class"]:
        result["node_class_resolved"] = node_class
    return result


@register_handler("set_knob_values")
def set_knob_values(params):
    node = _require_node(params["node_name"])
    with undo_group("NukeMCP: set_knob_values"):
        applied, errors = _apply_knobs(node, params.get("knobs"))
    return {"node_name": node.name(), "knobs_applied": applied, "errors": errors}


@register_handler("connect_nodes")
def connect_nodes(params):
    from_node = _require_node(params["from_node"])
    to_node = _require_node(params["to_node"])
    input_index = int(params.get("input_index", 0))

    auto_corrected = False
    # B-pipe guardrail: when the first connection to a Merge-type node arrives
    # at index 0 (A), silently route it to index 1 (B / background) instead.
    # Only applies when B is currently unconnected; once B is wired, subsequent
    # index-0 calls correctly target A.
    if (to_node.Class() in _MERGE_CLASSES
            and input_index == 0
            and to_node.input(1) is None):
        input_index = 1
        auto_corrected = True

    with undo_group("NukeMCP: connect_nodes"):
        to_node.setInput(input_index, from_node)

    result = {"from_node": from_node.name(), "to_node": to_node.name(), "input_index": input_index}
    if auto_corrected:
        result["auto_corrected"] = True
        result["note"] = (
            "routed to B (background) input -- "
            "pass input_index=0 explicitly once B is connected to target A"
        )
    return result


@register_handler("delete_node")
def delete_node(params):
    node = _require_node(params["node_name"])
    name = node.name()
    with undo_group("NukeMCP: delete_node"):
        nuke.delete(node)
    return {"deleted": name}


@register_handler("duplicate_node")
def duplicate_node(params):
    """Duplicate a node via script copy/paste, preserving all knob values."""
    original = _require_node(params["node_name"])
    xpos_offset = int(params.get("xpos_offset", 100))
    ypos_offset = int(params.get("ypos_offset", 0))

    with undo_group("NukeMCP: duplicate_node"):
        # Save current selection; work with a clean slate
        prev_selected = [n for n in nuke.allNodes() if n.isSelected()]
        for n in prev_selected:
            n.setSelected(False)

        original.setSelected(True)

        fd, tmp = tempfile.mkstemp(suffix=".nk")
        os.close(fd)
        try:
            nuke.nodeCopy(tmp)
            original.setSelected(False)
            nuke.nodePaste(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

        new_nodes = nuke.selectedNodes()
        orig_x, orig_y = original.xpos(), original.ypos()
        for new_node in new_nodes:
            new_node.setXpos(orig_x + xpos_offset)
            new_node.setYpos(orig_y + ypos_offset)

        # Restore original selection
        for n in nuke.allNodes():
            n.setSelected(False)
        for n in prev_selected:
            try:
                n.setSelected(True)
            except Exception:
                pass

    if not new_nodes:
        raise RuntimeError("duplicate produced no new nodes")

    return _node_summary(new_nodes[0])


@register_handler("list_node_classes")
def list_node_classes(params):
    """Walk the Nodes menu to return available node classes grouped by category."""
    category_filter = params.get("category")

    nodes_menu = nuke.menu("Nodes")
    result = {}

    for top_item in nodes_menu.items():
        cat_name = top_item.name()
        if not cat_name or cat_name.startswith("-"):
            continue
        try:
            sub_items = top_item.items()
        except AttributeError:
            # Leaf at top level; rare but include it
            result.setdefault("_top", []).append(cat_name)
            continue

        classes = []
        for sub_item in sub_items:
            name = sub_item.name()
            if not name or name.startswith("-"):
                continue
            try:
                # If this item also has children it's a sub-category;
                # include those class names too, prefixed with the sub-category.
                sub_sub = sub_item.items()
                for leaf in sub_sub:
                    lname = leaf.name()
                    if lname and not lname.startswith("-"):
                        classes.append("{}/{}".format(name, lname))
            except AttributeError:
                classes.append(name)

        if classes:
            result[cat_name] = sorted(classes)

    if category_filter:
        return {
            "category": category_filter,
            "classes": result.get(category_filter, []),
        }

    return {
        "categories": result,
        "total_classes": sum(len(v) for v in result.values()),
    }


@register_handler("get_node_default_knobs")
def get_node_default_knobs(params):
    """Create a temporary node of the given class, read its default knobs, then delete it."""
    from .knob_serialization import serialize_all_knobs

    node_class = params["node_class"]

    # Run inside an undo group so this transient creation can be undone if needed
    with undo_group("NukeMCP: get_node_default_knobs"):
        node = nuke.createNode(node_class, inpanel=False)
        try:
            knobs = serialize_all_knobs(node)
        finally:
            nuke.delete(node)

    return {"node_class": node_class, "knobs": knobs}
