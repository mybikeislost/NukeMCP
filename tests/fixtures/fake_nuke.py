"""A `nuke` module stand-in, so addon code can be imported without Nuke.

Only enough to satisfy import time. Every handler module does `import nuke` at
the top but never calls into it until a handler actually runs, so the pure
helpers -- path parsing, value validation, diffing, reference scanning -- can
be unit tested in CI with no Nuke installation and no licence.

Anything that genuinely needs Nuke's behaviour belongs in a *_in_nuke.py suite
instead; see tests/README.md. Do not grow this into a Nuke emulator. A fake
that reimplements Nuke's semantics would only ever test our guesses about them.
"""

import sys
import types


class _DataType:
    Invalid = "Invalid"
    String = "String"
    List = "List"


def install():
    """Register the stub as `nuke` in sys.modules. Idempotent."""
    if "nuke" in sys.modules and getattr(sys.modules["nuke"], "_is_fake", False):
        return sys.modules["nuke"]

    nuke = types.ModuleType("nuke")
    nuke._is_fake = True
    nuke.NUKE_VERSION_STRING = "0.0v0 (fake)"
    nuke.GUI = False

    gsv = types.ModuleType("nuke.gsv")
    gsv.DataType = _DataType
    nuke.gsv = gsv

    def _needs_real_nuke(name):
        def _stub(*_args, **_kwargs):
            raise RuntimeError(
                "nuke.{}() needs a real Nuke -- move this assertion into a "
                "tests/*_in_nuke.py suite".format(name)
            )
        return _stub

    for name in ("root", "toNode", "allNodes", "selectedNodes", "createNode",
                 "delete", "undo", "message", "scriptSaveAs"):
        setattr(nuke, name, _needs_real_nuke(name))

    # Two exceptions to "everything raises", both needed to exercise the socket
    # listener without Nuke.
    nuke.tprint = lambda *args, **kwargs: None

    def _execute_in_main_thread_with_result(call, args=(), kwargs=None):
        """Call straight through instead of marshalling to Nuke's main thread.

        This is the ONE piece of Nuke behaviour the stub imitates, and it is a
        deliberate hole: the real function hands work to Nuke's event loop and
        blocks for the result. Calling directly exercises everything around the
        bridge -- accept loop, framing, dispatch, error envelopes -- but NOT the
        marshalling itself, which needs a GUI session.

        It also matches the real contract in the way that matters for error
        handling: exceptions raised inside `call` do not propagate out, they
        surface as a None result. dispatch.handle() never raises anyway, but
        the listener has a branch for it.
        """
        try:
            return call(*args, **(kwargs or {}))
        except Exception:
            return None

    nuke.executeInMainThreadWithResult = _execute_in_main_thread_with_result

    nuke.Undo = types.SimpleNamespace(
        begin=lambda *_a, **_k: None, end=lambda *_a, **_k: None
    )

    sys.modules["nuke"] = nuke
    sys.modules["nuke.gsv"] = gsv
    return nuke
