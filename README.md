# mcp-workspace-toolkit

A from-scratch MCP server, built step by step to learn the protocol
mechanics, and grown into a real bridge between Claude and this
workspace: Home Assistant configuration, automations, dashboards,
Node-RED flows, and local files.

Built against MCP Python SDK v2.x (the SDK's major rework released
alongside the 2026-07-28 MCP spec revision).

## What this server does

`server.py` exposes 45 tools and 2 resources to any connected MCP
client (Claude Desktop, the Inspector, or any other MCP-compatible
client). All Home Assistant filesystem tools are scoped to the config
share defined by `ALLOWED_BASE_DIR`, nothing outside it is reachable.

### General

| Tool | What it does |
|---|---|
| `add_numbers(a, b)` | Adds two integers. Original hello-world tool, kept as a sanity check. |
| `get_server_status()` | Health check confirming the server is reachable. |

### Filesystem (scoped to the Home Assistant config folder)

| Tool | What it does |
|---|---|
| `read_file(path)` | Reads a text file's full contents. |
| `list_directory(path="")` | Lists files/subfolders, prefixed `[FILE]`/`[DIR]`. |
| `write_file(path, content, mode="overwrite", dry_run=True)` | Writes or appends to a file. Defaults to preview only, returns a diff and writes nothing until called with `dry_run=False`. Backs up the original to `<name>.bak` once per session before a real write. |
| `search_entity_references(entity_id, subfolder="", max_results=50)` | Searches YAML files and UI-built dashboard cards for references to an entity ID. Labels results `[Config]` or `[Dashboard card]`. Also works as a plain substring search across YAML files. |

### Single-entity YAML editing

For files structured as a top-level list of domain blocks (`- sensor:`,
`- binary_sensor:`), each holding a list of named entities. This is how
`templates/sensor/*.yaml` is organized. Unlike `write_file`, these
operate at entity granularity: find one entity by its `name` field,
edit/add/remove just that entity, leave everything else in the file
untouched.

Uses real YAML parsing (`ruamel.yaml`, configured for HA's custom
`!include`/`!secret` tags) to locate an entity, then splices a
rendered replacement into the original raw text. Never re-serializes
the whole file, so untouched formatting never reflows.

| Tool | What it does |
|---|---|
| `get_yaml_entity(path, entity_name)` | Returns one entity's exact current text, verbatim. |
| `edit_yaml_entity(path, entity_name, entity_yaml, dry_run=True)` | Replaces one entity's complete content. Diffs the whole file for preview, a correct edit only touches that entity's lines. |
| `add_yaml_entity(path, domain, entity_yaml, dry_run=True)` | Adds a new entity as the last item under a domain block, or creates the block if it doesn't exist. |
| `remove_yaml_entity(path, entity_name, dry_run=True)` | Deletes one entity. Previews the exact text that would be removed. |

Same automatic `.bak` backup as `write_file` before a real change.

### Home Assistant, live state and templates (REST API)

| Tool | What it does |
|---|---|
| `get_entity_state(entity_id)` | Current live state and attributes for one entity. |
| `test_ha_template(template)` | Renders a raw Jinja2 template against live data via HA's `/api/template`. Nothing is saved, use this to iterate on template logic before putting it in a file. |
| `reload_ha_templates()` | Validates config first, then reloads template entities. Refuses to reload if validation fails. |
| `reload_ha_full_config()` | Same validate-then-reload pattern, for the entire YAML config tree. Use when the narrower template reload doesn't pick up a change. |
| `get_ha_error_log(lines=40, level="important")` | Reads `home-assistant.log` from the config folder (requires the HA CLI option below), filtered to WARNING/ERROR/CRITICAL plus deprecation keywords by default. Pass `level="all"` for the unfiltered log. |

### Home Assistant, automations and scripts (REST API)

Uses HA's undocumented-but-stable `/api/config/<domain>/config/<id>`
endpoint, the same one HA's own frontend editor uses. One tool handles
both create and update, posting to an id that doesn't exist yet
creates it.

| Tool | What it does |
|---|---|
| `list_ha_automations()` | Lists automations with entity_id, name, on/off state, and config id (from the `id` attribute, not the entity_id). Flags automations with no config id as not editable this way. |
| `get_ha_automation_config(automation_id)` | Full trigger/condition/action structure as JSON. |
| `save_ha_automation_config(automation_id, config, dry_run=True)` | Create or update. Defaults to preview only. Applies live immediately on `dry_run=False`, no reload needed, HA validates server-side. To create new, make up a fresh unique id (a millisecond timestamp works). |
| `remove_ha_automation(automation_id, dry_run=True)` | Deletes by config id. No automatic backup, save the config with `get_ha_automation_config` first if you might want it back. |
| `list_ha_scripts()` / `get_ha_script_config(script_id)` / `save_ha_script_config(script_id, config, dry_run=True)` / `remove_ha_script(script_id, dry_run=True)` | Same shape as the automation tools, for scripts. A script's config id is the part of its entity_id after `script.` (e.g. `movie_mode`). |

### Home Assistant, automation/script traces (WebSocket API)

Answers a different question than `get_ha_error_log`: not "did
something go wrong somewhere" but "exactly what happened during this
one run", trigger data, each condition's pass/fail, each action's
result, with any error.

| Tool | What it does |
|---|---|
| `get_ha_automation_traces(automation_id, limit=5)` | Most recent runs first, with outcome. Deliberately slices from the end of HA's chronological (oldest-first) list, a real bug found in another Home Assistant MCP server during research. |
| `get_ha_automation_trace_detail(automation_id, run_id)` | Full step-by-step JSON for one run. |
| `get_ha_script_traces(script_id, limit=5)` / `get_ha_script_trace_detail(script_id, run_id)` | Same shape, for scripts. |

### Home Assistant, dashboards (WebSocket API)

Dashboards come in two modes. Check with `list_ha_dashboards` before
assuming which toolset applies.

- storage mode (UI-built): use the three tools below.
- yaml mode (e.g. UI Lovelace Minimalist, Dwains Dashboard): use the
  filesystem tools instead (`read_file`/`write_file`/
  `search_entity_references`). `save_ha_dashboard_config` will be
  rejected by Home Assistant for yaml-mode dashboards.

| Tool | What it does |
|---|---|
| `list_ha_dashboards()` | Lists every dashboard with its `url_path` and `mode`. Always check this first, filenames don't reliably tell you the mode. |
| `get_ha_dashboard_config(url_path="")` | Fetches a storage-mode dashboard's live config as JSON. |
| `save_ha_dashboard_config(url_path, config, dry_run=True)` | Saves a storage-mode dashboard. Defaults to preview only. Applies live (no HA restart), HA validates the structure server-side. Pre-checks the dashboard's mode and refuses cleanly if it's yaml-mode. |

### Node-RED

A completely separate service from Home Assistant, its own port, its
own auth. Uses Node-RED's Admin HTTP API, authenticated with plain
HTTP Basic Auth (see "Node-RED auth" below for why).

Flows (each flow is one tab in the editor):

| Tool | What it does |
|---|---|
| `list_node_red_flows()` | Every flow's name and id. Node-RED has no built-in name lookup, only by id, so use this first. |
| `get_node_red_flows()` / `save_node_red_flows(flows_json, dry_run=True)` | The entire flow set at once. Save replaces everything, bigger blast radius. Prefer the single-flow tools below when editing just one tab. |
| `get_node_red_flow(tab_id)` / `save_node_red_flow(tab_id, flow_json, dry_run=True)` | One tab only, via Node-RED's singular `/flow/:id` endpoint. A PUT here stops and restarts only that tab's nodes, every other flow keeps running. Prefer these over the plural versions. |
| `create_node_red_flow(label, nodes_json, dry_run=True)` | Creates a brand-new tab. Node-RED always assigns its own id, any id you pass is discarded. |
| `remove_node_red_flow(tab_id, dry_run=True)` | Deletes a tab. No automatic backup, use `get_node_red_flow` first if you want to keep a copy. |
| `search_node_red_flows(text, max_results=50)` | Substring search across every node's JSON in every flow: entity IDs, device names, function-node code, anything. Built to find every place a renamed device broke a flow silently. |

Installed node modules (the palette: inject, switch, api-call-service,
etc.):

| Tool | What it does |
|---|---|
| `list_node_red_nodes()` | Every installed node "set" with type name, owning module, version, enabled state. |
| `get_node_red_node_info(module)` | Full raw detail for one module. |
| `install_node_red_node(module, dry_run=True)` | Runs a real `npm install` on the Node-RED host. Can take a while, can fail for reasons outside this server's control. |
| `set_node_red_node_enabled(module, enabled, dry_run=True)` | Enable/disable a module without uninstalling it. |
| `remove_node_red_node(module, dry_run=True)` | Full uninstall. |

Diagnostics (sanity-check tools, not features, they confirm the
assumptions everything above depends on):

| Tool | What it does |
|---|---|
| `get_node_red_diagnostics()` | Node-RED's own health/version report. |
| `get_node_red_settings()` | Version, whether Projects mode is enabled (changes how flows persist, everything above assumes it's off), API base path. |

### Resources (reference data, not callable actions)

| Resource | What it holds |
|---|---|
| `ha://entities` | Live list of every entity and its current state. |
| `ha://conventions` | Static notes on this HA setup: entity-ID quirks, dashboard structure (storage vs yaml mode per dashboard, ULM's file layout and how its sidebar popups wire in). Update this as the setup evolves, see `ha_conventions_resource()` near the bottom of `server.py`. |

## Setup (Windows / PowerShell)

```powershell
cd mcp-workspace-toolkit
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Notes for this machine:
- Python is registered as `python`, not `python3`.
- Activating the venv uses `venv\Scripts\activate` in PowerShell, not
  `source venv/bin/activate` (that's Mac/Linux syntax). You'll know it
  worked when your prompt shows `(venv)` at the start.
- `requirements.txt` pins `mcp` to `>=2.0.0,<3.0.0`. v2 changed some
  import paths from v1 (see below) and swapped its HTTP dependency
  from `httpx` to `httpx2`, both expected.
- `mcp dev` also requires Node.js (npx) separately from Python, install
  the LTS build from nodejs.org if `node --version` doesn't resolve.
  You don't need the optional native-build tools checkbox during
  install.

### Required `.env` file

Several tools need credentials. Create `.env` in the project root
(already gitignored, never commit this file):

```
HA_URL=http://<your-ha-host>:8123
HA_TOKEN=<a long-lived access token from your HA profile page>

NODE_RED_URL=http://<your-node-red-host>:1880
NODE_RED_USERNAME=<your Node-RED editor username>
NODE_RED_PASSWORD=<your Node-RED editor password>
```

### Required Home Assistant-side setup

Log file access (`get_ha_error_log`): Home Assistant disabled
`home-assistant.log` by default in recent versions, which also broke
the REST `/api/error_log` endpoint (a known HA bug). Re-enable the
file via SSH/terminal on the HA host:

```
ha core options --duplicate-log-file=true
ha core rebuild
ha core restart
```

Config folder access: this server reads/writes over a network share
(`ALLOWED_BASE_DIR` in `server.py`, currently a Samba/SMB path). Point
it at wherever your HA config folder is reachable from this machine.

### Node-RED auth, why Basic Auth specifically

The Home Assistant Community Add-on: Node-RED wraps its editor in
something that looks tied to Home Assistant's login (no separate
account was ever set up), but two other approaches were tried first
and both failed with 401: Node-RED's own `/auth/token` username/password
flow, and reusing `HA_TOKEN` as a Bearer header. Capturing the actual
browser login in dev tools showed no POST/fetch request at all,
consistent with the browser's native HTTP Basic Auth prompt, handled
at the protocol level before any page JavaScript runs. That's why it
never shows up as a network request. `NODE_RED_USERNAME`/
`NODE_RED_PASSWORD` are sent as standard HTTP Basic Auth on every
request, no separate login step needed.

## Test it standalone (no Claude Desktop needed yet)

The `mcp` package ships an inspector, a local web UI that lets you call
your tools directly and see the raw protocol messages. This is the
fastest way to check your server works before wiring it into Claude
Desktop:

```powershell
mcp dev server.py
```

This opens a browser UI listing every tool above, where you can call
them by hand and watch the JSON-RPC traffic. Use this any time you add
a new tool, it's a much faster feedback loop than restarting Claude
Desktop.

Use Chrome or Edge, not Firefox. The Inspector's connection (browser to
local proxy to your server, over `localhost:6277`) hangs indefinitely
on "Disconnected"/"Connecting" in Firefox with no error in the
terminal. Chromium-based browsers work correctly.

If Firefox is your default browser, the Inspector will try to
auto-launch it. Stop that and open the URL in Chrome/Edge manually:

```powershell
$env:MCP_AUTO_OPEN_ENABLED="false"
mcp dev server.py
```

This only changes behavior for that terminal session, it does not
touch your system default browser. Copy the printed URL (it already
includes the session token) and paste it into Chrome or Edge.

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

Use absolute paths, Claude Desktop launches this as its own subprocess
and won't know about your venv or working directory otherwise.

Fully quit and reopen Claude Desktop after saving (or after editing
`server.py`, the running server process needs a restart to pick up new
or changed tools). If it's wired up correctly, you'll see
"workspace-toolkit" listed under the tools/plug icon in a new
conversation.

## Safety design

- Preview before commit. `write_file`, `edit_yaml_entity`,
  `add_yaml_entity`, `save_ha_dashboard_config`,
  `save_ha_automation_config`, `save_ha_script_config`,
  `save_node_red_flows`/`save_node_red_flow`, `create_node_red_flow`,
  and every install/enable/remove tool default to `dry_run=True`. An
  explicit `dry_run=False` is required to actually apply a change.
- Validate before reload. `reload_ha_templates` and
  `reload_ha_full_config` run Home Assistant's own config check first
  and refuse to reload if it fails. Automation/script/dashboard saves
  are validated server-side by Home Assistant itself on write.
- Automatic backups, but only for file writes. `write_file`,
  `edit_yaml_entity`, `add_yaml_entity`, and `remove_yaml_entity` back
  up the pre-edit file to `<name>.bak` once per session before
  changing it. Deletions that go through an API instead of a file
  (`remove_ha_automation`, `remove_ha_script`, `remove_node_red_flow`,
  `remove_node_red_node`) have no automatic backup. The dry-run preview
  is the only safety net for those, treat it as mandatory.
- Scoped access. Every filesystem tool is confined to
  `ALLOWED_BASE_DIR`, path traversal attempts (`../..`) are rejected.
- Entity-level edits never touch the whole file. `edit_yaml_entity`/
  `add_yaml_entity`/`remove_yaml_entity` locate one entity via real YAML
  parsing, then splice a change into the original raw text. Every
  other entity, comment, and domain block stays untouched.

## Troubleshooting

- `python3` not found: on Windows, use `python` instead.
- `source` not recognized: that's Mac/Linux syntax, use
  `venv\Scripts\activate`.
- pip install shows `httpx2` / `mcp-types` as dependencies: expected on
  v2, not a bad package. v2 replaced `httpx` + `httpx-sse` with a
  single `httpx2` dependency and split message serialization into a
  companion `mcp-types` package.
- `npx not found`: Node.js isn't installed. `mcp dev` launches the
  Inspector, an npm package. Install Node LTS from nodejs.org, then
  open a fresh terminal.
- Inspector stuck on "Disconnected"/"Connecting" forever, no error in
  the terminal: almost certainly the Firefox issue above, switch to
  Chrome or Edge.
- `get_ha_error_log` says the log file isn't found: run the HA CLI
  commands under "Required Home Assistant-side setup" above.
- `search_entity_references` times out or is very slow: should be
  fixed (it prunes known-large folders and only reads `.yaml`/`.yml`
  plus lovelace storage files). If it happens again, check
  `_SEARCH_SKIP_DIRS` in `server.py` for a new large folder that needs
  adding to the skip list.
- A HA tool returns "HA_URL or HA_TOKEN is not configured": check
  `.env` exists in the project root and `HA_TOKEN` has a real
  long-lived access token, not the placeholder text.
- `save_ha_dashboard_config` is rejected: that dashboard is probably
  yaml-mode, check with `list_ha_dashboards` and use `write_file` on
  the actual file instead.
- A Node-RED tool returns 401: confirm `NODE_RED_USERNAME`/
  `NODE_RED_PASSWORD` in `.env` are your real Node-RED editor login.
  This add-on uses plain HTTP Basic Auth, see "Node-RED auth" above.
- `list_node_red_nodes` or similar returns "Tool result is too large":
  this was a real, two-layer bug (both now fixed): `GET /nodes`
  defaults to returning raw HTML unless the request sends
  `Accept: application/json` (now sent on every Node-RED request), and
  the response shape is a flat array of individual node "sets", not
  modules grouping a nested list. If a similar error resurfaces on a
  different Node-RED endpoint, check both of these first.
- Server doesn't show up at all: check the config file path and JSON
  syntax (a trailing comma will silently break it). Claude Desktop logs
  are under the same directory as the config file, in a `logs/` folder.
- Server shows up but tool calls fail, or new tools are missing: run
  `mcp dev server.py` first to debug, also confirm Claude Desktop was
  restarted after the last `server.py` edit.
- `ModuleNotFoundError: mcp`: the `command` path in your config isn't
  pointing at the venv's python.
- `ModuleNotFoundError: websocket`: run `pip install -r requirements.txt`
  again, `websocket-client` was added for the dashboard tools.
- `ModuleNotFoundError: ruamel`: same fix, `ruamel.yaml` was added for
  the single-entity YAML editing tools.

## Note on the v1 to v2 API change

Older MCP tutorials or blog posts will likely import like this (v1):

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
works the same way, only the import and class name changed.

## Build history

Roughly the order things were added:

1. Hello-world tools (`add_numbers`, `get_server_status`), confirm the
   protocol round-trip works at all.
2. Read-only filesystem tools (`read_file`, `list_directory`), scoped
   to one folder via `_resolve_safe_path`.
3. `write_file`, the first tool that changes anything, dry-run-by-default
   and automatic backups before touching disk.
4. `get_entity_state`, first domain-specific tool, talking to HA's
   REST API instead of the filesystem.
5. Resources (`ha://entities`, `ha://conventions`), reference data
   distinct from callable tools.
6. `reload_ha_templates` / `reload_ha_full_config`, validate-then-act.
7. `test_ha_template`, closes the template development loop.
8. `get_ha_error_log`, originally REST-based, rewritten to read the log
   file directly after discovering HA's `/api/error_log` endpoint is
   broken (HA 2025.11+).
9. `search_entity_references`, walks the config tree for entity
   references, including parsing dashboard JSON. Rewritten once to fix
   a timeout (naive full-tree walk over a network share) by pruning
   known-noise directories.
10. Dashboard tools, added after discovering HA has no REST API for
    dashboards at all, built on the WebSocket API instead.
11. Automation and script tools, using HA's undocumented-but-stable
    `/api/config/<domain>/config/<id>` endpoint. Full round trip tested
    before trusting it on anything real, used shortly after to fix a
    silently-failing automation (a removed third-party integration,
    rebuilt with native `input_text` helpers).
12. Automation/script trace tools, built after realizing config
    validation can't catch runtime failures. Fixed a real ordering bug
    found in another Home Assistant MCP server during research (traces
    come back oldest-first, a naive slice grabs the wrong end).
13. Node-RED integration, real debugging before it worked: two wrong
    auth theories before landing on plain HTTP Basic Auth. Then a
    genuine two-layer bug in `list_node_red_nodes` (missing Accept
    header causing HTML instead of JSON, then a data-shape mismatch
    once JSON was flowing), both root-caused and fixed.
14. Node-RED single-flow tools, added after realizing the whole-flow-set
    tools carry unnecessary risk for a one-tab change.
15. Node-RED node-module tools and the `list_node_red_flows` name lookup
    (Node-RED has no built-in lookup by name, only by id).
16. Single-entity YAML editing tools, the most involved addition yet.
    Prototyped in a separate sandbox before touching any real file,
    found and fixed three genuine bugs: a wrong `ruamel.yaml`
    `TaggedScalar` constructor call that silently corrupted HA's
    custom tags, a boundary-detection approach that silently deleted
    domain-block headers between entities, and a doubled blank line
    from ruamel's own comment-preservation metadata. Validated
    end-to-end against a real, complex template file before being
    trusted.

## Where this could go next

- A generic "call any Home Assistant service" tool. Came up around
  triggering add-on backups (`hassio.backup_partial` / `backup.create`),
  but the exact service name for this HA version was never confirmed.
- Backup automation, letting a Node-RED-only (or any add-on) backup be
  triggered conversationally, once the above exists. Right now, partial
  backups are done manually via Settings, System, Backups, Create
  Backup, Partial backup, select the add-on.
- Bulk entity control (act on many entities matching a pattern at once).
- History/statistics, an entity's value over time (`/api/history/period`),
  not just its current state.
- Camera snapshots.
- Broader helpers/areas/zones/labels management, beyond the ad hoc
  `input_text` helpers added for the motion-tracking automation fix.
- Dashboard resources (registered JS/CSS) as distinct from dashboard
  content, which is all the current tools handle.
- A more formalized safety/permissions system beyond simple `dry_run`
  defaults (a real policy DSL, a Read-Only Mode toggle).
- Deeper exploration of the Dwains Dashboard yaml structure (not
  actually used day-to-day, only ULM has been fully mapped so far).
- Extending beyond Home Assistant/Node-RED to the presales tracker
  project (querying/updating its SQLite data through this server,
  alongside the existing separate `sqlite` MCP server already
  configured in Claude Desktop).
