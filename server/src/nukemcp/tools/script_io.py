import os

from mcp.server.fastmcp import FastMCP

from nukemcp.connection import send_request


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def open_script(path: str, dry_run: bool = False) -> dict:
        """Open a .nk script file, replacing the current Nuke session.

        This is destructive: any unsaved changes in the current session are
        lost, and there is no merge option. Use dry_run=True first to preview
        what would happen -- whether the target file exists and whether the
        current session has unsaved changes that would be discarded -- without
        actually opening anything.

        Args:
            path: absolute path to the .nk file.
            dry_run: if True, don't open the file -- just report a preview.
        """
        if not os.path.isabs(path):
            raise ValueError("path must be absolute: {!r}".format(path))
        return send_request("open_script", {"path": path, "dry_run": dry_run})

    @mcp.tool()
    def save_script(path: str) -> dict:
        """Save the current Nuke session to an explicit path. Silently
        overwrites an existing file at that path -- the result reports
        whether an existing file was overwritten.

        Args:
            path: absolute path to save the .nk file to.
        """
        if not os.path.isabs(path):
            raise ValueError("path must be absolute: {!r}".format(path))
        return send_request("save_script", {"path": path})

    @mcp.tool()
    def merge_script(path: str) -> dict:
        """Import nodes from another .nk file into the current script without
        replacing it. The imported nodes are added to the existing graph.
        Undoable.

        Args:
            path: absolute path to the .nk file to import.
        """
        if not os.path.isabs(path):
            raise ValueError("path must be absolute: {!r}".format(path))
        return send_request("merge_script", {"path": path})
