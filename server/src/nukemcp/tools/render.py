import asyncio
import os

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image

from nukemcp.connection import send_request

_POLL_INTERVAL_SECONDS = 1.5


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def render(
        node_name: str | None = None,
        first_frame: int | None = None,
        last_frame: int | None = None,
        frame_range: str | None = None,
        proxy_mode: bool = False,
        ctx: Context | None = None,
    ) -> dict:
        """Render a frame range. Can legitimately take a long time for large
        ranges or heavy comps -- progress is streamed as each frame completes
        (MCP progress notifications) instead of blocking silently.

        Frame range can be specified in two ways:
          - first_frame + last_frame (e.g. first_frame=1, last_frame=10)
          - frame_range string, which supports compound specs
            (e.g. "1-5,7,9-12" renders frames 1-5, then 7, then 9-12).
            frame_range takes priority if both forms are provided.

        Args:
            node_name: name of a Write node to render, or None to render all
                Write nodes in the script.
            first_frame, last_frame: simple frame range (inclusive).
            frame_range: compound frame range string, e.g. "1-10" or "1-5,7,9-12".
            proxy_mode: if True, enables Nuke proxy mode for this render only
                (useful for fast low-res previews), then restores the previous
                proxy setting.
        """
        params: dict = {"node_name": node_name, "proxy_mode": proxy_mode}
        if frame_range is not None:
            params["frame_range"] = frame_range
        elif first_frame is not None and last_frame is not None:
            params["first_frame"] = first_frame
            params["last_frame"] = last_frame
        else:
            raise ValueError("provide frame_range or both first_frame and last_frame")

        started = send_request("render_start", params)
        job_id = started["job_id"]
        total = started["total_frames"]

        last_reported = -1
        while True:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            status = send_request("render_status", {"job_id": job_id})
            done = status["done"]
            if ctx is not None and done != last_reported:
                await ctx.report_progress(done, total)
                await ctx.info("Rendered {}/{} frames".format(done, total))
                last_reported = done
            if status["status"] in ("done", "error"):
                return {
                    "success": status["success"],
                    "error": status["error"],
                    "stdout": status["stdout"],
                    "stderr": status["stderr"],
                    "segments_rendered": status.get("segments"),
                    "proxy_mode": status.get("proxy_mode"),
                }

    @mcp.tool()
    def get_node_screenshot(node_name: str, frame: int | None = None) -> Image:
        """Render one frame of the given node's output and return it as an image.

        Args:
            node_name: the node's name, e.g. "Blur1".
            frame: which frame to render, defaults to the current frame.
        """
        result = send_request("get_node_screenshot", {"node_name": node_name, "frame": frame})
        temp_path = result["path"]
        try:
            # Read bytes now rather than handing Image(path=...) the path: Image
            # only reads the file lazily, when the SDK serializes the tool result
            # *after* this function returns -- by which point a delete-on-return
            # would have already removed it. Reading eagerly and passing data=
            # sidesteps that timing entirely.
            with open(temp_path, "rb") as f:
                data = f.read()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return Image(data=data, format="png")
