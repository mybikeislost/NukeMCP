"""Tool-name -> handler registry.

`handle()` is the function that actually runs ON NUKE'S MAIN THREAD -- it is
passed as the `call` argument to `nuke.executeInMainThreadWithResult` by
listener.py. Keep this module free of socket/IO concerns; those belong in
listener.py. `import nuke` (beyond this file) should only appear in
handlers/*.py.

IMPORTANT: confirmed empirically (not just from docs) that
`nuke.executeInMainThreadWithResult` does NOT propagate exceptions raised
inside the dispatched callable back to the calling thread -- Nuke's own
`executeInMain.py` wrapper catches them, prints a traceback to Nuke's
console/log, and the caller just gets back `None`. So `handle()` must
catch its own exceptions and return a plain envelope dict instead of
raising -- raising here would silently look like success-with-null-result
to whoever called executeInMainThreadWithResult.
"""

_HANDLERS = {}

# Guards against a "batch" request recursively containing another "batch".
# Safe as a plain module-level bool: all handlers run on the single main thread.
_batch_active = False


def register_handler(name):
    def _decorator(func):
        _HANDLERS[name] = func
        return func
    return _decorator


def handle(request):
    tool_name = request.get("tool")
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return {"ok": False, "message": "no such tool: {!r}".format(tool_name), "error_type": "UnknownToolError"}
    try:
        result = handler(request.get("params") or {})
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "error_type": type(exc).__name__}


def _handle_one(tool_name, params):
    """Call a handler directly (no envelope wrapping). Used by batch."""
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        raise KeyError("no such tool: {!r}".format(tool_name))
    return handler(params)


def _load_handlers():
    # Importing these modules triggers their @register_handler decorators.
    from .handlers import (  # noqa: F401
        animation,
        diagnostics,
        execute,
        graph,
        gsv,
        layout,
        nodes,
        templates,
        render,
        script_info,
        script_io,
        selection,
        viewer,
    )
    # batch is defined inline below after _HANDLERS is populated.
    _register_batch()


def _register_batch():
    from .nuke_compat import undo_group

    @register_handler("batch")
    def batch(params):
        """Execute multiple tool operations as a single undoable action.

        Each operation is `{"tool": "<name>", "params": {...}}`.
        If stop_on_error is true, execution halts on the first failure.
        The whole batch is wrapped in one undo group named by `label`.
        """
        global _batch_active
        if _batch_active:
            raise RuntimeError("nested batch calls are not supported")

        operations = params.get("operations") or []
        stop_on_error = bool(params.get("stop_on_error", False))
        label = str(params.get("label", "batch"))

        results = []
        _batch_active = True
        try:
            with undo_group("NukeMCP: {}".format(label)):
                for op in operations:
                    tool_name = op.get("tool", "")
                    op_params = op.get("params") or {}
                    try:
                        result = _handle_one(tool_name, op_params)
                        results.append({"ok": True, "tool": tool_name, "result": result})
                    except Exception as exc:
                        results.append({
                            "ok": False,
                            "tool": tool_name,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        })
                        if stop_on_error:
                            break
        finally:
            _batch_active = False

        return {
            "results": results,
            "count": len(results),
            "all_ok": all(r["ok"] for r in results),
        }


_load_handlers()
