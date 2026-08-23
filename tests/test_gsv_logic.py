"""Offline unit tests for the GSV handler's pure logic.

Runs in CI with no Nuke and no licence. Everything here is string/dict work
that does not touch the Nuke API, so it is cheap to run on every commit --
which matters, because the one real bug found in this code so far was in the
reference regex, pure string logic that a manual licensed Nuke run happened to
catch late.

The expected values encode behaviour MEASURED against real Nuke, and asserted
against it in tests/characterization_gsv_in_nuke.py. When Nuke's behaviour is the thing under
test, the assertion belongs in a *_in_nuke.py suite instead; these tests only
check that our parsing agrees with what we measured.
"""

import importlib
import pathlib
import sys

import pytest

import fake_nuke

fake_nuke.install()

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "nuke_addon"))

gsv = importlib.import_module("nukemcp_server.handlers.gsv")


# ---------------------------------------------------------------------------
# Reference parsing -- both syntaxes, measured in Nuke 17
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # braced
    ("%{shot}", [("shot", True)]),
    ("/x/%{seq}/%{shot}.exr", [("seq", True), ("shot", True)]),
    ("%{delivery.quality}", [("delivery.quality", True)]),
    # Nuke does NOT resolve "%{ shot }" -- yielded unstripped so it fails to
    # match a real variable and is reported broken, rather than silently
    # "corrected" into a reference Nuke never honours.
    ("%{ shot }", [(" shot ", True)]),
    # "%{%shot}" resolves the INNER reference, leaving the braces literal.
    ("%{%shot}", [("shot", False)]),
    # unbraced -- Foundry's own docs use this form, and the first version of
    # this regex missed it entirely
    ("%shot", [("shot", False)]),
    ("/Volumes/%shot/file-####.exr", [("shot", False)]),
    ("/x/%show/%shot.exr", [("show", False), ("shot", False)]),
    # an unbraced name stops at a dot but keeps underscores
    ("/x/%shot.exr", [("shot", False)]),
    ("/x/%shot_key.exr", [("shot_key", False)]),
    ("/x/%{shot}_key.exr", [("shot", True)]),
    # mixed
    ("%{show}/%shot", [("show", True), ("shot", False)]),
    # A numeric knob's expression, as knob.toScript() returns it. value()
    # would have returned the evaluated number with no reference in it.
    ("{%blur_size}", [("blur_size", False)]),
    ("{%{delivery.quality}}", [("delivery.quality", True)]),
    # nothing to find
    ("/x/plain/path.exr", []),
    ("%{}", []),
    ("", []),
])
def test_reference_forms(raw, expected):
    assert list(gsv._iter_references(raw)) == expected


@pytest.mark.parametrize("terminator", list("-./\\+=:;,@#^&*()[]<>?!~| '"))
def test_these_characters_end_an_unbraced_name(terminator):
    """Measured against Nuke: the name spans [A-Za-z0-9_] and stops at anything else."""
    assert list(gsv._iter_references("/x/%shot" + terminator + "Z.exr")) == [("shot", False)]


def test_percent_both_ends_a_name_and_starts_another():
    """"/x/%shot%Z.exr" is two candidates, not one.

    Nuke resolves shot and leaves "%Z" literal only because no variable Z
    exists -- so reporting both is correct, and validate_references is what
    decides which of them actually resolve.
    """
    assert list(gsv._iter_references("/x/%shot%Z.exr")) == [("shot", False), ("Z", False)]


@pytest.mark.parametrize("continuation", list("_9Zz0"))
def test_these_characters_continue_an_unbraced_name(continuation):
    """Word characters are part of the name, so %shot_key means "shot_key"."""
    got = list(gsv._iter_references("/x/%shot" + continuation + "Z.exr"))
    assert got == [("shot" + continuation + "Z", False)]


@pytest.mark.parametrize("raw", [
    "/out/plate.%04d.exr",   # frame padding
    "/out/plate.%08d.exr",
    "/out/%v/plate.exr",     # view token
    "/out/%V/plate.exr",
    "/out/plate.%d.exr",     # bare frame token
])
def test_nuke_path_tokens_are_not_treated_as_variables(raw):
    """%04d / %v / %V are Nuke's own, not GSV references.

    A deliberate heuristic, not a rule: a DEFINED variable does win over the
    path token (verified -- %view resolves when a `view` variable exists). The
    cost is missing a reference to a variable literally named "d" or "v", which
    is worth paying to avoid flagging every frame-padded path in the script.
    """
    assert list(gsv._iter_references(raw)) == []


def test_a_defined_looking_name_starting_with_d_is_still_found():
    """Only the bare tokens are excluded, not every name beginning with them.

    Nuke resolves %delivery to a variable called `delivery` when one exists;
    it only falls through to %d + "elivery" when none does.
    """
    assert list(gsv._iter_references("/x/%delivery.exr")) == [("delivery", False)]
    assert list(gsv._iter_references("/x/%done.exr")) == [("done", False)]


# ---------------------------------------------------------------------------
# VariableSwitch bindings -- structural dependencies with no "%" in them
# ---------------------------------------------------------------------------

class _FakeEnumKnob:
    """Stands in for a VariableSwitch `variable` Enumeration_Knob.

    Shapes taken verbatim from a real session; the VariableSwitch checks in
    tests/characterization_gsv_in_nuke.py assert Nuke still produces them.
    """

    def __init__(self, value, index=0, names=()):
        self._value, self._index, self._names = value, index, names

    def value(self):
        return self._value

    def getValue(self):
        return self._index

    def enumName(self, i):
        return self._names[i]

    def Class(self):
        return "Enumeration_Knob"


def test_switch_binding_reads_the_bound_path():
    knob = _FakeEnumKnob(
        '{1} "None\t " "__default__.shot\tshot"',
        index=1,
        names=("None\t ", "__default__.shot\tshot"),
    )
    assert gsv._switch_binding(knob) == ("__default__.shot", False)


def test_switch_binding_detects_nukes_error_marker():
    """Renaming out from under a switch leaves setErrorValue and it stops working."""
    knob = _FakeEnumKnob(
        'setErrorValue __default__.shot {{2} "None\t " '
        '"__default__.shot_renamed\tshot_renamed" __default__.shot}'
    )
    assert gsv._switch_binding(knob) == ("__default__.shot", True)


def test_switch_binding_ignores_the_unbound_entry():
    knob = _FakeEnumKnob('{0} "None\t "', index=0, names=("None\t ",))
    assert gsv._switch_binding(knob) == (None, False)


def test_switch_binding_ignores_a_non_gsv_enumeration():
    """Every real GSV path is set-qualified, so an unqualified entry is not one."""
    knob = _FakeEnumKnob('{1} "off" "on"', index=1, names=("off", "on"))
    assert gsv._switch_binding(knob) == (None, False)


def test_switch_binding_survives_a_knob_that_raises():
    class _Boom:
        def value(self):
            raise RuntimeError("no")
        def Class(self):
            return "Enumeration_Knob"
    assert gsv._switch_binding(_Boom()) == (None, False)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    ("shot", "__default__.shot"),
    ("  shot  ", "__default__.shot"),
    ("__default__.shot", "__default__.shot"),
    ("delivery.quality", "delivery.quality"),
])
def test_normalize_path(given, expected):
    assert gsv._normalize_path(given) == expected


@pytest.mark.parametrize("bad", ["", "   ", None, 42, []])
def test_normalize_path_rejects_junk(bad):
    with pytest.raises(ValueError):
        gsv._normalize_path(bad)


@pytest.mark.parametrize("name", ["shot", "_lead", "shot2", "SHOT", "__default__"])
def test_valid_gsv_names_accepted(name):
    assert gsv._validate_name("__default__." + name) is not None


@pytest.mark.parametrize("name", ["has-hyphen", "has space", "2leading", ""])
def test_invalid_gsv_names_rejected(name):
    """Nuke stores nothing for these and does not raise -- so we raise instead."""
    with pytest.raises(ValueError) as exc:
        gsv._validate_name("__default__." + name)
    assert "GSV names must match" in str(exc.value)


def test_invalid_set_name_rejected_too():
    with pytest.raises(ValueError) as exc:
        gsv._validate_name("bad-set.shot")
    assert "invalid set name" in str(exc.value)


def test_split_path():
    assert gsv._split_path("delivery.quality") == ("delivery", "quality")
    assert gsv._split_path("shot") == ("__default__", "shot")


# ---------------------------------------------------------------------------
# Value validation -- Nuke rejects non-strings outright, so we do too
# ---------------------------------------------------------------------------

def test_strings_accepted():
    assert gsv._validate_value("__default__.shot", "sh020") == "sh020"
    assert gsv._validate_value("__default__.shot", "") == ""


@pytest.mark.parametrize("value", [1001, 1.5, ["a"], {"a": 1}, True, False])
def test_non_strings_rejected_not_coerced(value):
    """Coercing would silently turn a caller's frame number into "1001"."""
    with pytest.raises(TypeError) as exc:
        gsv._validate_value("__default__.frame", value)
    assert "must be strings" in str(exc.value)


def test_none_points_at_the_removal_tool():
    with pytest.raises(ValueError) as exc:
        gsv._validate_value("__default__.shot", None)
    assert "gsv_remove_variable" in str(exc.value)


# ---------------------------------------------------------------------------
# Flatten / diff
# ---------------------------------------------------------------------------

def test_flatten_keeps_the_set_layer_in_the_key():
    assert gsv._flatten({
        "__default__": {"shot": "sh010"},
        "delivery": {"quality": "final"},
    }) == {"__default__.shot": "sh010", "delivery.quality": "final"}


def test_flatten_handles_empty_and_none():
    assert gsv._flatten({}) == {}
    assert gsv._flatten(None) == {}
    assert gsv._flatten({"__default__": {}}) == {}


def test_diff_reports_modify_add_and_remove():
    before = {"__default__": {"shot": "sh010", "gone": "x"}}
    after = {"__default__": {"shot": "sh020", "added": "y"}}
    changes, set_changes = gsv._diff(before, after)
    assert changes == [
        {"path": "__default__.added", "action": "add", "before": None, "after": "y"},
        {"path": "__default__.gone", "action": "remove", "before": "x", "after": None},
        {"path": "__default__.shot", "action": "modify", "before": "sh010", "after": "sh020"},
    ]
    assert set_changes == []


def test_diff_reports_set_level_changes():
    before = {"__default__": {}, "delivery": {"quality": "final"}}
    after = {"__default__": {}, "plates": {"v": "v004"}}
    _changes, set_changes = gsv._diff(before, after)
    assert set_changes == [
        {"set": "plates", "action": "add"},
        {"set": "delivery", "action": "remove"},
    ]


def test_diff_of_identical_state_is_empty():
    state = {"__default__": {"shot": "sh010"}, "delivery": {"quality": "final"}}
    assert gsv._diff(state, dict(state)) == ([], [])


def test_diff_does_not_confuse_same_name_in_different_sets():
    before = {"a": {"shot": "1"}, "b": {"shot": "2"}}
    after = {"a": {"shot": "1"}, "b": {"shot": "9"}}
    changes, _ = gsv._diff(before, after)
    assert changes == [
        {"path": "b.shot", "action": "modify", "before": "2", "after": "9"},
    ]
