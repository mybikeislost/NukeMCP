"""Graph Scope Variable read/write handlers.

Behaviour here is pinned to Nuke 17 semantics that were measured, not assumed.
Each of those measurements is asserted in tests/characterization_gsv_in_nuke.py,
which names the exact build they were taken on and fails if Nuke stops behaving
that way -- so the record cannot go stale without something saying so.

Three of them shape this module:

1. `Gsv_Knob.setValue()` REPLACES the whole knob, silently deleting any set not
   present in the dict passed to it. Every write here is therefore a targeted
   `setGsvValue()` on one path -- never a rebuilt-dict `setValue()`. That is
   what preserves unrelated sets and any structure we do not model.

2. GSV values are strings only; ints/lists/bools raise TypeError from Nuke. We
   reject them up front with a message that says so, rather than coercing --
   coercion would quietly turn a caller's frame number into "1001".

3. `addBeforeUser*` callbacks fire on Python API writes and can return False to
   VETO the write. So every write is read back and verified; a vetoed write is
   reported as vetoed, never as success.
"""

import contextlib
import re

import nuke

from ..dispatch import _handle_one, register_handler
from ..nuke_compat import undo_group

DEFAULT_SET = "__default__"

# How a GSV is referenced from a knob string. BOTH forms are valid:
#
#   %{shot}              braced
#   %shot                unbraced -- Foundry's own docs use /Volumes/%shot/f.exr
#
# Braces are delimiters marking where the expression ends, so they matter when
# the reference butts up against more text:
#
#   %shot.exr            -> "sh020.exr"   (name stops at the dot)
#   %shot_key.exr        -> unresolved    ("shot_key" is parsed as the name)
#   %{shot}_key.exr      -> "sh020_key.exr"
#
# Set-qualified references MUST be braced. The docs claim
# "%VariableSet.Variable" works, but in Nuke 17 it does not:
# "/x/%delivery.quality.mov" renders as "/x/0elivery.quality.mov". The unbraced
# form names a VARIABLE, never a set -- so Nuke looks for a variable called
# "delivery", finds none, and the leading %d falls through to printf frame
# padding. Verified, contradicting the docs.
#
# Note the precedence, which is the documented evaluation order (GSVs, then
# TCL, then OpenAssetIO, then view/frame tokens): a DEFINED variable wins over
# the path token. "%done", "%delivery" and "%view" all resolve correctly when
# variables of those names exist -- only unmatched names fall through.
#
# So excluding bare %d / %v / %V below is a heuristic, not a rule. It is the
# right default because those are overwhelmingly frame-padding and view tokens
# in real paths, at the cost of missing a reference to a variable literally
# named "d" or "v". %04d never matches anyway -- leading digit.
#
# And nothing errors on a bad reference: "%{gone}" evaluates to the literal
# "%{gone}", so a broken reference silently becomes part of a filename rather
# than failing. That is why gsv_validate_references exists.
_REFERENCE_RE = re.compile(r"%(?:\{([^{}]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

# Path/view tokens Nuke resolves itself; never GSV references.
_NOT_VARIABLES = frozenset(["d", "v", "V"])


def _iter_references(raw):
    """Yield (token, braced) for each GSV reference in a string."""
    for match in _REFERENCE_RE.finditer(raw):
        braced, bare = match.group(1), match.group(2)
        if braced is not None:
            if not braced:
                continue
            if "%" in braced:
                # Nested: "%{%shot}" resolves the inner reference and leaves the
                # braces literal. Parse what Nuke actually resolves.
                for nested in _iter_references(braced):
                    yield nested
                continue
            # Whitespace inside the braces stops Nuke resolving it entirely --
            # "%{ shot }" stays literal. Yield it unstripped so it fails to
            # match a real variable and gets reported as broken, which is what
            # it is, rather than being silently "fixed" by a strip().
            yield braced, True
        elif bare not in _NOT_VARIABLES:
            # Unbraced references cannot name a set -- see above.
            yield bare, False


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------

def _scope_node(scope):
    """Resolve a scope name to a node carrying a gsv knob.

    None / "root" / "Root" -> the Root node. Anything else is looked up as a
    node name (typically a VariableGroup).
    """
    if scope is None or scope in ("root", "Root"):
        return nuke.root()
    node = nuke.toNode(scope)
    if node is None:
        raise LookupError("no such scope node: {!r}".format(scope))
    if node.knob("gsv") is None:
        raise ValueError(
            "node {!r} (class {}) has no gsv knob -- only Root and "
            "VariableGroup-style nodes carry Graph Scope Variables".format(
                scope, node.Class()
            )
        )
    return node


def _scope_knob(scope):
    node = _scope_node(scope)
    knob = node.knob("gsv")
    if knob is None:
        raise ValueError("scope {!r} has no gsv knob".format(scope))
    return node, knob


def _scope_label(node):
    return "root" if node is nuke.root() else node.fullName()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _normalize_path(path):
    """Accept "shot" or "__default__.shot"; always return a set-qualified path.

    Nuke addresses every variable as `<set>.<name>`, with `__default__` holding
    the variables shown directly on the group. Bare names are a convenience for
    callers working in the common case.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("variable path must be a non-empty string")
    path = path.strip()
    if "." not in path:
        return "{}.{}".format(DEFAULT_SET, path)
    return path


def _split_path(path):
    set_name, _sep, var_name = _normalize_path(path).rpartition(".")
    return set_name, var_name


# A GSV name is [A-Za-z_][A-Za-z0-9_]* -- exactly the characters an unbraced
# %reference can span, which is not a coincidence. Measured: setGsvValue with a
# name containing "-", a space, or a leading digit SILENTLY STORES NOTHING. No
# exception, no warning, and a read-back returns None. Validating up front turns
# that into an error that says what is wrong.
_VALID_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_name(path):
    """Check both halves of a set-qualified path are legal GSV identifiers."""
    set_name, var_name = _split_path(path)
    for part, what in ((set_name, "set"), (var_name, "variable")):
        if not _VALID_NAME_RE.match(part or ""):
            raise ValueError(
                "invalid {} name {!r} in path {!r}. GSV names must match "
                "[A-Za-z_][A-Za-z0-9_]* -- no hyphens, spaces, dots or leading "
                "digits. Nuke silently stores nothing for an invalid name "
                "rather than raising.".format(what, part, path)
            )
    return path


def _validate_value(path, value):
    if value is None:
        raise ValueError(
            "cannot set {!r} to None -- use gsv_remove_variable to delete a "
            "variable".format(path)
        )
    if isinstance(value, bool) or not isinstance(value, str):
        raise TypeError(
            "GSV values must be strings; {!r} got {} ({!r}). Nuke rejects "
            "non-strings outright -- convert deliberately, e.g. str(1001), "
            "and keep non-string data in context metadata instead.".format(
                path, type(value).__name__, value
            )
        )
    return value


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def _flatten(value):
    """{set: {var: val}} -> {"set.var": val}, for comparison only."""
    flat = {}
    for set_name, variables in (value or {}).items():
        for var_name, var_value in (variables or {}).items():
            flat["{}.{}".format(set_name, var_name)] = var_value
    return flat


def _diff(before, after):
    """Structured before/after over the flattened views of two knob values."""
    flat_before, flat_after = _flatten(before), _flatten(after)
    changes = []
    for path in sorted(set(flat_before) | set(flat_after)):
        old, new = flat_before.get(path), flat_after.get(path)
        if old == new:
            continue
        if old is None:
            changes.append({"path": path, "action": "add", "before": None, "after": new})
        elif new is None:
            changes.append({"path": path, "action": "remove", "before": old, "after": None})
        else:
            changes.append({"path": path, "action": "modify", "before": old, "after": new})

    sets_before, sets_after = set(before or {}), set(after or {})
    set_changes = (
        [{"set": s, "action": "add"} for s in sorted(sets_after - sets_before)]
        + [{"set": s, "action": "remove"} for s in sorted(sets_before - sets_after)]
    )
    return changes, set_changes


def _describe_type(knob, path):
    """Everything Nuke records about one variable beyond its value.

    Not just the type. A variable also carries a label, a tooltip and a
    Variables-panel favourite flag, and all three serialise into the .nk
    alongside the type:

        shot sh020 { { vp:visiblity True } { vp:type List }
                     { vp:options { sh010 sh020 } }
                     { vp:label Shot } { vp:tooltip "which shot" } }

    (`vp:visiblity` is Foundry's spelling, not ours.) Reporting only the type
    made a schema look complete while hiding three fields a template author had
    set deliberately -- and made them invisible to any diff built on the schema.

    Our writes preserve all of it: a two-argument `setGsvValue` and `renameGsv`
    both leave it intact, measured. It is a `setValue()` round-trip that would
    destroy it, which is why we never do one.

    Each getter is tried separately. They arrived across versions, and one
    missing method must not blank the rest.
    """
    info = {}
    try:
        data_type = knob.getDataType(path)
        info["data_type"] = getattr(data_type, "name", str(data_type))
    except Exception:
        return info
    if info.get("data_type") == "List":
        try:
            info["list_options"] = list(knob.getListOptions(path))
        except Exception:
            pass
    for key, method in (("label", "getLabel"),
                        ("tooltip", "getTooltip"),
                        ("favorite", "isFavorite")):
        getter = getattr(knob, method, None)
        if getter is None:
            continue
        try:
            value = getter(path)
        except Exception:
            continue
        # Only report what was actually set. An empty label is the default, and
        # carrying it would make every schema diff noisier for no information.
        if value not in (None, "", False):
            info[key] = value
    return info


# ---------------------------------------------------------------------------
# Read handlers
# ---------------------------------------------------------------------------

@register_handler("gsv_get")
def gsv_get(params):
    """Full GSV state for one scope, returned exactly as Nuke reports it."""
    node, knob = _scope_knob(params.get("scope"))
    value = knob.value()
    sets = {name: dict(variables or {}) for name, variables in (value or {}).items()}
    return {
        "scope": _scope_label(node),
        "sets": sets,
        "variable_count": sum(len(v) for v in sets.values()),
        "set_names": sorted(sets),
    }


@register_handler("gsv_list_scopes")
def gsv_list_scopes(params):
    """Every scope in the script: Root plus each VariableGroup.

    Inheritance is NOT reported by Nuke -- a child VariableGroup returns None
    for a variable defined only on its parent. `defines` below is therefore
    strictly what each scope declares itself, and any effective value must be
    resolved by walking `parent_scope` upward.
    """
    scopes = [{
        "scope": "root",
        "class": "Root",
        "parent_scope": None,
        "defines": sorted(_flatten(nuke.root().knob("gsv").value())),
    }]

    for node in nuke.allNodes(recurseGroups=True):
        knob = node.knob("gsv")
        if knob is None or node is nuke.root():
            continue
        parent = node.parent()
        parent_label = (
            "root" if parent is None or parent is nuke.root() else parent.fullName()
        )
        scopes.append({
            "scope": node.fullName(),
            "class": node.Class(),
            "parent_scope": parent_label,
            "defines": sorted(_flatten(knob.value())),
        })

    return {"scopes": scopes, "count": len(scopes)}


@register_handler("gsv_get_variable")
def gsv_get_variable(params):
    """One variable, from one scope.

    Scope-local only. A value of None means this scope does not define it --
    which is not the same as "no such variable anywhere", because a parent may
    define it and Nuke will not tell you that from here.
    """
    node, knob = _scope_knob(params.get("scope"))
    path = _normalize_path(params["path"])
    set_name, var_name = _split_path(path)

    value = knob.getGsvValue(path)
    result = {
        "scope": _scope_label(node),
        "path": path,
        "set": set_name,
        "name": var_name,
        "value": value,
        "defined_in_scope": value is not None,
    }
    result.update(_describe_type(knob, path))
    return result


@register_handler("gsv_diff")
def gsv_diff(params):
    """What would change if these variables were applied. Writes nothing."""
    node, knob = _scope_knob(params.get("scope"))
    variables = params.get("variables") or {}

    before = knob.value()
    projected = {name: dict(vars_ or {}) for name, vars_ in (before or {}).items()}

    rejected = {}
    for raw_path, value in variables.items():
        path = _normalize_path(raw_path)
        try:
            _validate_name(path)
            _validate_value(path, value)
        except (TypeError, ValueError) as exc:
            rejected[path] = str(exc)
            continue
        set_name, var_name = _split_path(path)
        projected.setdefault(set_name, {})[var_name] = value

    changes, set_changes = _diff(before, projected)
    return {
        "scope": _scope_label(node),
        "changes": changes,
        "set_changes": set_changes,
        "rejected": rejected,
        "would_change": len(changes),
        "before": before,
        "after": projected,
    }


# ---------------------------------------------------------------------------
# Write handlers
# ---------------------------------------------------------------------------

def _apply_variables(knob, variables, create_missing=True):
    """Write each path individually, then read back to confirm it landed.

    Targeted setGsvValue calls, never a rebuilt setValue -- see module docstring.
    Read-back is what catches a `addBeforeUser*` callback vetoing the write.

    setGsvValue CREATES a variable that does not exist, so a typo'd path would
    otherwise invent a variable and report success. Creations are tracked
    separately and reported, and `create_missing=False` refuses them outright.
    """
    applied, vetoed, errors, created = [], {}, {}, []

    for raw_path, value in variables.items():
        path = _normalize_path(raw_path)
        try:
            _validate_name(path)
            _validate_value(path, value)
        except (TypeError, ValueError) as exc:
            errors[path] = str(exc)
            continue

        set_name, _var_name = _split_path(path)
        is_new = knob.getGsvValue(path) is None
        if is_new and not create_missing:
            errors[path] = (
                "{!r} does not exist in this scope and create_missing is false. "
                "setGsvValue would silently create it -- check for a typo, or "
                "use gsv_create_variable to add it deliberately.".format(path)
            )
            continue
        try:
            # A set must exist before a variable can be placed in it.
            if set_name != DEFAULT_SET and not knob.containsGsvSet(set_name):
                knob.addGsvSet(set_name)
            knob.setGsvValue(path, value)
        except Exception as exc:
            errors[path] = "{}: {}".format(type(exc).__name__, exc)
            continue

        landed = knob.getGsvValue(path)
        if landed == value:
            applied.append(path)
            if is_new:
                created.append(path)
        else:
            vetoed[path] = {
                "requested": value,
                "actual": landed,
                "note": "write did not land -- a beforeUser GSV callback likely "
                        "returned False, or the value was rejected downstream",
            }

    return applied, vetoed, errors, created


def _write(params, variables, label):
    node, knob = _scope_knob(params.get("scope"))
    dry_run = bool(params.get("dry_run", False))
    before = knob.value()

    if dry_run:
        preview = gsv_diff({"scope": params.get("scope"), "variables": variables})
        preview["dry_run"] = True
        preview["applied"] = []
        return preview

    with undo_group("NukeMCP: {}".format(label)):
        applied, vetoed, errors, created = _apply_variables(
            knob, variables, create_missing=bool(params.get("create_missing", True))
        )

    after = knob.value()
    changes, set_changes = _diff(before, after)
    return {
        "scope": _scope_label(node),
        "dry_run": False,
        "applied": applied,
        "created": created,
        "vetoed": vetoed,
        "errors": errors,
        "changes": changes,
        "set_changes": set_changes,
        "before": before,
        "after": after,
        "all_ok": not vetoed and not errors,
    }


@register_handler("gsv_set_variable")
def gsv_set_variable(params):
    """Set one variable. Undoable, dry-runnable, verified after write."""
    path = params["path"]
    return _write(params, {path: params.get("value")}, "gsv_set_variable")


@register_handler("gsv_set_variables")
def gsv_set_variables(params):
    """Set many variables in ONE undo group."""
    variables = params.get("variables")
    if not variables:
        raise ValueError("variables must be a non-empty {path: value} mapping")
    return _write(params, variables, "gsv_set_variables")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@register_handler("gsv_get_schema")
def gsv_get_schema(params):
    """The shape of the GSVs -- names, types, list options -- across all scopes.

    This is what a context record has to conform to. Values are included only
    as `example_value`; the schema is about structure, and a context supplies
    the values.
    """
    scopes = gsv_list_scopes({})["scopes"]
    variables = {}

    for entry in scopes:
        scope_name = entry["scope"]
        _node, knob = _scope_knob(None if scope_name == "root" else scope_name)
        for path in entry["defines"]:
            info = variables.setdefault(path, {
                "path": path,
                "set": path.split(".", 1)[0],
                "name": path.split(".", 1)[1],
                "defined_in": [],
                "data_type": "String",
                "list_options": None,
                "example_value": None,
            })
            info["defined_in"].append(scope_name)
            described = _describe_type(knob, path)
            data_type = described.get("data_type")
            if data_type and data_type != "Invalid":
                info.setdefault("_types", {})[scope_name] = data_type
                # "String" is the default every variable reports, so it must
                # never overwrite a more specific type declared by another
                # scope -- a VariableGroup overriding a List-typed Root
                # variable reports plain String for its own copy.
                if info["data_type"] == "String":
                    info["data_type"] = data_type
            if described.get("list_options"):
                info["list_options"] = described["list_options"]
            # Label, tooltip and favourite are only present when set, so the
            # first scope that declares one wins -- same rule as list options.
            # Absent keys stay absent rather than becoming null: a schema diff
            # should show a label appearing, not every variable growing three
            # empty fields.
            for key in ("label", "tooltip", "favorite"):
                if key in described and key not in info:
                    info[key] = described[key]
            if info["example_value"] is None:
                info["example_value"] = knob.getGsvValue(path)

    ordered = []
    conflicts = []
    for key in sorted(variables):
        info = variables[key]
        per_scope = info.pop("_types", {})
        if len(set(per_scope.values())) > 1:
            info["type_conflict"] = per_scope
            conflicts.append(info["path"])
        ordered.append(info)

    return {
        "variables": ordered,
        "variable_count": len(ordered),
        "sets": sorted({v["set"] for v in ordered}),
        "scopes": [e["scope"] for e in scopes],
        "overridden": [v["path"] for v in ordered if len(v["defined_in"]) > 1],
        "type_conflicts": conflicts,
    }


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _scope_chain(node):
    """Scopes a node resolves against, innermost first, ending at root.

    Nuke exposes no "effective value" of its own -- a child VariableGroup
    cannot read a value it inherits -- so resolution is ours: walk the parent
    chain and take the first scope that defines the path.
    """
    chain, seen = [], set()
    current = node
    while current is not None:
        if id(current) in seen:
            break
        seen.add(id(current))
        if current.knob("gsv") is not None:
            chain.append(current)
        try:
            current = current.parent()
        except Exception:
            break
    root = nuke.root()
    if root not in chain:
        chain.append(root)
    return chain


def _scan_knobs():
    """Yield (node, knob_name, raw_string, knob) for every string knob using %.

    Deliberately scans by value type rather than a knob-class allowlist -- a
    reference is just text, and it turns up in file paths, labels, Text2
    messages, TCL expressions and elsewhere. Reading raw .value(), never
    .evaluate(), because a broken reference evaluates to itself and would be
    indistinguishable from working text.
    """
    for node in nuke.allNodes(recurseGroups=True) + [nuke.root()]:
        try:
            knobs = node.knobs()
        except Exception:
            continue
        for knob_name, knob in knobs.items():
            # Two places a reference can live, and they are not the same text.
            #
            # A STRING knob holds it in its value: "/jobs/%{shot}/...".
            #
            # A NUMERIC knob holds it in an EXPRESSION, and value() returns the
            # evaluated number instead -- setExpression("%blur_size") makes
            # value() return 12.0 with no trace of the reference. The
            # expression text is only reachable via toScript(), which returns
            # "{%blur_size}". Scanning values alone misses these entirely, so a
            # rename would report no references and silently break the knob.
            texts = []
            try:
                raw = knob.value()
                if isinstance(raw, str):
                    texts.append(raw)
            except Exception:
                pass
            try:
                if knob.hasExpression():
                    script = knob.toScript()
                    if isinstance(script, str):
                        texts.append(script)
            except Exception:
                pass

            seen = set()
            for text in texts:
                if "%" not in text or text in seen:
                    continue
                seen.add(text)
                yield node, knob_name, text, knob


def _scan_gsv_values():
    """Yield references held inside GSV VALUES, not just knobs.

    Nuke supports recursive evaluation -- a variable whose value is
    "%{inner}_suffix" resolves through to the inner variable. Those references
    break exactly like knob references do, so they have to be scanned too.
    """
    for entry in gsv_list_scopes({})["scopes"]:
        scope_name = entry["scope"]
        try:
            node, knob = _scope_knob(None if scope_name == "root" else scope_name)
        except Exception:
            continue
        for set_name, variables in (knob.value() or {}).items():
            for var_name, value in (variables or {}).items():
                if isinstance(value, str) and "%" in value:
                    yield node, "gsv", value, knob, "{}.{}".format(set_name, var_name)


def _switch_binding(knob):
    """Extract the GSV path a VariableSwitch-style `variable` knob is bound to.

    Returns (path, broken). The knob is an Enumeration_Knob whose entries are
    "<gsv path>\\t<display name>", e.g. "__default__.shot\\tshot" -- so the
    binding contains no "%" and no text-scanning approach will ever find it.

    When the bound variable disappears (renamed or removed) Nuke rewrites the
    selection to "setErrorValue <old path> {...}" and the switch silently stops
    responding. That is the broken=True case.
    """
    try:
        raw = knob.value()
    except Exception:
        return None, False

    if isinstance(raw, str) and raw.startswith("setErrorValue"):
        rest = raw[len("setErrorValue"):].strip()
        token = rest.split(None, 1)[0].strip('"{}') if rest else ""
        return (token or None), True

    try:
        name = knob.enumName(int(knob.getValue()))
    except Exception:
        return None, False
    if not name:
        return None, False
    path = name.split("\t")[0].strip().strip('"')
    # Every real GSV path is set-qualified; "None" is the unbound entry.
    if not path or path == "None" or "." not in path:
        return None, False
    return path, False


def _scan_switch_bindings():
    """Yield (node, knob, path, broken) for every variable-driven switch node.

    Separate from _scan_knobs because these dependencies are structural, not
    textual -- and missing them means rename/remove reports "no references" for
    a variable a VariableSwitch depends on, then breaks it.
    """
    for node in nuke.allNodes(recurseGroups=True):
        knob = node.knob("variable")
        if knob is None:
            continue
        try:
            if knob.Class() != "Enumeration_Knob":
                continue
        except Exception:
            continue
        path, broken = _switch_binding(knob)
        if path:
            yield node, knob, path, broken


def _references(target_path=None):
    """Every GSV dependency in the script, optionally filtered to one path.

    Three kinds, all of which break the same way when a variable is renamed or
    removed:
      knob_text       -- "%{shot}" / "%shot" in any string knob
      gsv_value       -- a reference inside another variable's value
      variable_switch -- a VariableSwitch bound to the variable by path
    """
    found = []
    sources = [(n, k, raw, knob, None) for n, k, raw, knob in _scan_knobs()]
    sources += list(_scan_gsv_values())

    for node, knob_name, raw, knob, defined_in in sources:
        for token, braced in _iter_references(raw):
            path = _normalize_path(token)
            if target_path is not None and path != target_path:
                continue
            entry = {
                "kind": "gsv_value" if defined_in is not None else "knob_text",
                "node": node.fullName(),
                "node_class": node.Class(),
                "knob": knob_name,
                "knob_class": knob.Class(),
                "path": path,
                "token": token,
                "braced": braced,
                "qualified": "." in token,
                "raw": raw,
            }
            if defined_in is not None:
                entry["in_variable"] = defined_in
            found.append(entry)

    for node, knob, path, broken in _scan_switch_bindings():
        if target_path is not None and path != target_path:
            continue
        found.append({
            "kind": "variable_switch",
            "node": node.fullName(),
            "node_class": node.Class(),
            "knob": "variable",
            "knob_class": knob.Class(),
            "path": path,
            "token": path,
            "braced": None,
            "qualified": True,
            "raw": path,
            "binding_broken": broken,
        })
    return found


@register_handler("gsv_find_references")
def gsv_find_references(params):
    """Find every knob referencing a GSV, as %{name} or %{set.name}."""
    path = _normalize_path(params["path"]) if params.get("path") else None
    refs = _references(path)
    return {
        "path": path,
        "references": refs,
        "count": len(refs),
        "nodes": sorted({r["node"] for r in refs}),
    }


@register_handler("gsv_validate_references")
def gsv_validate_references(params):
    """Report references that will not resolve.

    A dangling reference does not raise in Nuke -- "%{gone}" evaluates to the
    literal string "%{gone}", so it lands silently in a render path. This is
    the tool that catches that before a render does.
    """
    # Every set name defined anywhere, so a reference to a bare set can be
    # named as such rather than reported as a missing variable.
    known_sets = set()
    for entry in gsv_list_scopes({})["scopes"]:
        try:
            _n, k = _scope_knob(None if entry["scope"] == "root" else entry["scope"])
        except Exception:
            continue
        known_sets.update(k.value() or {})
    known_sets.discard(DEFAULT_SET)

    broken, ok = [], []
    for ref in _references():
        node = nuke.toNode(ref["node"])

        # A switch binding is authoritative about its own breakage: Nuke marks
        # it with setErrorValue the moment the bound variable disappears.
        if ref["kind"] == "variable_switch":
            if ref.get("binding_broken"):
                broken.append(dict(
                    ref,
                    resolved_in=None,
                    reason="VariableSwitch is bound to {}, which no longer "
                           "exists".format(ref["path"]),
                    hint="Nuke has flagged the selection as an error value and "
                         "the switch has stopped responding to the variable. "
                         "Re-point its 'variable' knob at an existing GSV.",
                ))
            else:
                ok.append(dict(ref, resolved_in="binding"))
            continue

        resolved_in = None
        if node is not None:
            for scope_node in _scope_chain(node):
                knob = scope_node.knob("gsv")
                if knob is not None and knob.getGsvValue(ref["path"]) is not None:
                    resolved_in = _scope_label(scope_node)
                    break

        entry = dict(ref, resolved_in=resolved_in)
        if resolved_in is None:
            # A bare %{x} only ever resolves against __default__; flag the
            # named-set mistake explicitly, it is easy to make and silent.
            entry["reason"] = (
                "no scope in this node's parent chain defines {}".format(ref["path"])
            )
            # Most specific diagnosis first. An unbraced set-qualified attempt
            # names a set AND is followed by a member, so it would match the
            # bare-set case too -- but the caller's intent is unambiguous and
            # the fix is different.
            if "%{}.".format(ref["token"]) in ref["raw"]:
                entry["hint"] = (
                    "looks like an attempt to reference a named set as "
                    "%{0}.<variable>. The unbraced form names a VARIABLE and "
                    "stops at the dot, so this never reaches the set -- and "
                    "the unmatched %{0} then falls through to Nuke's own path "
                    "tokens. Set-qualified references must be braced: "
                    "%{{{0}.<variable>}}".format(ref["token"])
                )
            elif ref["token"] in known_sets:
                # Naming a set where a variable belongs. A set has no value of
                # its own, so this stays literal -- braces do not help.
                entry["hint"] = (
                    "{0!r} is a variable SET, not a variable, and a set has no "
                    "value of its own -- %{{{0}}} stays literal. Reference a "
                    "member instead: %{{{0}.<variable>}}".format(ref["token"])
                )
            elif not ref["qualified"]:
                entry["hint"] = (
                    "an unqualified reference resolves only against the "
                    "__default__ set -- a variable in a named set must be "
                    "written %{{<set>.{}}}".format(ref["token"])
                )
            broken.append(entry)
        else:
            ok.append(entry)

    return {
        "valid": len(ok),
        "invalid": len(broken),
        "broken": broken,
        "checked": len(ok) + len(broken),
    }


# ---------------------------------------------------------------------------
# Schema mutation -- create / rename / remove
# ---------------------------------------------------------------------------

def _blocked_by_references(path, force, action):
    """Refuse a destructive change that would orphan references, unless forced.

    Nuke does not rewrite references on rename, and removal leaves the knob
    string untouched -- both verified. So the caller has to be told what will
    break before it breaks.
    """
    refs = _references(path)
    if refs and not force:
        return {
            "blocked": True,
            "action": action,
            "path": path,
            "reason": (
                "{} dependency/ies on {} ({}). Nuke repairs none of them: text "
                "references silently stop resolving, and a bound VariableSwitch "
                "is left on an error value. Review them, then pass force=true "
                "to proceed anyway.".format(
                    len(refs), path,
                    ", ".join(sorted({r["kind"] for r in refs})))
            ),
            "references": refs,
            "applied": False,
        }
    return None


@register_handler("gsv_create_variable")
def gsv_create_variable(params):
    """Create a variable, optionally typed as a List with fixed options."""
    node, knob = _scope_knob(params.get("scope"))
    path = _normalize_path(params["path"])
    value = params.get("value", "")
    _validate_name(path)
    _validate_value(path, value)
    set_name, _ = _split_path(path)
    dry_run = bool(params.get("dry_run", False))
    list_options = params.get("list_options")

    if knob.getGsvValue(path) is not None:
        raise ValueError(
            "{!r} already exists in scope {!r} -- use gsv_set_variable to "
            "change its value".format(path, _scope_label(node))
        )

    before = knob.value()
    if dry_run:
        return {"scope": _scope_label(node), "path": path, "dry_run": True,
                "applied": False, "would_create": path, "before": before}

    with undo_group("NukeMCP: gsv_create_variable"):
        if set_name != DEFAULT_SET and not knob.containsGsvSet(set_name):
            knob.addGsvSet(set_name)
        knob.setGsvValue(path, value)
        if list_options:
            knob.setDataType(path, nuke.gsv.DataType.List)
            knob.setListOptions(path, [str(o) for o in list_options])

    landed = knob.getGsvValue(path)
    changes, set_changes = _diff(before, knob.value())
    return {
        "scope": _scope_label(node), "path": path, "dry_run": False,
        "applied": landed == value, "value": landed,
        "list_options": list_options,
        "changes": changes, "set_changes": set_changes, "after": knob.value(),
    }


@register_handler("gsv_rename_variable")
def gsv_rename_variable(params):
    """Rename a variable. Reports referencing knobs; never rewrites them."""
    node, knob = _scope_knob(params.get("scope"))
    path = _normalize_path(params["path"])
    new_name = params["new_name"]
    dry_run = bool(params.get("dry_run", False))
    force = bool(params.get("force", False))

    if knob.getGsvValue(path) is None:
        raise LookupError("no such variable {!r} in scope {!r}".format(path, _scope_label(node)))
    if "." in new_name:
        raise ValueError(
            "new_name must be a bare variable name, not a path -- got {!r}. "
            "Renaming across sets is not supported.".format(new_name)
        )
    _validate_name("{}.{}".format(_split_path(path)[0], new_name))

    refs = _references(path)
    blocked = _blocked_by_references(path, force, "rename")
    if blocked:
        return blocked

    set_name, _ = _split_path(path)
    new_path = "{}.{}".format(set_name, new_name)
    before = knob.value()

    if dry_run:
        return {"scope": _scope_label(node), "dry_run": True, "applied": False,
                "path": path, "new_path": new_path,
                "references_left_dangling": refs, "before": before}

    with undo_group("NukeMCP: gsv_rename_variable"):
        knob.renameGsv(path, new_name)

    after = knob.value()
    changes, set_changes = _diff(before, after)
    return {
        "scope": _scope_label(node), "dry_run": False,
        "applied": knob.getGsvValue(new_path) is not None,
        "path": path, "new_path": new_path,
        "references_left_dangling": refs,
        "warning": (
            "{0} dependency/ies still point at {1}. Text references still say "
            "%{2} or %{{{2}}} and now resolve to nothing; any VariableSwitch "
            "bound to it is left on an error value and stops responding. Nuke "
            "does not fix either -- update them.".format(
                len(refs), path, path.split(".", 1)[1])
        ) if refs else None,
        "changes": changes, "set_changes": set_changes, "after": after,
    }


@register_handler("gsv_remove_variable")
def gsv_remove_variable(params):
    """Remove a variable. Refuses if referenced, unless force=true."""
    node, knob = _scope_knob(params.get("scope"))
    path = _normalize_path(params["path"])
    dry_run = bool(params.get("dry_run", False))
    force = bool(params.get("force", False))

    if knob.getGsvValue(path) is None:
        raise LookupError("no such variable {!r} in scope {!r}".format(path, _scope_label(node)))

    refs = _references(path)
    blocked = _blocked_by_references(path, force, "remove")
    if blocked:
        return blocked

    before = knob.value()
    if dry_run:
        return {"scope": _scope_label(node), "dry_run": True, "applied": False,
                "path": path, "references_left_dangling": refs, "before": before}

    with undo_group("NukeMCP: gsv_remove_variable"):
        knob.removeGsv(path)

    after = knob.value()
    changes, set_changes = _diff(before, after)
    return {
        "scope": _scope_label(node), "dry_run": False,
        "applied": knob.getGsvValue(path) is None,
        "path": path, "references_left_dangling": refs,
        "changes": changes, "set_changes": set_changes, "after": after,
    }


@register_handler("gsv_create_set")
def gsv_create_set(params):
    """Create an empty variable set."""
    node, knob = _scope_knob(params.get("scope"))
    set_name = params["name"]
    if "." in set_name:
        raise ValueError("set name must not contain '.', got {!r}".format(set_name))
    _validate_name("{}.placeholder".format(set_name))
    if knob.containsGsvSet(set_name):
        raise ValueError("set {!r} already exists in scope {!r}".format(set_name, _scope_label(node)))

    before = knob.value()
    if bool(params.get("dry_run", False)):
        return {"scope": _scope_label(node), "dry_run": True, "applied": False,
                "name": set_name, "before": before}

    with undo_group("NukeMCP: gsv_create_set"):
        knob.addGsvSet(set_name)

    after = knob.value()
    _changes, set_changes = _diff(before, after)
    return {"scope": _scope_label(node), "dry_run": False,
            "applied": knob.containsGsvSet(set_name), "name": set_name,
            "set_changes": set_changes, "after": after}


@register_handler("gsv_remove_set")
def gsv_remove_set(params):
    """Remove a set and everything in it. Refuses if anything is referenced."""
    node, knob = _scope_knob(params.get("scope"))
    set_name = params["name"]
    force = bool(params.get("force", False))
    dry_run = bool(params.get("dry_run", False))

    if not knob.containsGsvSet(set_name):
        raise LookupError("no such set {!r} in scope {!r}".format(set_name, _scope_label(node)))

    before = knob.value()
    members = sorted("{}.{}".format(set_name, v) for v in (before.get(set_name) or {}))
    refs = [r for r in _references() if r["path"] in set(members)]

    if refs and not force:
        return {
            "blocked": True, "action": "remove_set", "name": set_name,
            "reason": (
                "removing set {!r} would drop {} variable(s), {} of which are "
                "still referenced. Review them, then pass force=true.".format(
                    set_name, len(members), len(refs))
            ),
            "members": members, "references": refs, "applied": False,
        }

    if dry_run:
        return {"scope": _scope_label(node), "dry_run": True, "applied": False,
                "name": set_name, "members": members,
                "references_left_dangling": refs, "before": before}

    with undo_group("NukeMCP: gsv_remove_set"):
        knob.removeGsvSet(set_name)

    after = knob.value()
    changes, set_changes = _diff(before, after)
    return {"scope": _scope_label(node), "dry_run": False,
            "applied": not knob.containsGsvSet(set_name), "name": set_name,
            "removed_variables": members, "references_left_dangling": refs,
            "changes": changes, "set_changes": set_changes, "after": after}


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------

@register_handler("gsv_trace")
def gsv_trace(params):
    """Where a variable's value comes from at a given point in the graph.

    Nuke exposes no effective-value API, so this walks the scope chain and
    reports each scope's own value plus which one wins -- the Root/
    Root / VariableGroup / effective value, per scope.
    """
    path = _normalize_path(params["path"])
    node_name = params.get("node")
    node = nuke.toNode(node_name) if node_name else nuke.root()
    if node is None:
        raise LookupError("no such node: {!r}".format(node_name))

    layers, effective, effective_scope = [], None, None
    for scope_node in _scope_chain(node):
        knob = scope_node.knob("gsv")
        value = knob.getGsvValue(path) if knob is not None else None
        label = _scope_label(scope_node)
        layers.append({"scope": label, "class": scope_node.Class(), "value": value,
                       "defines": value is not None})
        if value is not None and effective is None:
            effective, effective_scope = value, label

    return {
        "path": path,
        "from_node": node.fullName(),
        "layers": layers,
        "effective": effective,
        "effective_scope": effective_scope,
        "resolves": effective is not None,
    }


# ---------------------------------------------------------------------------
# Temporary application
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _undo_suspended():
    """Stop Nuke recording undo entries for the duration.

    A temporary apply is net-zero: variables go in, work happens, the original
    values come back. Recording that would push 2N entries onto the artist's
    undo stack for a change they never made and cannot see. `Undo.disable()`
    prevents it.

    Paired in a finally, because leaving undo disabled would silently break the
    artist's Cmd+Z for the rest of the session -- a far worse outcome than the
    stack noise this avoids.
    """
    nuke.Undo.disable()
    try:
        yield
    finally:
        nuke.Undo.enable()


def _restore_snapshot(knob, snapshot):
    """Put a scope back exactly as it was, without rebuilding the knob.

    Deliberately NOT `knob.setValue(snapshot)`, even though the snapshot came
    from `knob.value()`. A real .nk carries a `__panel__` set holding Variables
    panel configuration that `.value()` never reports -- rebuilding the knob
    from a dict would delete it silently. So restore, like every other write
    here, is a series of targeted operations.
    """
    current = knob.value() or {}
    flat_now = _flatten(current)
    flat_then = _flatten(snapshot)
    restored, failed = [], {}

    # Revert changed values and re-add anything that was removed.
    for path, old_value in flat_then.items():
        if flat_now.get(path) != old_value:
            try:
                set_name = path.split(".", 1)[0]
                if set_name != DEFAULT_SET and not knob.containsGsvSet(set_name):
                    knob.addGsvSet(set_name)
                knob.setGsvValue(path, old_value)
                restored.append(path)
            except Exception as exc:
                failed[path] = "{}: {}".format(type(exc).__name__, exc)

    # Remove anything that was not there before.
    for path in flat_now:
        if path not in flat_then:
            try:
                knob.removeGsv(path)
                restored.append(path)
            except Exception as exc:
                failed[path] = "{}: {}".format(type(exc).__name__, exc)

    for set_name in set(current) - set(snapshot or {}):
        if not (knob.value() or {}).get(set_name):
            try:
                knob.removeGsvSet(set_name)
            except Exception as exc:
                failed[set_name] = "{}: {}".format(type(exc).__name__, exc)

    return restored, failed


def _verify_restored(knob, snapshot):
    """Confirm the scope really is back. A silent failure here corrupts state."""
    return _flatten(knob.value()) == _flatten(snapshot)


@register_handler("gsv_apply_temporary")
def gsv_apply_temporary(params):
    """Apply variables, run operations, then restore the original values.

    Snapshot, apply, do a known operation, restore -- including
    on exception. Restoration is explicit rather than `nuke.undo()`, because
    undo is the artist's stack and other things push onto it; rolling back by
    undoing would race with whatever else is there.

    Undo recording is suspended for the whole operation, so a net-zero
    inspection leaves no trace in the artist's history.
    """
    node, knob = _scope_knob(params.get("scope"))
    variables = params.get("variables") or {}
    operations = params.get("operations") or []

    snapshot = knob.value()
    applied, op_results, apply_errors = [], [], {}
    failure = None

    with _undo_suspended():
        try:
            applied, _vetoed, apply_errors, _created = _apply_variables(
                knob, variables, create_missing=bool(params.get("create_missing", True))
            )
            for op in operations:
                tool_name = op.get("tool", "")
                try:
                    op_results.append({
                        "ok": True, "tool": tool_name,
                        "result": _handle_one(tool_name, op.get("params") or {}),
                    })
                except Exception as exc:
                    op_results.append({
                        "ok": False, "tool": tool_name,
                        "error_type": type(exc).__name__, "message": str(exc),
                    })
        except Exception as exc:
            failure = "{}: {}".format(type(exc).__name__, exc)
        finally:
            restored, restore_errors = _restore_snapshot(knob, snapshot)

    verified = _verify_restored(knob, snapshot)
    return {
        "scope": _scope_label(node),
        "applied": applied,
        "apply_errors": apply_errors,
        "operations": op_results,
        "restored": sorted(set(restored)),
        "restore_errors": restore_errors,
        "restored_cleanly": verified and not restore_errors,
        "failure": failure,
        "state": knob.value(),
        "warning": None if (verified and not restore_errors) else (
            "SCOPE NOT FULLY RESTORED -- the session no longer matches its "
            "pre-call state. Compare 'state' against what you expected before "
            "making further changes."
        ),
    }



# Frame padding as it appears in a RAW knob value: "####" or "%04d"/"%d".
_PADDING_TOKEN_RE = re.compile(r"(#+)|%(0?)(\d*)d")


def _padding_width(raw):
    """Width of the frame field in a raw path, or None if there is none."""
    match = _PADDING_TOKEN_RE.search(raw or "")
    if not match:
        return None
    if match.group(1):
        return len(match.group(1))
    digits = match.group(3)
    return int(digits) if digits else 1


def _padded_form(knob, raw, resolved_at_current):
    """Rebuild a frame-padded path by diffing two evaluations.

    NOT nuke.filename(): that returns variables UNRESOLVED inside a temporary
    apply, because it does not refresh within one synchronous handler call.
    evaluate() is reliable, so evaluate twice and treat whatever differs as the
    frame field.

    The two frames must differ in EVERY digit of the field, or only the last
    digit shows up as different and the padding comes out as "000#" -- a glob
    that silently matches the wrong frames. So the width comes from the raw
    path's own padding token, and the probe frames are all-1s and all-2s of
    that width.
    """
    width = _padding_width(raw)
    if width is None:
        return resolved_at_current

    probe = max(width, 4)
    try:
        a = knob.evaluate(int("1" * probe))
        b = knob.evaluate(int("2" * probe))
    except Exception:
        return resolved_at_current
    if a == b or len(a) != len(b):
        return resolved_at_current

    out, run = [], 0
    for ch_a, ch_b in zip(a, b):
        if ch_a == ch_b:
            if run:
                out.append("#" * run)
                run = 0
            out.append(ch_a)
        else:
            run += 1
    if run:
        out.append("#" * run)
    return "".join(out)


@register_handler("context_resolve_paths")
def context_resolve_paths(params):
    """What paths would actually be written under these variables?

    Applies the variables temporarily and asks NUKE to evaluate each knob,
    rather than reimplementing its reference grammar. That grammar has three
    parses, a fall-through to path tokens, and whitespace rules -- getting a
    second implementation subtly wrong is exactly how a render silently writes
    to the wrong place.

    Reports both the raw and the resolved string, and flags any resolved value
    still containing "%", which is what an unresolved reference looks like:
    Nuke leaves it as literal text rather than erroring.
    """
    node, knob = _scope_knob(params.get("scope"))
    variables = params.get("variables") or {}
    knob_filter = params.get("knobs") or ["file"]
    classes = params.get("node_classes")
    frames = params.get("frames") or []

    targets = []
    for candidate in nuke.allNodes(recurseGroups=True):
        if classes and candidate.Class() not in classes:
            continue
        for knob_name in knob_filter:
            k = candidate.knob(knob_name)
            if k is None:
                continue
            try:
                raw = k.value()
            except Exception:
                continue
            if isinstance(raw, str) and raw:
                targets.append((candidate.fullName(), knob_name, k, raw))

    snapshot = knob.value()
    resolved = []
    with _undo_suspended():
        try:
            _apply_variables(knob, variables,
                             create_missing=bool(params.get("create_missing", True)))
            for full_name, knob_name, k, raw in targets:
                try:
                    value = k.evaluate()
                except Exception as exc:
                    value = "<evaluate raised {}>".format(type(exc).__name__)
                entry = {
                    "node": full_name,
                    "knob": knob_name,
                    "raw": raw,
                    "resolved": value,
                    "has_references": "%" in raw,
                }

                # A frame-padded form, for range checks and globbing.
                #
                # NOT nuke.filename(): it substitutes variables when called
                # standalone but returns them UNRESOLVED inside a temporary
                # apply -- it does not refresh within one synchronous handler
                # call. Measured; it silently produced "/jobs/%{show}/..." and
                # would have fed that to the filesystem checks.
                #
                # Instead evaluate at two frames and diff. Whatever differs is
                # the frame field, wherever it appears and however it is
                # padded, and evaluate() is reliable here.
                if knob_name == "file":
                    entry["padded"] = _padded_form(k, raw, value)

                # Optionally resolve at specific frames, without disturbing
                # nuke.frame() -- evaluate() takes a frame argument.
                if frames:
                    at = {}
                    for frame in frames:
                        try:
                            at[str(frame)] = k.evaluate(int(frame))
                        except Exception as exc:
                            at[str(frame)] = "<{}>".format(type(exc).__name__)
                    entry["at_frames"] = at
                if isinstance(value, str) and "%" in value:
                    # Frame padding is expected; a leftover %{...} or %name is not.
                    leftovers = [t for t, _b in _iter_references(value)]
                    entry["unresolved"] = leftovers
                    entry["fully_resolved"] = not leftovers
                else:
                    entry["fully_resolved"] = True
                resolved.append(entry)
        finally:
            _restore_snapshot(knob, snapshot)

    unresolved = [r for r in resolved if not r["fully_resolved"]]
    return {
        "scope": _scope_label(node),
        "variables": variables,
        "paths": resolved,
        "count": len(resolved),
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
        "restored_cleanly": _verify_restored(knob, snapshot),
    }


# ---------------------------------------------------------------------------
# Scoping an existing subgraph
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _selection_preserved():
    """Restore the artist's selection afterwards.

    `nuke.collapseToVariableGroup()` works on the current selection, so using it
    means clobbering whatever the artist had selected. Putting it back is the
    difference between a tool that is safe to run mid-session and one that is
    quietly annoying.
    """
    previous = [n.fullName() for n in nuke.selectedNodes()]
    try:
        yield
    finally:
        for node in nuke.allNodes(recurseGroups=True):
            try:
                node.setSelected(False)
            except Exception:
                pass
        for name in previous:
            node = nuke.toNode(name)
            if node is not None:
                try:
                    node.setSelected(True)
                except Exception:
                    pass


@register_handler("gsv_wrap_in_variable_group")
def gsv_wrap_in_variable_group(params):
    """Move nodes into a new VariableGroup, giving them their own GSV scope.

    This is the "scope a subgraph" operation. Wrapping a single Write is the
    interesting case: each Write then carries its own scope, so several can
    resolve DIFFERENT values for the same variable in one script rather than
    everything following one Root-level switch. Foundry's own docs describe
    VariableGroups passing overrides up the graph so upstream nodes evaluate at
    several values at once; this is how you set that up.

    Built on nuke.collapseToVariableGroup(), which maintains existing
    connections and adds Input/Output nodes. It operates on the SELECTION, so
    the caller's selection is saved and restored.
    """
    node_names = params.get("nodes") or []
    if not node_names:
        raise ValueError("nodes must be a non-empty list of node names")

    nodes = []
    for name in node_names:
        node = nuke.toNode(name)
        if node is None:
            raise LookupError("no such node: {!r}".format(name))
        nodes.append(node)

    group_name = params.get("name")
    variables = params.get("variables") or {}
    for raw_path, value in variables.items():
        _validate_name(_normalize_path(raw_path))
        _validate_value(_normalize_path(raw_path), value)

    if params.get("dry_run"):
        return {
            "dry_run": True, "applied": False,
            "would_wrap": [n.fullName() for n in nodes],
            "would_name": group_name,
            "would_set": variables,
        }

    with _selection_preserved():
        with undo_group("NukeMCP: gsv_wrap_in_variable_group"):
            for node in nuke.allNodes(recurseGroups=True):
                try:
                    node.setSelected(False)
                except Exception:
                    pass
            for node in nodes:
                node.setSelected(True)

            # show=False: do not yank the artist's node graph into the new
            # group's contents, which is what the default does.
            group = nuke.collapseToVariableGroup(False)
            if group is None:
                raise RuntimeError("collapseToVariableGroup returned nothing")

            if group_name:
                group.setName(group_name)

            knob = group.knob("gsv")
            applied, vetoed, errors, created = [], {}, {}, []
            if variables and knob is not None:
                applied, vetoed, errors, created = _apply_variables(
                    knob, variables,
                    create_missing=bool(params.get("create_missing", True)),
                )

    with group:
        contents = [n.name() for n in nuke.allNodes()]

    return {
        "group": group.fullName(),
        "class": group.Class(),
        "wrapped": [n for n in contents if n not in ("Input1", "Output1")],
        "contents": contents,
        "inputs": group.inputs(),
        "applied": applied,
        "created": created,
        "vetoed": vetoed,
        "errors": errors,
        "gsv": knob.value() if knob is not None else None,
        "dry_run": False,
    }
