import contextlib
import io
import os
import tempfile
import threading
import uuid

import nuke

from ..dispatch import register_handler


def _parse_frame_range(spec):
    """Parse a frame range spec into a sorted list of (first, last) segments.

    Accepts:
      "1-5"           -> [(1, 5)]
      "7"             -> [(7, 7)]
      "1-5,7,9-12"   -> [(1, 5), (7, 7), (9, 12)]
    """
    segments = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            segments.append((int(lo), int(hi)))
        else:
            f = int(part)
            segments.append((f, f))
    if not segments:
        raise ValueError("empty frame range spec: {!r}".format(spec))
    return segments


@register_handler("render")
def render(params):
    node_name = params.get("node_name")
    proxy_mode = bool(params.get("proxy_mode", False))

    # Resolve frame range -- compound spec takes priority over first/last ints.
    frame_range_str = params.get("frame_range")
    if frame_range_str:
        segments = _parse_frame_range(frame_range_str)
    elif "first_frame" in params and "last_frame" in params:
        segments = [(int(params["first_frame"]), int(params["last_frame"]))]
    else:
        raise ValueError(
            "provide either 'frame_range' (e.g. '1-5,7,9-12') "
            "or both 'first_frame' and 'last_frame'"
        )

    if node_name:
        node = nuke.toNode(node_name)
        if node is None:
            raise LookupError("no such node: {!r}".format(node_name))
        targets = node
    else:
        targets = nuke.allNodes("Write")
        if not targets:
            raise ValueError("no node_name given and no Write nodes exist in the script")

    # Apply proxy mode for the duration of this render only.
    root = nuke.root()
    prev_proxy = None
    if proxy_mode:
        prev_proxy = root["proxy"].value()
        root["proxy"].setValue(True)

    # nuke.execute()'s own exception is the authoritative success/failure signal.
    # Captured stdout/stderr is best-effort supplementary info -- Nuke's render
    # engine may log via its own path rather than Python's sys.stdout, so don't
    # rely on an empty buffer to mean "no warnings."
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    success, error_message = True, None
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            for first, last in segments:
                nuke.execute(targets, first, last)
    except Exception as exc:
        success, error_message = False, str(exc)
    finally:
        if prev_proxy is not None:
            root["proxy"].setValue(prev_proxy)

    return {
        "success": success,
        "error": error_message,
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "segments_rendered": segments,
        "proxy_mode": proxy_mode,
    }


# ---------------------------------------------------------------------------
# Async job variant: render_start / render_status.
#
# Used by the MCP-side `render` tool so it can stream per-frame progress via
# ctx.report_progress instead of blocking silently for the whole render. Each
# frame is dispatched via its own nuke.executeInMainThreadWithResult() call
# from a dedicated background thread (spawned by render_start, NOT the
# connection thread), so the driving loop never occupies the main thread
# itself -- if it did, it would deadlock against the very frame calls it's
# trying to submit. The synchronous `render` handler above is left completely
# unchanged for any direct/raw-socket caller.
# ---------------------------------------------------------------------------

_JOBS_LOCK = threading.Lock()
_JOBS = {}  # job_id -> dict


def _resolve_render_params(params):
    node_name = params.get("node_name")
    proxy_mode = bool(params.get("proxy_mode", False))

    frame_range_str = params.get("frame_range")
    if frame_range_str:
        segments = _parse_frame_range(frame_range_str)
    elif "first_frame" in params and "last_frame" in params:
        segments = [(int(params["first_frame"]), int(params["last_frame"]))]
    else:
        raise ValueError(
            "provide either 'frame_range' (e.g. '1-5,7,9-12') "
            "or both 'first_frame' and 'last_frame'"
        )

    if node_name:
        node = nuke.toNode(node_name)
        if node is None:
            raise LookupError("no such node: {!r}".format(node_name))
        targets = node
    else:
        targets = nuke.allNodes("Write")
        if not targets:
            raise ValueError("no node_name given and no Write nodes exist in the script")

    return targets, segments, proxy_mode


@register_handler("render_start")
def render_start(params):
    """Start a render job in the background and return immediately with a
    job_id. Poll progress with render_status. Runs on the main thread just
    long enough to resolve targets and flip proxy mode, then hands off to a
    background driver thread before returning.
    """
    targets, segments, proxy_mode = _resolve_render_params(params)

    root = nuke.root()
    prev_proxy = None
    if proxy_mode:
        prev_proxy = root["proxy"].value()
        root["proxy"].setValue(True)

    total_frames = sum(last - first + 1 for first, last in segments)
    job_id = uuid.uuid4().hex

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "status": "running",
            "done": 0,
            "total": total_frames,
            "segments": segments,
            "proxy_mode": proxy_mode,
        }

    thread = threading.Thread(
        target=_run_render_job,
        args=(job_id, targets, segments, root, prev_proxy),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id, "total_frames": total_frames}


def _run_render_job(job_id, targets, segments, root, prev_proxy):
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    done_frames = 0
    success, error_message = True, None

    try:
        for first, last in segments:
            for frame in range(first, last + 1):
                def _do_frame(f=frame):
                    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                        nuke.execute(targets, f, f)
                nuke.executeInMainThreadWithResult(_do_frame)
                done_frames += 1
                with _JOBS_LOCK:
                    _JOBS[job_id]["done"] = done_frames
    except Exception as exc:
        success, error_message = False, str(exc)
    finally:
        if prev_proxy is not None:
            nuke.executeInMainThreadWithResult(lambda: root["proxy"].setValue(prev_proxy))

    with _JOBS_LOCK:
        _JOBS[job_id].update({
            "status": "done" if success else "error",
            "success": success,
            "error": error_message,
            "stdout": stdout_buf.getvalue(),
            "stderr": stderr_buf.getvalue(),
        })


@register_handler("render_status")
def render_status(params):
    """Report progress for a render_start job. Once the job reaches a
    terminal state (done/error), its entry is removed after being reported
    once -- callers should stop polling as soon as they see a terminal status.
    """
    job_id = params["job_id"]
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise LookupError("no such render job: {!r}".format(job_id))
        result = dict(job)
        if job["status"] in ("done", "error"):
            del _JOBS[job_id]
    return result


@register_handler("get_node_screenshot")
def get_node_screenshot(params):
    node_name = params["node_name"]
    node = nuke.toNode(node_name)
    if node is None:
        raise LookupError("no such node: {!r}".format(node_name))

    frame = params.get("frame")
    frame = int(frame) if frame is not None else int(nuke.frame())

    fd, temp_path = tempfile.mkstemp(suffix=".png", prefix="nukemcp_")
    os.close(fd)

    write_node = nuke.createNode("Write", inpanel=False)
    try:
        write_node.setInput(0, node)
        write_node["file"].setValue(temp_path)
        nuke.execute(write_node, frame, frame)
    finally:
        nuke.delete(write_node)

    return {"path": temp_path, "frame": frame}
