from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# Three safety classes, expressed in MCP's standard annotations. These are
# per-tool, not per-call: a flag may make a tool more dangerous within its
# class, but must never be the only thing making it safe.
# These are per-TOOL, not per-call, which is what decides where a flag ends and
# a separate tool begins: a flag may make a tool more dangerous within its
# class, but it must never be the only thing making it safe. gsv_set_variables
# with dry_run=true is read-only and with dry_run=false is not, so it cannot be
# annotated honestly as read-only -- which is exactly why gsv_diff exists as its
# own tool rather than as that flag.
READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
WRITE_REVERSIBLE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
WRITE_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)

from nukemcp.connection import send_request


def register(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ)
    def gsv_get(scope: str | None = None) -> dict:
        """Read every Graph Scope Variable defined in one scope.

        Returns variables grouped by variable set, exactly as Nuke stores them.
        Every scope has a "__default__" set holding the variables shown directly
        on the group; named sets sit alongside it.

        This reports only what the given scope DEFINES ITSELF. A VariableGroup
        inherits its parent's variables at render time, but Nuke does not report
        inherited values here -- use gsv_list_scopes to see the hierarchy.

        Args:
            scope: omit or pass "root" for the Root node's variables; otherwise
                the name of a VariableGroup node, as reported by
                gsv_list_scopes.
        """
        return send_request("gsv_get", {"scope": scope})

    @mcp.tool(annotations=READ)
    def gsv_list_scopes() -> dict:
        """List every GSV scope in the script: Root plus each VariableGroup.

        Each entry reports the variables that scope declares itself, and its
        parent_scope. Nuke provides no API for an effective/resolved value, so
        working out what a variable evaluates to at some point in the graph
        means walking parent_scope upward from the scope in question.
        """
        return send_request("gsv_list_scopes", {})

    @mcp.tool(annotations=READ)
    def gsv_get_variable(path: str, scope: str | None = None) -> dict:
        """Read one Graph Scope Variable, plus its data type and list options.

        A null value means this scope does not define the variable. That is not
        the same as the variable not existing -- a parent scope may define it.

        Args:
            path: the variable to read, as "<set>.<variable>", e.g.
                "__default__.shot". A bare "shot" means "__default__.shot".
            scope: omit or pass "root" for Root; otherwise a VariableGroup name.
        """
        return send_request("gsv_get_variable", {"path": path, "scope": scope})

    @mcp.tool(annotations=READ)
    def gsv_diff(variables: dict, scope: str | None = None) -> dict:
        """Preview what applying these variables would change. Writes nothing.

        Returns a structured change list (add / modify / remove per path), any
        set-level additions, and the full before/after knob values. Values that
        could not be applied appear under "rejected" with the reason.

        This is the same operation as gsv_set_variables(dry_run=True) and
        returns the same report -- use whichever reads better at the call site.
        Prefer this one when you are only asking a question and have no
        intention of applying the change.

        Args:
            variables: {path: new value} to test, e.g.
                {"__default__.shot": "sh020"}. Bare names default to the
                "__default__" set.
            scope: omit or pass "root" for Root; otherwise a VariableGroup name.
        """
        return send_request("gsv_diff", {"variables": variables, "scope": scope})

    @mcp.tool(annotations=WRITE_REVERSIBLE)
    def gsv_set_variable(
        path: str,
        value: str,
        scope: str | None = None,
        dry_run: bool = False,
        create_missing: bool = True,
    ) -> dict:
        """Set one Graph Scope Variable. Undoable.

        Values must be STRINGS -- Nuke rejects ints, floats, bools and lists
        outright. Convert deliberately (str(1001)) and keep non-string data out
        of GSVs.

        Only the named variable is touched; all other variables and sets in the
        scope are preserved. The write is read back afterwards and reported
        under "vetoed" if it did not land, which happens when a studio
        beforeUser GSV callback returns False to block it.

        CREATES the variable if it does not exist, and lists it under
        "created" -- so a typo'd path invents a variable rather than failing.
        Pass create_missing=false to refuse that, or use gsv_create_variable
        when adding one deliberately (it errors if the name is already taken
        and can declare List options).

        For more than one variable use gsv_set_variables: same behaviour, one
        round trip, one undo step.

        Args:
            path: the variable to set, as "<set>.<variable>", e.g.
                "__default__.shot". A bare "shot" means "__default__.shot".
            value: the new value, as a string.
            scope: omit or pass "root" for Root; otherwise a VariableGroup name.
            dry_run: if true, report the diff without writing anything.
            create_missing: if false, refuse to create a variable that does not
                already exist, instead of silently adding it.
        """
        return send_request(
            "gsv_set_variable",
            {"path": path, "value": value, "scope": scope, "dry_run": dry_run,
             "create_missing": create_missing},
        )

    @mcp.tool(annotations=WRITE_REVERSIBLE)
    def gsv_set_variables(
        variables: dict,
        scope: str | None = None,
        dry_run: bool = False,
        create_missing: bool = True,
    ) -> dict:
        """Set many Graph Scope Variables as ONE undoable operation.

        Prefer this over repeated gsv_set_variable calls -- it is a single round
        trip and collapses into one undo step, which matters when applying a
        whole shot context at once.

        Values must be STRINGS. Named sets are created automatically if a path
        references one that does not exist yet, so gsv_create_set is only needed
        for an EMPTY set. Variables and sets not mentioned are left untouched.

        CREATES any variable that does not exist and lists it under "created",
        so typo'd paths invent variables rather than failing. Pass
        create_missing=false to refuse that -- worth doing when applying a shot
        context, where every name should already be in the template.

        Returns applied paths, created paths, any vetoed writes, per-path
        errors, and a structured before/after diff.

        Args:
            variables: {path: new value}, e.g.
                {"__default__.shot": "sh020", "__default__.comp_version": "v012"}
                Bare names default to the "__default__" set.
            scope: omit or pass "root" for Root; otherwise a VariableGroup name.
            dry_run: if true, report the diff without writing anything.
            create_missing: if false, refuse to create variables that do not
                already exist, instead of silently adding them.
        """
        return send_request(
            "gsv_set_variables",
            {"variables": variables, "scope": scope, "dry_run": dry_run,
             "create_missing": create_missing},
        )

    # -- schema, references, and safe schema mutation ------------------------

    @mcp.tool(annotations=READ)
    def gsv_get_schema() -> dict:
        """The SHAPE of the script's Graph Scope Variables, across all scopes.

        Returns each variable's set, data type, list options (if it is a List),
        which scopes define it, one example value, and its label, tooltip and
        Variables-panel favourite flag where those are set. The schema
        describes structure; values come from whatever drives the render.

        "overridden" lists variables defined in more than one scope, i.e. where
        a VariableGroup shadows a parent.

        CHECK A COMMAND LINE AGAINST THIS BEFORE RENDERING. Nuke's `--var`
        flag is the only way to vary a render without editing the script, and
        it fails silently in four ways, all measured:

            --var quality:high      name not defined in the script
                                    -> IGNORED. Exit 0. Renders the script's
                                       own value, with nothing in the log.
            --var noset.shot:sh010  set does not exist        -> IGNORED
            --var shot:a,b          value containing a comma  -> TRUNCATED to
                                       "a"; the comma separates PAIRS
            --var shot:             empty value               -> IGNORED. It
                                       does not clear the variable.

        So a typo renders a full batch with the wrong values and reports
        success. Nothing downstream can tell the difference afterwards, which
        is why the check has to happen before submission.

        Two further rules the schema is needed for:

          Always pass set-qualified names, "__default__.shot" rather than
          "shot". The bare form works in knob references but --var ignores it
          for anything outside __default__ -- the opposite of what the
          reference syntax suggests.

          For a List variable, check the value is in "list_options". Nuke does
          not enforce List membership; an unlisted value applies silently, and
          a VariableSwitch that matches no pattern falls back to input 0.
        """
        return send_request("gsv_get_schema", {})

    @mcp.tool(annotations=READ)
    def gsv_find_references(path: str | None = None) -> dict:
        """Find every knob that references a GSV, as %{name} or %{set.name}.

        Scans raw knob text across all nodes -- file paths, labels, Text2
        messages, anywhere a string lives. Reads raw values, never evaluated
        ones, because a broken reference evaluates to itself and would be
        invisible.

        Args:
            path: the variable to search for, e.g. "__default__.shot" or
                "shot". Omit to return every GSV reference in the script.
        """
        return send_request("gsv_find_references", {"path": path})

    @mcp.tool(annotations=READ)
    def gsv_validate_references() -> dict:
        """Report GSV references that will not resolve.

        Important: Nuke does NOT error on a dangling reference. "%{gone}"
        evaluates to the literal text "%{gone}", so a broken reference ends up
        embedded in a render path instead of failing loudly. This is the check
        that catches that before a render does.

        Resolution walks each node's VariableGroup parent chain up to Root,
        since Nuke exposes no effective-value API.
        """
        return send_request("gsv_validate_references", {})

    @mcp.tool(annotations=READ)
    def gsv_trace(path: str, node: str | None = None) -> dict:
        """Show where a variable's value comes from at a point in the graph.

        Returns each scope in the node's parent chain with the value that scope
        defines itself, plus which one wins. Use it to answer "why is shot
        sh099 here when Root says sh020".

        Args:
            path: the variable, e.g. "shot" or "__default__.shot".
            node: node to resolve from. Omit to resolve at Root.
        """
        return send_request("gsv_trace", {"path": path, "node": node})

    @mcp.tool(annotations=WRITE_REVERSIBLE)
    def gsv_create_variable(
        path: str,
        value: str = "",
        scope: str | None = None,
        list_options: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Create a new Graph Scope Variable. Undoable.

        Differs from gsv_set_variable, which also creates a missing variable:
        this one ERRORS if the name is already taken, and can declare List
        options. Use it when adding a variable deliberately, so a name
        collision is caught rather than silently overwriting.

        The variable's set is created automatically if needed.

        Args:
            path: e.g. "__default__.shot" or "delivery.quality". A bare name
                means the "__default__" set.
            value: initial value, as a string.
            scope: omit or "root" for Root; otherwise a VariableGroup name.
            list_options: if given, the variable becomes a List type restricted
                to these options in the UI. Note Nuke does NOT enforce the list
                on write -- validate membership yourself.
            dry_run: report what would happen without writing.
        """
        return send_request("gsv_create_variable", {
            "path": path, "value": value, "scope": scope,
            "list_options": list_options, "dry_run": dry_run,
        })

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    def gsv_rename_variable(
        path: str,
        new_name: str,
        scope: str | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict:
        """Rename a Graph Scope Variable. Undoable.

        Nuke does NOT rewrite knobs that reference the old name -- verified.
        Those references silently stop resolving. So this refuses to run when
        references exist, returning them under "references" with blocked=true;
        review them, then pass force=true.

        Args:
            path: the variable to rename.
            new_name: the new bare name, not a path. Moving between sets is not
                supported.
            scope: omit or "root" for Root; otherwise a VariableGroup name.
            dry_run: report what would happen without writing.
            force: proceed even though references would be orphaned.
        """
        return send_request("gsv_rename_variable", {
            "path": path, "new_name": new_name, "scope": scope,
            "dry_run": dry_run, "force": force,
        })

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    def gsv_remove_variable(
        path: str,
        scope: str | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict:
        """Remove a Graph Scope Variable. Undoable.

        Refuses when the variable is referenced, returning the referencing
        knobs with blocked=true. Review them, then pass force=true.

        Removal does not touch referencing knobs -- their text stays "%{name}"
        and quietly stops resolving.

        Args:
            path: the variable to remove.
            scope: omit or "root" for Root; otherwise a VariableGroup name.
            dry_run: report what would happen without writing.
            force: proceed even though references would be orphaned.
        """
        return send_request("gsv_remove_variable", {
            "path": path, "scope": scope, "dry_run": dry_run, "force": force,
        })

    @mcp.tool(annotations=WRITE_REVERSIBLE)
    def gsv_create_set(
        name: str,
        scope: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Create an EMPTY variable set. Undoable.

        Rarely needed: gsv_set_variables and gsv_create_variable both create a
        set automatically when a path references one that does not exist. Use
        this only to declare a set before it has any members.

        Args:
            name: the set name. Must not contain a dot.
            scope: omit or "root" for Root; otherwise a VariableGroup name.
            dry_run: report what would happen without writing.
        """
        return send_request("gsv_create_set", {
            "name": name, "scope": scope, "dry_run": dry_run,
        })

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    def gsv_remove_set(
        name: str,
        scope: str | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict:
        """Remove a variable set and every variable in it. Undoable.

        Refuses when any member variable is referenced, returning the members
        and the referencing knobs with blocked=true. Review, then force=true.

        Args:
            name: the set to remove.
            scope: omit or "root" for Root; otherwise a VariableGroup name.
            dry_run: report what would happen without writing.
            force: proceed even though references would be orphaned.
        """
        return send_request("gsv_remove_set", {
            "name": name, "scope": scope, "dry_run": dry_run, "force": force,
        })

    @mcp.tool(annotations=WRITE_REVERSIBLE)
    def gsv_wrap_in_variable_group(
        nodes: list[str],
        name: str | None = None,
        variables: dict | None = None,
        create_missing: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """Move nodes into a new VariableGroup, giving them their own GSV scope.

        Wrapping a single Write is the interesting case. Each Write then carries
        its own scope, so several Writes can resolve DIFFERENT values for the
        same variable in one script -- a delivery Write on "final" while the
        comp Write stays on "draft" -- instead of everything following one
        Root-level switch.

        Foundry's docs describe VariableGroups passing overrides UP the graph,
        so shared upstream nodes get evaluated at several values at once. This
        is how that gets set up.

        Existing connections are maintained and Input/Output nodes are added
        inside. Your node selection is saved and restored -- the underlying Nuke
        call works on the selection.

        Args:
            nodes: names of nodes to move into the group.
            name: name for the new VariableGroup. Auto-named if omitted.
            variables: {path: value} to set ON THE NEW GROUP, i.e. the overrides
                that make this scope different from its parent.
            create_missing: allow variables not already defined in the group.
                True by default here -- a new scope legitimately introduces its
                own overrides.
            dry_run: report what would be wrapped without changing anything.
        """
        return send_request("gsv_wrap_in_variable_group", {
            "nodes": nodes, "name": name, "variables": variables,
            "create_missing": create_missing, "dry_run": dry_run,
        })
