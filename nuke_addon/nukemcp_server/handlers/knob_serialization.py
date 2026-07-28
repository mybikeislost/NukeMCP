"""Per-knob JSON serialization.

`knob.value()` returns wildly different Python types depending on knob
class (float, tuple, str, a nuke.Format object, ...) and for an animated
or expression-driven knob it silently returns only the value evaluated at
the current frame, hiding the fact that it's animated. Confirmed via
Nuke's own docs (PythonDevGuide/Nuke/animation.html) that the correct way
to detect this is `knob.isAnimated()` (true for animation OR expressions)
and `knob.hasExpression()` (true specifically for expressions) -- so we
report those alongside the evaluated value instead of collapsing
everything to a bare `.value()` call.
"""

import nuke


def serialize_knob(knob):
    knob_type = None
    try:
        knob_type = knob.Class()
    except Exception:
        pass

    # Multi-component knobs (AColor_Knob, XY_Knob, UV_Knob, etc.) report
    # arraySize() > 1. knob.value() returns only the first component for
    # these, which hides the vector nature entirely. Use getValue(i) instead
    # to expose all components as a list so the LLM knows the full shape.
    arr_size = 1
    try:
        arr_size = knob.arraySize()
    except AttributeError:
        pass

    try:
        if arr_size > 1:
            value = [knob.getValue(i) for i in range(arr_size)]
        else:
            value = _to_jsonable(knob.value())
    except Exception as exc:
        return {"value": None, "error": str(exc), "animated": False,
                "expression": None, "knob_type": knob_type}

    animated = False
    expression = None
    try:
        animated = bool(knob.isAnimated())
        if knob.hasExpression():
            # toScript() reflects the current expression text for single-field
            # knobs; for array knobs, fall back to a generic marker rather than
            # guessing which field holds the expression.
            try:
                expression = knob.toScript(False)
            except Exception:
                expression = True
    except (AttributeError, NameError):
        pass  # not every knob subclass supports animation (e.g. Tab_Knob)

    return {"value": value, "animated": animated, "expression": expression,
            "knob_type": knob_type}


def _to_jsonable(value):
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "name"):
        # nuke.Format and similar objects expose .name()
        try:
            return value.name()
        except Exception:
            pass
    return str(value)


def serialize_all_knobs(node):
    result = {}
    for name, knob in node.knobs().items():
        result[name] = serialize_knob(knob)
    return result
