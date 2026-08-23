"""Characterization tests: assertions about NUKE's behaviour, not ours.

Run:
    Nuke17.0 -ti tests/characterization_gsv_in_nuke.py

Every other suite tests code we wrote. This one tests the assumptions that code
is built on. Most of them are silent failures worked around elsewhere in the
repo, so if Foundry fixes one, nothing else would notice: the guard would keep
passing its own tests while quietly guarding nothing.

This file is therefore the primary record of what Nuke was measured to do --
there is no separate document to fall out of date, because a claim that stops
being true fails here.

Measured against **Nuke 17.0v2** (macOS, arm64). That is the build every
expectation below was taken on; anything else is a comparison, not a baseline.

**Run this first after any Nuke upgrade.** A failure here is not a bug -- it
means Nuke changed and something in the design needs revisiting. Each check
says what to do about it.

Deliberately not covered: `--var` behaviour, which is a real gap rather than a
principled exclusion. Asserting it means rendering, not reading: `--var` does
not apply to a script opened with `scriptOpen()` from Python, so the value
cannot simply be read back in-process. A check would have to run a subprocess
render and inspect the output path. That is worth adding.

(Launching that subprocess costs nothing in licensing -- a Nuke licence covers
the machine, not the running instance, so a headless child alongside a GUI
session needs no second seat.)

Until then, those measurements are written up in the `gsv_get_schema` tool
description, where a caller building a command line will meet them.
"""
import os
import sys

ADDON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nuke_addon")
sys.path.insert(0, ADDON)

import nuke  # noqa: E402

CHANGED = []


def characterize(label, holds, detail="", impact=""):
    """Assert a Nuke behaviour we depend on."""
    print(f"  {'ok  ' if holds else 'CHANGED'}  {label}" + (f" -- {detail}" if detail else ""))
    if not holds:
        CHANGED.append((label, detail, impact))


print("nuke:", nuke.NUKE_VERSION_STRING, "| python:", sys.version.split()[0])

g = nuke.root()["gsv"]
c = nuke.nodes.Constant()
w = nuke.nodes.Write(inputs=[c], name="CW")


def ev(expr):
    w["file"].setValue(expr)
    return w["file"].evaluate()


# ---------------------------------------------------------------------------
print("\n=== knob semantics ===")
# ---------------------------------------------------------------------------
g.setGsvValue("__default__.a", "A")
g.addGsvSet("keepme")
g.setGsvValue("keepme.b", "B")
g.setValue({"__default__": {"a": "A2"}})
characterize(
    "setValue() REPLACES rather than merges",
    "keepme" not in g.value(),
    "sets omitted from the dict are deleted",
    "if this changed, gsv_set_variables could use setValue directly instead of "
    "read-modify-write",
)

g.setGsvValue("__default__.didnotexist", "made")
characterize(
    "setGsvValue CREATES a variable that does not exist",
    g.getGsvValue("__default__.didnotexist") == "made",
    "there is no set-only mode",
    "gsv_set_variable reports these under 'created' and can refuse them with "
    "create_missing=false; if Nuke gained a set-only call, that guard could use it",
)

# ---------------------------------------------------------------------------
print("\n=== scope and inheritance ===")
# ---------------------------------------------------------------------------
parent = nuke.nodes.VariableGroup(name="cparent")
parent["gsv"].setGsvValue("__default__.inherited", "FROM_PARENT")
with parent:
    child = nuke.nodes.VariableGroup(name="cchild")

characterize(
    "a child scope does NOT report an inherited value",
    child["gsv"].getGsvValue("__default__.inherited") is None,
    "getGsvValue returns None despite the parent defining it",
    "gsv_trace computes effective values by walking the parent chain because "
    "of this; if Nuke exposes inheritance now, that logic can be deleted",
)
characterize(
    "Root's value() does not nest VariableGroups",
    "cparent" not in g.value(),
    impact="gsv_list_scopes walks nodes rather than reading Root",
)

for cls, knob in (("VariableGroup", "gsv"), ("LiveGroup", "gsv"),
                  ("VariableSwitch", "variable")):
    node = nuke.createNode(cls, inpanel=False)
    characterize(f"{cls} still carries a {knob!r} knob", node.knob(knob) is not None,
             impact="scope/binding discovery keys off these knobs")
    nuke.delete(node)

# ---------------------------------------------------------------------------
print("\n=== reference grammar ===")
# ---------------------------------------------------------------------------
g.setGsvValue("__default__.shot", "SHOTVAL")
g.setGsvValue("__default__.shot_key", "KEYVAL")
g.addGsvSet("aset")
g.setGsvValue("aset.member", "MEMBERVAL")

for label, expr, expected, note in [
    ("unbraced %shot resolves", "/x/%shot.exr", "/x/SHOTVAL.exr", ""),
    ("braced %{shot} resolves", "/x/%{shot}.exr", "/x/SHOTVAL.exr", ""),
    ("hyphen ENDS a name", "/x/%shot-v01.exr", "/x/SHOTVAL-v01.exr", ""),
    ("underscore CONTINUES a name", "/x/%shot_key.exr", "/x/KEYVAL.exr", ""),
    ("set-qualified needs braces", "/x/%{aset.member}.exr", "/x/MEMBERVAL.exr", ""),
    ("unbraced set-qualified does NOT work", "/x/%aset.member.exr",
     "/x/%aset.member.exr", "may differ if %a is consumed"),
    ("a bare set name has no value", "/x/%{aset}.exr", "/x/%{aset}.exr", ""),
    ("whitespace in braces breaks it", "/x/%{ shot }.exr", "/x/%{ shot }.exr", ""),
    ("a broken reference evaluates to ITSELF", "/x/%{gone}.exr", "/x/%{gone}.exr",
     "this silence is why gsv_validate_references exists"),
]:
    got = ev(expr)
    if label == "unbraced set-qualified does NOT work":
        holds = "MEMBERVAL" not in got
    else:
        holds = got == expected
    characterize(label, holds, f"{expr} -> {got}", note)

# ---------------------------------------------------------------------------
print("\n=== rename does not repair dependents ===")
# ---------------------------------------------------------------------------
g.setGsvValue("__default__.renameme", "RV")
w["file"].setValue("/x/%{renameme}.exr")
vs = nuke.nodes.VariableSwitch(inputs=[c, c], name="CVS")
vs["variable"].setValue("__default__.renameme")
g.renameGsv("__default__.renameme", "renamed")

characterize(
    "renaming leaves a bound VariableSwitch on setErrorValue",
    str(vs["variable"].value()).startswith("setErrorValue"),
    impact="gsv_validate_references detects broken bindings via this marker; "
           "if Nuke repoints the switch now, that detection is obsolete",
)

# ---------------------------------------------------------------------------
print("\n=== callbacks ===")
# ---------------------------------------------------------------------------
set_fired, add_fired = [], []


def _after_set(path):
    set_fired.append(path)


def _after_add(path):
    add_fired.append(path)


# setGsvValue on a NEW variable is an ADD, not a SET -- which callback fires
# depends on whether the variable already exists. A studio guard installed only
# on SetGsvValue will not see variables being created.
nuke.addAfterUserSetGsvValue(_after_set)
nuke.addAfterUserAddGsv(_after_add)

g.setGsvValue("__default__.cb_new", "v")            # this CREATES the variable
after_create = (list(add_fired), list(set_fired))
g.setGsvValue("__default__.cb_new", "v2")           # this UPDATES it
after_update = (list(add_fired), list(set_fired))

nuke.removeAfterUserSetGsvValue(_after_set)
nuke.removeAfterUserAddGsv(_after_add)

characterize(
    "callbacks fire for PYTHON API writes, not just the UI",
    bool(set_fired or add_fired),
    "add={} set={}".format(add_fired, set_fired),
    "every MCP write runs studio callbacks; this is why writes are read back",
)
characterize(
    "creating a variable fires AddGsv and NOT SetGsvValue",
    len(after_create[0]) == 1 and len(after_create[1]) == 0,
    "after create: add={} set={}".format(*after_create),
    "a veto installed only on SetGsvValue does NOT block creation -- guard "
    "AddGsv too",
)
characterize(
    "updating an existing variable fires SetGsvValue and NOT AddGsv",
    len(after_update[0]) == 1 and len(after_update[1]) == 1,
    "after update: add={} set={}".format(*after_update),
    "the two operations are distinguishable by which callback fires",
)

sig = []


def _before(path, value):
    sig.append((path, value))
    return False


# Pre-create it, so the write under test is an UPDATE and routes through
# beforeUserSetGsvValue rather than the add path.
g.setGsvValue("__default__.vetoed", "original")
nuke.addBeforeUserSetGsvValue(_before)
g.setGsvValue("__default__.vetoed", "should_not_land")
nuke.removeBeforeUserSetGsvValue(_before)
characterize(
    "beforeUserSetGsvValue takes (path, value)",
    len(sig) == 1 and len(sig[0]) == 2,
    str(sig),
    "a one-arg callback raises TypeError inside Nuke's dispatcher",
)
characterize(
    "returning False from a before-callback ABORTS the write",
    g.getGsvValue("__default__.vetoed") == "original",
    impact="gsv_set_variable reports this as 'vetoed' rather than success",
)


def _none_before(path, value):
    return None


g.setGsvValue("__default__.nonecb", "before")
nuke.addBeforeUserSetGsvValue(_none_before)
g.setGsvValue("__default__.nonecb", "landed")
nuke.removeBeforeUserSetGsvValue(_none_before)
characterize(
    "returning None does NOT abort (only literal False does)",
    g.getGsvValue("__default__.nonecb") == "landed",
    impact="_doGsvCallbacks tests `== False` explicitly",
)

# ---------------------------------------------------------------------------
# Per-variable metadata: label, tooltip, favourite
#
# All three serialise into the .nk next to the type, and gsv_get_schema now
# reports them. What matters is that our WRITES leave them alone -- if a plain
# value write started clearing a template author's labels, nothing would say so.

g.setGsvValue("__default__.meta_probe", "a")
g.setDataType("__default__.meta_probe", nuke.gsv.DataType.List)
g.setListOptions("__default__.meta_probe", ["a", "b"])
g.setLabel("__default__.meta_probe", "Probe")
g.setTooltip("__default__.meta_probe", "a tooltip")
g.setFavorite("__default__.meta_probe", True)


def _meta(path):
    return (
        g.getLabel(path),
        g.getTooltip(path),
        g.isFavorite(path),
        str(g.getDataType(path)),
        list(g.getListOptions(path)),
    )


before = _meta("__default__.meta_probe")
g.setGsvValue("__default__.meta_probe", "b")
characterize(
    "a two-argument setGsvValue PRESERVES type, options, label, tooltip and favourite",
    _meta("__default__.meta_probe") == before,
    f"before={before} after={_meta('__default__.meta_probe')}",
    "if this now clears them, every value write silently strips a template's "
    "authored metadata -- gsv_set_variable would need to save and restore it",
)

g.renameGsv("__default__.meta_probe", "meta_renamed")
characterize(
    "renameGsv PRESERVES the same five",
    _meta("__default__.meta_renamed") == before,
    f"after rename={_meta('__default__.meta_renamed')}",
    "gsv_rename_variable would have to carry the metadata across by hand",
)

g.removeGsv("__default__.meta_renamed")

# ---------------------------------------------------------------------------
print("\n" + "=" * 66)
if CHANGED:
    print(f"NUKE BEHAVIOUR CHANGED IN {len(CHANGED)} PLACE(S) -- not necessarily a bug:")
    for label, detail, impact in CHANGED:
        print(f"\n  {label}")
        if detail:
            print(f"    observed: {detail}")
        if impact:
            print(f"    revisit : {impact}")
    print("\nRe-check the guards named above, and update the expectations here.")
    sys.exit(1)
print("every measured Nuke behaviour still holds")
