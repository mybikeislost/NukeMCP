"""Phase 1 GSV acceptance test, run inside a real Nuke.

Not named test_* on purpose: it needs a live `nuke` module, so pytest must not
collect it. The offline suite (test_*.py) runs without Nuke.

Run:
    Nuke17.0 -ti tests/integration_gsv_in_nuke.py

Exercises every GSV tool against real Nuke:
  1. reads actual Nuke state
  2. shows a structured diff
  3. modifies only the requested value
  4. preserves all unrelated GSV data
  5. is undoable            <- reported only; nuke.Undo is inert under -t
  6. returns resulting state
  7. does not use raw generated Python

Calls dispatch.handle() directly rather than going through the socket, for the
reason given below.
"""
import json
import os
import sys

# tests/ -> repo root -> nuke_addon/
ADDON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nuke_addon")
sys.path.insert(0, ADDON)

import nuke  # noqa: E402

from nukemcp_server import dispatch  # noqa: E402

FAILURES = []


def call(tool, params=None):
    out = dispatch.handle({"id": 1, "tool": tool, "params": params or {}})
    if not out.get("ok"):
        FAILURES.append((tool, out.get("message")))
        return None
    return out.get("result")


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append((label, detail))


print("nuke:", nuke.NUKE_VERSION_STRING, "| gui:", nuke.GUI)

# --- fixture: a master-template-ish GSV layout -----------------------------
g = nuke.root()["gsv"]
g.setGsvValue("__default__.show", "nike")
g.setGsvValue("__default__.sequence", "sq010")
g.setGsvValue("__default__.shot", "sh010")
g.addGsvSet("delivery")
g.setGsvValue("delivery.quality", "final")
g.setGsvValue("delivery.codec", "prores")
seq_group = nuke.nodes.VariableGroup(name="sq010_grp")
seq_group["gsv"].setGsvValue("__default__.shot", "sh099")

print("\n--- 1. read actual state ---")
state = call("gsv_get")
print("   ", json.dumps(state, default=str)[:200])
check("root reports both sets", set(state["set_names"]) == {"__default__", "delivery"},
      str(state["set_names"]))
check("variable_count counts across sets", state["variable_count"] == 5,
      str(state["variable_count"]))
check("shot reads sh010", state["sets"]["__default__"]["shot"] == "sh010")

print("\n--- scopes ---")
scopes = call("gsv_list_scopes")
names = [s["scope"] for s in scopes["scopes"]]
print("   ", names)
check("root and the VariableGroup are both listed",
      "root" in names and "sq010_grp" in names, str(names))
vg = next(s for s in scopes["scopes"] if s["scope"] == "sq010_grp")
check("VariableGroup's parent is root", vg["parent_scope"] == "root", vg["parent_scope"])
check("VariableGroup declares only its own override",
      vg["defines"] == ["__default__.shot"], str(vg["defines"]))

print("\n--- single variable, with type info ---")
one = call("gsv_get_variable", {"path": "shot"})
check("bare name resolves to the __default__ set", one["path"] == "__default__.shot", one["path"])
check("value is sh010", one["value"] == "sh010")
check("defined_in_scope true", one["defined_in_scope"] is True)
missing = call("gsv_get_variable", {"path": "__default__.not_a_var"})
check("undefined variable reports null, not an error",
      missing["value"] is None and missing["defined_in_scope"] is False)

print("\n--- 2. structured diff: shot sh010 -> sh020 ---")
d = call("gsv_diff", {"variables": {"__default__.shot": "sh020"}})
print("   ", json.dumps(d["changes"], default=str))
check("exactly one change", d["would_change"] == 1, str(d["changes"]))
check("change is a modify with correct before/after",
      d["changes"][0] == {"path": "__default__.shot", "action": "modify",
                          "before": "sh010", "after": "sh020"},
      str(d["changes"][0]))
check("diff wrote nothing", g.getGsvValue("__default__.shot") == "sh010",
      g.getGsvValue("__default__.shot"))

print("\n--- dry run writes nothing ---")
dr = call("gsv_set_variable", {"path": "__default__.shot", "value": "sh020", "dry_run": True})
check("dry_run flagged", dr["dry_run"] is True)
check("dry_run applied nothing", dr["applied"] == [])
check("script untouched after dry run", g.getGsvValue("__default__.shot") == "sh010")

print("\n--- 3+4. apply: only shot changes, everything else preserved ---")
before_full = g.value()
res = call("gsv_set_variable", {"path": "__default__.shot", "value": "sh020"})
check("applied the one path", res["applied"] == ["__default__.shot"], str(res["applied"]))
check("no vetoes, no errors", res["all_ok"] is True, str(res))
check("shot is now sh020", g.getGsvValue("__default__.shot") == "sh020")
check("delivery set fully preserved",
      g.value()["delivery"] == before_full["delivery"], str(g.value().get("delivery")))
check("unrelated __default__ vars preserved",
      g.getGsvValue("__default__.show") == "nike"
      and g.getGsvValue("__default__.sequence") == "sq010")
check("VariableGroup override untouched",
      seq_group["gsv"].getGsvValue("__default__.shot") == "sh099")

print("\n--- 6. returns resulting state ---")
check("reports one change", len(res["changes"]) == 1, str(res["changes"]))
check("after-state present and correct",
      res["after"]["__default__"]["shot"] == "sh020")

print("\n--- string-only enforcement ---")
for bad, kind in [(1001, "int"), (["a"], "list"), (True, "bool"), (None, "None")]:
    r = call("gsv_set_variable", {"path": "__default__.frame", "value": bad})
    rejected = bool(r["errors"]) and not r["applied"]
    check(f"{kind} rejected with a clear error", rejected,
          list(r["errors"].values())[0][:60] if r["errors"] else "ACCEPTED -- should not be")
check("nothing was written by the rejected attempts",
      g.getGsvValue("__default__.frame") is None)

print("\n--- bulk set, and set auto-creation ---")
bulk = call("gsv_set_variables", {"variables": {
    "__default__.shot": "sh030",
    "__default__.comp_version": "v012",
    "plates.plate_version": "v004",
}})
check("all three applied", len(bulk["applied"]) == 3, str(bulk["applied"]))
check("named set was auto-created", "plates" in g.value(), str(list(g.value())))
check("plates.plate_version landed", g.getGsvValue("plates.plate_version") == "v004")
check("set_changes reports the new set",
      any(c["set"] == "plates" and c["action"] == "add" for c in bulk["set_changes"]),
      str(bulk["set_changes"]))
check("delivery STILL preserved after bulk write",
      g.value()["delivery"] == before_full["delivery"], str(g.value().get("delivery")))

print("\n--- the setValue() replace trap: unrelated sets must survive ---")
snapshot = g.value()
call("gsv_set_variables", {"variables": {"__default__.show": "adidas"}})
now = g.value()
check("every pre-existing set still present",
      set(snapshot) == set(now), f"{sorted(snapshot)} vs {sorted(now)}")
check("every unrelated variable still present",
      all(now[s].get(k) == v for s in snapshot for k, v in snapshot[s].items()
          if not (s == "__default__" and k == "show")))

print("\n--- scope targeting ---")
r = call("gsv_set_variable", {"path": "__default__.shot", "value": "sh777", "scope": "sq010_grp"})
check("wrote into the VariableGroup", seq_group["gsv"].getGsvValue("__default__.shot") == "sh777")
check("root's shot unaffected by the group write",
      g.getGsvValue("__default__.shot") == "sh030", g.getGsvValue("__default__.shot"))
bad_scope = dispatch.handle({"id": 1, "tool": "gsv_get", "params": {"scope": "NoSuchNode"}})
check("unknown scope errors cleanly", bad_scope.get("error_type") == "LookupError",
      str(bad_scope.get("error_type")))

print("\n--- setting a variable that does not exist CREATES it ---")
# setGsvValue creates on write, so a typo'd path invents a variable and reports
# success. Creations are now reported separately, and refusable.
before_names = set(g.value()["__default__"])
typo = call("gsv_set_variable", {"path": "__default__.shto", "value": "sh020"})
check("the write succeeded", typo["applied"] == ["__default__.shto"], str(typo["applied"]))
check("but it is reported as CREATED, not silently applied",
      typo["created"] == ["__default__.shto"], str(typo.get("created")))
check("and the diff shows it as an add",
      any(c["action"] == "add" and c["path"] == "__default__.shto" for c in typo["changes"]),
      str(typo["changes"]))
g.removeGsv("__default__.shto")

strict = call("gsv_set_variable",
              {"path": "__default__.shto", "value": "sh020", "create_missing": False})
check("create_missing=false refuses instead", strict["applied"] == [], str(strict["applied"]))
check("with an error naming the likely cause",
      "typo" in list(strict["errors"].values())[0] if strict["errors"] else False,
      str(strict["errors"]))
check("and nothing was written", g.getGsvValue("__default__.shto") is None)

existing = call("gsv_set_variable",
                {"path": "__default__.show", "value": "adidas2", "create_missing": False})
check("an EXISTING variable still writes under create_missing=false",
      existing["applied"] == ["__default__.show"] and existing["created"] == [],
      str(existing["applied"]))

print("\n--- a vetoing callback must be reported as vetoed, not success ---")
# Nuke's addBeforeUser* callbacks fire on Python API writes and can return
# False to abort them. A studio callback doing that is why every write here is
# read back: reporting a change that did not happen is worse than failing.
_vetoed_paths = []


def _veto_everything(path, value):
    # (path, value) -- NOT (path). Confirmed against Nuke's own
    # nuke_internal/callbacks.py: beforeUserSetGsvValue(path, value), while
    # afterUserSetGsvValue(path) takes only one. The docs describe neither.
    _vetoed_paths.append((path, value))
    # _doGsvCallbacks tests `== False` explicitly, so returning None does not
    # abort -- it has to be literal False.
    return False


nuke.addBeforeUserSetGsvValue(_veto_everything)
try:
    g.setGsvValue("__default__.veto_probe", "before")
    res_v = call("gsv_set_variable",
                 {"path": "__default__.veto_probe", "value": "after"})
    if _vetoed_paths:
        check("callback actually fired", True, str(_vetoed_paths[:1]))
        check("write reported as NOT applied", res_v["applied"] == [], str(res_v["applied"]))
        check("and reported under 'vetoed'", "__default__.veto_probe" in res_v["vetoed"],
              str(res_v["vetoed"]))
        check("all_ok is False", res_v["all_ok"] is False)
        check("value genuinely unchanged",
              g.getGsvValue("__default__.veto_probe") == "before",
              g.getGsvValue("__default__.veto_probe"))
        check("vetoed entry says what was requested vs actual",
              res_v["vetoed"]["__default__.veto_probe"]["requested"] == "after",
              str(res_v["vetoed"]))
    else:
        print("  ----  beforeUserSetGsvValue did not fire for an API write; "
              "veto path unverified on this build")
finally:
    nuke.removeBeforeUserSetGsvValue(_veto_everything)

# and confirm removing the callback restores normal writes
res_ok = call("gsv_set_variable", {"path": "__default__.veto_probe", "value": "after"})
check("writes succeed again once the callback is removed",
      res_ok["all_ok"] is True and g.getGsvValue("__default__.veto_probe") == "after")

print("\n--- 5. undo ---")
n_before = g.getGsvValue("__default__.show")
call("gsv_set_variable", {"path": "__default__.show", "value": "undo_probe"})
nuke.undo()
n_after = g.getGsvValue("__default__.show")
if n_after == n_before:
    print(f"  PASS  GSV write is undoable ({n_before!r} restored)")
else:
    print(f"  ----  undo inert under -t (expected): {n_before!r} -> {n_after!r}; retest in GUI")

print("\n" + "=" * 62)
if FAILURES:
    print(f"FAILURES ({len(FAILURES)}):")
    for label, detail in FAILURES:
        print(f"  {label}: {detail}")
    sys.exit(1)
print("ALL PHASE 1 CHECKS PASSED")
