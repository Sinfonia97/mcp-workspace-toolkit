# mcp-workspace-toolkit

A from-scratch MCP server, built step by step to learn the protocol
mechanics, and designed to grow into a general-purpose bridge between
Claude and this workspace — project files, configs, and local tools.

Built against **MCP Python SDK v2.x** (the SDK's major rework released
alongside the 2026-07-28 MCP spec revision).

## What's here right now (Step 1)

`server.py` defines two tools:

- `add_numbers(a, b)` — proves a tool call round-trips correctly
- `get_server_status()` — a zero-argument health check

That's it for now. The goal of this step is just: **see these tools show
up in Claude Desktop and successfully call one.**

## Setup (Windows / PowerShell)

```powershell
cd mcp-workspace-toolkit
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

> Notes for this machine:
> - Python is registered as `python`, not `python3` — use `python` for
>   all commands here.
> - Activating the venv uses `venv\Scripts\activate` in PowerShell —
>   not `source venv/bin/activate` (that's the Mac/Linux syntax).
>   You'll know it worked when your prompt shows `(venv)` at the start.
> - `requirements.txt` pins `mcp` to `>=2.0.0,<3.0.0`. v2 changed some
>   import paths from v1 (see below) and swapped its HTTP dependency
>   from `httpx` to `httpx2` — both expected, not a bad install.
> - `mcp dev` also requires **Node.js** (npx) separately from Python —
>   install the LTS build from nodejs.org if `node --version` doesn't
>   resolve. You don't need the optional native-build tools checkbox
>   during install.

## Test it standalone (no Claude Desktop needed yet)

The `mcp` package ships an inspector — a local web UI that lets you call
your tools directly and see the raw protocol messages. This is the
fastest way to check your server works before wiring it into Claude
Desktop:

```powershell
mcp dev server.py
```

This opens a browser UI where you can call `add_numbers` and
`get_server_status` by hand and watch the JSON-RPC traffic. Use this any
time you add a new tool — it's a much faster feedback loop than
restarting Claude Desktop.

> **Use Chrome or Edge, not Firefox.** The Inspector's connection
> (browser → local proxy → your server, over `localhost:6277`) hangs
> indefinitely on "Disconnected"/"Connecting" in Firefox with no error
> in the terminal. Chromium-based browsers work correctly.
>
> If Firefox is your default browser, the Inspector will try to
> auto-launch it. Stop that and open the URL in Chrome/Edge manually:
>
> ```powershell
> $env:MCP_AUTO_OPEN_ENABLED="false"
> mcp dev server.py
> ```
>
> This only changes behavior for that terminal session — it does not
> touch your system default browser. Copy the printed URL (it already
> includes the session token) and paste it into Chrome or Edge.

## Wire it into Claude Desktop

Claude Desktop reads a config file listing the MCP servers it should
launch:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add an entry (create the file if it doesn't exist):

```json
{
  "mcpServers": {
    "workspace-toolkit": {
      "command": "C:\\Projects\\mcp-workspace-toolkit\\venv\\Scripts\\python.exe",
      "args": ["C:\\Projects\\mcp-workspace-toolkit\\server.py"]
    }
  }
}
```

Use **absolute paths** — Claude Desktop launches this as its own
subprocess and won't know about your venv or working directory
otherwise.

Fully quit and reopen Claude Desktop after saving. If it's wired up
correctly, you'll see "workspace-toolkit" listed under the tools/plug
icon in a new conversation, and you can ask Claude something like
"use add_numbers to add 4 and 7" to confirm the round trip.

## Troubleshooting

- **`python3` not found** → on Windows, use `python` instead — `python3`
  isn't a registered command here even though Python itself is installed.
- **`source` not recognized** → that's Mac/Linux syntax. On Windows use
  `venv\Scripts\activate` instead.
- **pip install shows `httpx2` / `mcp-types` as dependencies** → expected
  on v2, not a sign of a bad or malicious package. v2 replaced `httpx` +
  `httpx-sse` with a single `httpx2` dependency and split message
  serialization out into a companion `mcp-types` package.
- **`npx not found`** → Node.js isn't installed. `mcp dev` launches the
  Inspector, which is an npm package, not a pure-Python tool. Install
  Node LTS from nodejs.org, then open a fresh terminal.
- **Inspector stuck on "Disconnected" / "Connecting" forever, no error
  in the terminal** → almost certainly the Firefox issue above. Switch
  to Chrome or Edge.
- **Server doesn't show up at all** → check the config file path and
  JSON syntax (a trailing comma will silently break it). Claude Desktop
  logs are under the same directory as the config file, in a `logs/`
  folder — check there for startup errors.
- **Server shows up but tool calls fail** → run `mcp dev server.py`
  first; it's much easier to debug there than through Claude Desktop.
- **`ModuleNotFoundError: mcp`** → the `command` path in your config
  isn't pointing at the venv's python. Double check the absolute path.

## Note on the v1 → v2 API change

If you ever look at older MCP tutorials or blog posts, they'll likely
import like this (v1):

```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("name")
```

This project uses the v2 equivalent instead:

```python
from mcp.server import MCPServer
mcp = MCPServer("name")
```

Everything else (the `@mcp.tool()` decorator, type hints, docstrings)
works the same way — only the import and class name changed.

## Where this goes next

This scaffold intentionally stops at "one tool works." The planned
build order is:

1. ✅ Hello-world tool (`add_numbers`) — this step
2. Read-only file tool (`read_file`, scoped to one directory)
3. Write tool (`write_file`) — and deciding what confirmation/safety
   behavior the tool description should ask Claude for
4. A domain-specific tool — e.g. reading Home Assistant's entity
   registry or querying the presales tracker's SQLite file
5. Resources (read-only reference data) alongside tools (actions)

Each step is a small, additive change to `server.py` — no need to
restructure what's already working.
