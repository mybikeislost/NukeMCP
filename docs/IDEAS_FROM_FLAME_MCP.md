# Ideas evaluated from abrahamadsk/flame-mcp — for a future NukeMCP update

Written 2026-07-28, while building FlameMCP
(`/Volumes/Vault/Projects/Dev/Flame/FlameMCP`) with
[abrahamadsk/flame-mcp](https://github.com/abrahamadsk/flame-mcp) as a
reference repo for comparison. That project is a third-party MCP server
for Flame, architecturally much heavier than NukeMCP because Flame's
Python API is far more crash-prone and far less documented than Nuke's —
most of its complexity exists to compensate for problems NukeMCP doesn't
actually have. A few pieces translate anyway. Recorded here so they're
not lost between sessions.

## Worth adding

- **`install.sh --doctor` health-check sweep.** flame-mcp runs a 5-check
  PASS/FAIL/WARN/SKIP sweep (MCP registration, addon symlink, venv
  importability, etc.) with remediation hints. Cheap, general-purpose,
  no reason NukeMCP shouldn't have the equivalent — addon on `NUKE_PATH`,
  `claude mcp` registration, venv importable, `.mcpb` build present.

- **AST-parse tool-permission sync at server startup.** flame-mcp's
  `server.py` parses its own source at startup to register non-destructive
  tool approvals into `~/.claude/settings.json` automatically. Same DX
  win would apply to NukeMCP's `server/src/nukemcp/tools/*.py` — walk the
  `@mcp.tool()` decorators, pre-approve the read-only ones.

- **Progress heartbeat for long-blocking tools.** flame-mcp streams an
  MCP `ctx.info` heartbeat every 10s for its handful of long-running
  tools instead of staying silent until done. NukeMCP's
  `render` is documented in `docs/TOOLS.md` as "Blocks until done" with
  no interim feedback — a direct, low-risk UX improvement.

- **Runtime API introspection → AST pre-flight for `execute_nuke_code`.**
  flame-mcp walks the live API module at install time, emits a symbol
  graph, and statically rejects hallucinated attribute paths before the
  socket round-trip — instant "did you mean X?" instead of a wasted round
  trip and a traceback. Nuke's API is much better documented than
  Flame's, but it's still huge, and this would still turn a class of
  wasted round-trips into instant, actionable feedback. Medium priority —
  worth it once `execute_nuke_code` usage data shows this class of
  failure happening often enough to justify the build.

- **`dry_run` mode on destructive tools.** flame-mcp's `execute_python`
  supports `dry_run=True` to preview safety/routing checks without
  executing. NukeMCP's `open_script` in particular has no merge option —
  it silently replaces the whole session. A dry-run preview (or at least
  a confirmation echo of what will be replaced) is a cheap safety net
  independent of anything Flame-specific.

## Deliberately not adding (solved differently, or not needed)

- **Pattern-based undo-code synthesis** (`journal.py` in flame-mcp).
  Moot for NukeMCP — every mutating handler already wraps in a real
  `nuke.Undo()` group (see `nuke_addon/nukemcp_server/nuke_compat.py`).
  Regex-guessing undo code from a text description is strictly worse
  than an actual undo stack.

- **Known-crasher regex/AST blocklist** (`safety.py` in flame-mcp, ~15
  patterns). That list exists almost entirely to compensate for calling
  Flame's API from an unmarshaled background thread — see
  `FlameMCP/docs/ARCHITECTURE.md` for the full writeup. NukeMCP's
  `nuke.executeInMainThreadWithResult` usage in
  `nuke_addon/nukemcp_server/listener.py` already prevents that whole
  class of bug. Not zero value as a general safety net, but low priority
  until real recurring crash patterns show up in practice.

- **Structured JSON op registry (`execute_plan`).** flame-mcp built this
  because its LLM otherwise writes raw Python that can hallucinate
  symbols. NukeMCP already gets the same benefit natively — every
  operation is already a separate, typed `@mcp.tool()` function
  (`create_node`, `set_knob_values`, etc.) rather than one big
  code-execution tool. No need to retrofit an indirection layer NukeMCP's
  design already avoids.

- **Crash-recovery snapshot before every exec.** Lower value here —
  Nuke crashes far less often given proper main-thread marshaling, so
  the "no trace after a crash" problem this solves is much smaller.

- **The embedded Qt chat widget + multi-backend LLM picker.** Scope
  creep regardless of target app — it reimplements a chat client inside
  the DCC, which is orthogonal to being an MCP server. Claude Code/
  Desktop already is the chat client.

- **The full RAG stack** (ChromaDB + sentence-transformers hybrid
  BM25/semantic search over API docs). Heavy dependency chain that
  exists because Flame's official docs are thin. Nuke's docs are good
  enough that NukeMCP hasn't needed this, and there's no evidence it
  would.
