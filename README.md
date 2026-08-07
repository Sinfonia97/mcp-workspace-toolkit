# mcp-workspace-toolkit

A from-scratch MCP server, built step by step to learn the protocol
mechanics, and grown into a real bridge between Claude and this
workspace — Home Assistant configuration, dashboards, and local files.

Built against **MCP Python SDK v2.x** (the SDK's major rework released
alongside the 2026-07-28 MCP spec revision).

## What this server does

`server.py` exposes 14 tools and 2 resources to any connected MCP
client (Claude Desktop, the Inspector, or any other MCP-compatible
client). All Home Assistant tools are scoped to the config share
defined by `ALLOWED_BASE_DIR`; nothing outside it is reachable.

### General

| Tool | What it does |
|---|---|
| `add_numbers(a, b)` | Adds two integers. The original hello-world tool — kept as a quick sanity check that the server is wired up correctly. |
| `get_server_status()` | Zero-argument health check confirming the server is reachable. |

### Filesystem (scoped to the Home Assistant config folder)

| Tool | What it does |
|---|---|
| `read_file(path)` | Reads a text file's full contents. |
| `list_directory(path="")` | Lists files/subfolders, prefixed `[FILE]`/`[DIR]`. |
| `write_file(path, content, mode="overwrite", dry_run=True)` | Writes or appends to a file. **Defaults to preview-only** — returns a `git diff`-style preview and writes nothing until called again with `dry_run=False`. Automatically backs up the original to `<name>.bak` (once per session) before a real write. |
| `search_entity_references(entity_id, subfolder="", max_results=50)` | Searches YAML files *and* UI-built dashboard cards (parsed from `.storage/lovelace*` JSON) for references to an entity ID. Labels results `[Config]` or `[Dashboard card]` so you can tell a real definition from a dashboard reference at a glance. Also works as a general substring search across YAML files — pass any text, not just an entity ID. |

### Home Assistant — live state & templates (REST API)

| Tool | What it does |
|---|---|
| `get_entity_state(entity_id)` | Current live state + attributes for one entity. |
| `test_ha_template(template)` | Renders a raw Jinja2 template against live data via HA's `/api/template`. Nothing is saved — use this to iterate on template logic before putting it in a file. |
| `reload_ha_templates()` | Validates config first (via HA's check_config), then reloads template entities. Refuses to reload if validation fails. |
| `reload_ha_full_config()` | Same validate-then-reload pattern, but for the entire YAML config tree. Use when the narrower template reload doesn't pick up a change. |
| `get_ha_error_log(lines=40, level="important")` | Reads `home-assistant.log` from the config folder (**requires** the HA CLI option below), filtered to WARNING/ERROR/CRITICAL plus deprecation/breaking-change keywords by default. Pass `level="all"` for the unfiltered log. |

### Home Assistant — dashboards (WebSocket API)

Dashboards come in two modes — check with `list_ha_dashboards` before
assuming which toolset applies:

- **`storage` mode** (UI-built) → use the three tools below.
- **`yaml` mode** (e.g. UI Lovelace Minimalist, Dwains Dashboard) → use
  the filesystem tools above instead (`read_file`/`write_file`/
  `search_entity_references`). `save_ha_dashboard_config` will be
  rejected by Home Assistant for yaml-mode dashboards.

| Tool | What it does |
|---|---|
| `list_ha_dashboards()` | Lists every dashboard with its `url_path` and `mode`. Always check this first — filenames don't reliably tell you the mode. |
| `get_ha_dashboard_config(url_path="")` | Fetches a storage-mode dashboard's live config as JSON. |
| `save_ha_dashboard_config(url_path, config, dry_run=True)` | Saves a storage-mode dashboard. **Defaults to preview-only**, same as `write_file` — returns a diff until called with `dry_run=False`. Applies live (no HA restart) and Home Assistant validates the structure server-side, rejecting malformed config with a clear error. Pre-checks the dashboard's mode and refuses cleanly (pointing you to the file tools) if it's yaml-mode. |

### Resources (reference data, not callable actions)

| Resource | What it holds |
|---|---|
| `ha://entities` | Live list of every entity and its current state. |
| `ha://conventions` | Static, hand-maintained notes on this specific HA setup — entity-ID quirks, dashboard structure (which is storage vs. yaml mode, ULM's file layout, how its sidebar popups wire in), and other context so any client understands this instance without being re-told. **Update this as the setup evolves** — see `ha_conventions_resource()` near the bottom of `server.py`. |

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

### Required `.env` file

Several tools need Home Assistant credentials. Create `.env` in the
project root (already gitignored — never commit this file):

```
HA_URL=http://<your-ha-host>:8123
HA_TOKEN=<a long-lived access token from your HA profile page>
```

### Required Home Assistant-side setup

- **Log file access** (`get_ha_error_log`): Home Assistant disabled
  `home-assistant.log` by default in recent versions, which also broke
  the REST `/api/error_log` endpoint (a known HA bug, not fixable from
  this server). Re-enable the file via SSH/terminal on the HA host:
  ```
  ha core options --duplicate-log-file=true
  ha core rebuild
  ha core restart
  ```
- **Config folder access**: this server reads/writes over a network
  share (`ALLOWED_BASE_DIR` in `server.py`, currently a Samba/SMB path).
  Point it at wherever your HA config folder is reachable from this
  machine.

## Test it standalone (no Claude Desktop needed yet)

The `mcp` package ships an inspector — a local web UI that lets you call
your tools directly and see the raw protocol messages. This is the
fastest way to check your server works before wiring it into Claude
Desktop:

```powershell
mcp dev server.py
```

This opens a browser UI listing every tool above, where you can call
them by hand and watch the JSON-RPC traffic. Use this any time you add
a new tool — it's a much faster feedback loop than restarting Claude
Desktop.

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

Add an entry (create the file if it doesn't exist, or add alongside
any existing `mcpServers` entries):

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

Fully quit and reopen Claude Desktop after saving (or after editing
`server.py` — the running server process needs a restart to pick up
new or changed tools). If it's wired up correctly, you'll see
"workspace-toolkit" listed under the tools/plug icon in a new
conversation.

## Safety design

A few principles apply across every tool that can change something:

- **Preview before commit.** `write_file` and `save_ha_dashboard_config`
  both default to `dry_run=True` — they compute and return a diff
  without touching anything. An explicit `dry_run=False` is required to
  actually apply a change.
- **Validate before reload.** `reload_ha_templates` and
  `reload_ha_full_config` both run Home Assistant's own config check
  first and refuse to reload if it fails.
- **Automatic backups.** `write_file` backs up the pre-edit version of
  any file it's about to overwrite, once per session.
- **Scoped access.** Every filesystem tool is confined to
  `ALLOWED_BASE_DIR`; path traversal attempts (`../..`) are rejected.

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
- **`get_ha_error_log` says the log file isn't found** → run the HA CLI
  commands under "Required Home Assistant-side setup" above.
- **`search_entity_references` times out or is very slow** → should be
  fixed (it prunes known-large folders like `deps`/`backups`/`.storage`
  noise and only reads `.yaml`/`.yml` plus lovelace storage files) — if
  it happens again, check `_SEARCH_SKIP_DIRS` in `server.py` for a new
  large folder that needs adding to the skip list.
- **A HA tool returns "HA_URL or HA_TOKEN is not configured"** → check
  `.env` exists in the project root and `HA_TOKEN` has a real
  long-lived access token, not the placeholder text.
- **`save_ha_dashboard_config` is rejected** → that dashboard is
  probably yaml-mode; check with `list_ha_dashboards` and use
  `write_file` on the actual file instead.
- **Server doesn't show up at all** → check the config file path and
  JSON syntax (a trailing comma will silently break it). Claude Desktop
  logs are under the same directory as the config file, in a `logs/`
  folder — check there for startup errors.
- **Server shows up but tool calls fail, or new tools are missing** →
  run `mcp dev server.py` first to debug; also confirm Claude Desktop
  was restarted after the last `server.py` edit, since it won't pick up
  changes from a running process.
- **`ModuleNotFoundError: mcp`** → the `command` path in your config
  isn't pointing at the venv's python. Double check the absolute path.
- **`ModuleNotFoundError: websocket`** → run
  `pip install -r requirements.txt` again; `websocket-client` was added
  for the dashboard tools.

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

## Build history

Roughly the order things were added, for context on *why* the code
looks the way it does:

1. Hello-world tools (`add_numbers`, `get_server_status`) — confirm the
   protocol round-trip works at all.
2. Read-only filesystem tools (`read_file`, `list_directory`), scoped
   to one folder via `_resolve_safe_path`.
3. `write_file` — the first tool that changes anything, designed with
   dry-run-by-default and automatic backups before touching disk.
4. `get_entity_state` — first domain-specific tool, talking to HA's
   REST API instead of the filesystem.
5. Resources (`ha://entities`, `ha://conventions`) — reference data
   distinct from callable tools.
6. `reload_ha_templates` / `reload_ha_full_config` — validate-then-act,
   mirroring `write_file`'s safety philosophy for HA's own config.
7. `test_ha_template` — closes the template development loop (check
   data → test logic → write file → reload).
8. `get_ha_error_log` — originally REST-based, rewritten to read the
   log file directly after discovering HA's `/api/error_log` endpoint
   is broken (HA 2025.11+); filters to WARNING/ERROR/CRITICAL plus
   breaking-change keywords by default.
9. `search_entity_references` — walks the config tree for entity
   references, including parsing UI dashboard JSON to identify which
   card/view referenced an entity, not just that the text matched.
   Rewritten once to fix a timeout (naive full-tree walk over a network
   share) by pruning known-noise directories before descending into
   them.
10. Dashboard tools (`list_ha_dashboards`, `get_ha_dashboard_config`,
    `save_ha_dashboard_config`) — added after discovering HA has no
    REST API for dashboards at all; built on the WebSocket API instead,
    with the same dry-run-diff safety pattern as `write_file`.

## Where this could go next

Ideas not yet built:

- A tool to call Home Assistant services generally (e.g. turning
  entities on/off), following the same "design the safety behavior
  first" approach used for `write_file` and the dashboard tools.
- Deeper exploration of the Dwains Dashboard yaml structure (only ULM
  has been fully mapped so far).
- Extending beyond Home Assistant to the presales tracker project
  (querying/updating its SQLite data through this server, alongside
  the existing separate `sqlite` MCP server already configured in
  Claude Desktop).
