"""
mcp-workspace-toolkit
----------------------
A minimal MCP (Model Context Protocol) server, built from scratch to learn
how the pieces fit together, and to grow into a general-purpose bridge
between Claude and this workspace (project files, configs, local tools).

Built against MCP Python SDK v2.x (new spec release, mid-2026). Note the
import path below is different from v1 — v2 moved the quick-start server
class from mcp.server.fastmcp.FastMCP to mcp.server.MCPServer.

Step 1 goal: register one tool, run the server over stdio, and see it show
up (and get called) inside Claude Desktop. DONE — add_numbers and
get_server_status both confirmed working via the MCP Inspector.

Step 2 goal: add a read-only file tool, scoped to one folder, and learn
the pattern for safely exposing filesystem access to an MCP client.

Run it directly to sanity-check startup:
    python server.py

It won't do anything visible when run directly — MCP servers are meant to
be launched BY a client (like Claude Desktop) that speaks the protocol over
stdin/stdout. See README.md for how to wire it up.
"""

import json
import os
from difflib import unified_diff
from pathlib import Path

import requests
from dotenv import load_dotenv
from mcp.server import MCPServer

# Load HA_URL and HA_TOKEN from the .env file in this folder into the
# process environment. .env is gitignored — this is what keeps the token
# out of the code and out of GitHub, while still letting the server read
# it at startup via os.environ.
load_dotenv()

# The name here is what shows up in Claude Desktop's tool/server list.
mcp = MCPServer("workspace-toolkit")


# -----------------------------------------------------------------------
# ALLOWED_BASE_DIR
#
# Any tool that touches the filesystem is scoped to ONLY this folder (and
# its subfolders). This is a deliberate safety boundary: an MCP client
# should never be able to ask this server to read or write files outside
# of what you've explicitly decided to expose — no matter how the request
# is phrased.
#
# Currently pointed at the Home Assistant config share. Change this one
# line if you want to point the server somewhere else later.
# -----------------------------------------------------------------------
ALLOWED_BASE_DIR = Path(r"\\192.168.2.90\config").resolve()


def _resolve_safe_path(user_path: str) -> Path:
    """Resolve a user-supplied path and verify it stays inside ALLOWED_BASE_DIR.

    This is the core safety check reused by every filesystem tool in this
    server. It works in two steps:
      1. Join the requested path onto ALLOWED_BASE_DIR and resolve it to
         its full, canonical form (this collapses things like "..").
      2. Confirm the resolved path is still located inside ALLOWED_BASE_DIR.

    Raises ValueError if the path would escape the allowed folder — e.g.
    a request like "..\\..\\Windows\\System32\\config" gets blocked here,
    even though step 1 would technically resolve it to a real path.
    """
    candidate = (ALLOWED_BASE_DIR / user_path).resolve()

    if not candidate.is_relative_to(ALLOWED_BASE_DIR):
        raise ValueError(
            f"Access denied: '{user_path}' resolves outside the allowed "
            f"directory ({ALLOWED_BASE_DIR}). This tool can only read "
            f"files within that folder."
        )

    return candidate


# -----------------------------------------------------------------------
# HA_URL / HA_TOKEN
#
# Read from the environment (populated by load_dotenv() above, from the
# .env file). These are NOT hardcoded in this file on purpose — unlike
# ALLOWED_BASE_DIR, a token is a secret, and secrets never belong in code
# that might end up in git history, even a private repo.
#
# If either is missing (e.g. .env wasn't filled in), get_entity_state
# below returns a clear error instead of crashing the whole server.
# -----------------------------------------------------------------------
HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# -----------------------------------------------------------------------
# NODE_RED_URL / NODE_RED_USERNAME / NODE_RED_PASSWORD
#
# Node-RED is a COMPLETELY SEPARATE service from Home Assistant — its own
# process, own port (default 1880), own auth system. It's not reachable
# through anything above; it needs its own client logic entirely.
#
# Also unlike HA_TOKEN (a permanent long-lived token), Node-RED's admin
# API uses short-lived access tokens obtained by logging in with a
# username/password each time — there's no long-lived token to store.
# So instead of a token, the .env file holds credentials, and
# _node_red_login() below fetches a fresh token whenever a tool needs one.
# -----------------------------------------------------------------------
NODE_RED_URL = os.environ.get("NODE_RED_URL", "").rstrip("/")
NODE_RED_USERNAME = os.environ.get("NODE_RED_USERNAME", "")
NODE_RED_PASSWORD = os.environ.get("NODE_RED_PASSWORD", "")


# -----------------------------------------------------------------------
# _ha_ws_call
#
# Home Assistant's REST API (used by every tool so far) has NO way to
# read or write dashboard/lovelace configuration — that's a real,
# long-standing gap in HA itself, not something missing from this
# server. Dashboards are only accessible through HA's WEBSOCKET API,
# which is a fundamentally different protocol: a persistent connection
# with an auth handshake, rather than a single authenticated HTTP
# request like `requests.get(...)`.
#
# This helper opens a FRESH connection per call (connect → authenticate
# → send one command → receive the result → close) rather than keeping
# a connection open between tool calls. That's less efficient than a
# persistent connection, but far simpler and safer for how MCP tools
# work — each tool call is independent and stateless, so a connection
# that outlives a single call would be extra complexity for no real
# benefit here (dashboard edits are occasional, not high-frequency).
#
# Returns (success, data): on success, data is HA's "result" payload
# (already a Python object, not a JSON string). On failure, data is a
# human-readable error message string.
# -----------------------------------------------------------------------
def _ha_ws_call(message: dict, timeout: int = 15):
    if not HA_URL or not HA_TOKEN:
        return False, "HA_URL or HA_TOKEN is not configured in .env."

    try:
        import websocket
    except ImportError:
        return False, "The websocket-client package is not installed. Run: pip install -r requirements.txt"

    # HA's WebSocket endpoint lives at the same host as the REST API,
    # just with ws:// (or wss:// for https) instead of http(s)://, and
    # a fixed /api/websocket path.
    ws_url = HA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
    except Exception as e:
        return False, f"Could not connect to Home Assistant WebSocket at {ws_url}: {e}"

    try:
        # Step 1: HA sends "auth_required" as soon as we connect.
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            return False, f"Unexpected WebSocket handshake: {hello}"

        # Step 2: authenticate with the same long-lived token used
        # everywhere else in this server.
        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        auth_result = json.loads(ws.recv())
        if auth_result.get("type") != "auth_ok":
            return False, f"WebSocket authentication failed: {auth_result.get('message', auth_result)}"

        # Step 3: send the actual command. HA requires a unique "id" on
        # every command; since each call gets a fresh connection, id=1
        # is always valid here (it only needs to be unique WITHIN one
        # connection, not across calls).
        message = dict(message, id=1)
        ws.send(json.dumps(message))
        result = json.loads(ws.recv())

        if not result.get("success", False):
            error = result.get("error", {})
            return False, f"Home Assistant rejected the command: {error.get('message', error)}"

        return True, result.get("result")
    except Exception as e:
        return False, f"WebSocket communication error: {e}"
    finally:
        ws.close()


# -----------------------------------------------------------------------
# TOOL 1: add_numbers
#
# This is the "hello world" of MCP tools. A few things to notice:
#   - The function name becomes the tool name Claude calls.
#   - Type hints (int, int -> int) are used by the SDK to build the JSON
#     schema Claude sees describing what arguments this tool takes.
#   - The docstring is NOT just documentation for humans — it's what
#     Claude reads to decide WHEN to call this tool and how to use it.
#     Treat it like a mini prompt.
# -----------------------------------------------------------------------
@mcp.tool()
def add_numbers(a: int, b: int) -> int:
    """Add two integers together and return the sum.

    Use this to demonstrate that the MCP server is wired up correctly
    and that Claude can successfully call a tool and get a result back.
    """
    return a + b


# -----------------------------------------------------------------------
# TOOL 2: get_server_status
#
# A zero-argument tool. Useful pattern for health checks, or any tool
# where you just want to expose some read-only state.
# -----------------------------------------------------------------------
@mcp.tool()
def get_server_status() -> str:
    """Report that the workspace toolkit MCP server is running and reachable.

    Use this as a quick check that Claude can talk to this server at all.
    """
    return "workspace-toolkit is up and responding."


# -----------------------------------------------------------------------
# TOOL 3: read_file
#
# The first tool that actually touches the filesystem. Notice the shape:
#   1. Take the raw path the client sent.
#   2. Run it through _resolve_safe_path() BEFORE touching disk at all —
#      never read/write with a path you haven't validated first.
#   3. If validation raises, let that error surface back to the client
#      as a normal string return rather than letting the whole server
#      crash on a bad request. Claude will see the message and can
#      explain it to you, or try a different path.
#   4. Only after validation succeeds do we actually open the file.
#
# This function/docstring pair is also a template — Step 3's write_file
# tool, and any future filesystem tool, will follow this exact same
# validate-then-act shape.
# -----------------------------------------------------------------------
@mcp.tool()
def read_file(path: str) -> str:
    """Read and return the full text contents of a file.

    The path is relative to the server's allowed base directory (the
    Home Assistant config folder). For example, to read the main
    templates file, pass "templates.yaml" — not a full Windows or UNC
    path. Subfolders are fine, e.g. "packages/my_package.yaml".

    Only files within the allowed base directory can be read. Requests
    for anything outside it (including via "../" tricks) are rejected.
    Use this to inspect configuration or template files before proposing
    changes to them.
    """
    try:
        safe_path = _resolve_safe_path(path)
    except ValueError as e:
        return str(e)

    if not safe_path.exists():
        return f"Error: '{path}' does not exist in the allowed directory."

    if not safe_path.is_file():
        return f"Error: '{path}' is a directory, not a file."

    try:
        return safe_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return (
            f"Error: '{path}' could not be read as UTF-8 text. "
            f"It may be a binary file."
        )


# -----------------------------------------------------------------------
# TOOL 4: list_directory
#
# A companion to read_file. Before you can read the right file, you often
# need to see what's actually there — this returns the contents of a
# folder (files and subfolders) rather than a file's text.
#
# Same validate-then-act shape as read_file: resolve and check the path
# first, then act on it. Returns [FILE] / [DIR] prefixes so it's easy to
# tell at a glance what each entry is, similar to how a normal directory
# listing tool works.
# -----------------------------------------------------------------------
@mcp.tool()
def list_directory(path: str = "") -> str:
    """List the files and subfolders inside a directory.

    The path is relative to the server's allowed base directory (the
    Home Assistant config folder). Pass an empty string (or omit the
    argument) to list the root of that folder. Pass a subfolder name,
    e.g. "templates", to list inside it.

    Each entry is prefixed with [FILE] or [DIR] so you can tell files
    and subfolders apart. Use this before read_file to find the exact
    filename you're looking for.
    """
    try:
        safe_path = _resolve_safe_path(path)
    except ValueError as e:
        return str(e)

    if not safe_path.exists():
        return f"Error: '{path}' does not exist in the allowed directory."

    if not safe_path.is_dir():
        return f"Error: '{path}' is a file, not a directory."

    entries = sorted(safe_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))

    if not entries:
        return f"'{path or '.'}' is empty."

    lines = []
    for entry in entries:
        tag = "[FILE]" if entry.is_file() else "[DIR]"
        lines.append(f"{tag} {entry.name}")

    return "\n".join(lines)


# -----------------------------------------------------------------------
# TOOL 5: write_file
#
# The first tool that can CHANGE something on disk, not just read it. The
# design here reflects four decisions made deliberately before writing
# any code, rather than defaults picked for convenience:
#
#   1. Supports both "overwrite" (replace the whole file) and "append"
#      (add to the end) via a mode parameter, so the caller has to be
#      explicit about which behavior they want — there's no ambiguous
#      default that could surprise you.
#   2. Automatically backs up the ORIGINAL file to "<name>.bak" before
#      any write to a file that already exists. This only happens once
#      per file — a second edit to the same file will NOT overwrite an
#      existing .bak, so the .bak always holds the earliest known-good
#      version from this session, not just the most recent edit.
#   3. Allowed to create brand-new files (no backup needed, since there's
#      nothing to preserve).
#   4. Defaults to PREVIEW-ONLY (dry_run=True). Calling this tool without
#      explicitly setting dry_run=False computes what the file WOULD look
#      like after the change, diffs it against the current content using
#      Python's difflib, and returns that diff as text — without writing
#      anything to disk. Only an explicit dry_run=False actually commits
#      the change. This mirrors the same philosophy as `mode` above: the
#      safe behavior is what happens if you don't say otherwise.
#
# Same validate-then-act shape as read_file/list_directory: resolve and
# check the path first, before touching disk.
# -----------------------------------------------------------------------
@mcp.tool()
def write_file(path: str, content: str, mode: str = "overwrite", dry_run: bool = True) -> str:
    """Write text content to a file, creating it if it doesn't exist.

    The path is relative to the server's allowed base directory (the
    Home Assistant config folder), e.g. "templates/sensor/my_sensor.yaml".

    mode controls how the write behaves:
      - "overwrite" (default): replace the file's entire contents with
        `content`. Use this for full file edits.
      - "append": add `content` to the end of the existing file, leaving
        what's already there untouched. Use this to add a new entry to
        a file without retyping the whole thing.

    dry_run controls whether this actually writes anything:
      - True (default): DO NOT write to disk. Instead, compute what the
        file would look like after the change and return a unified diff
        (the same +/- format as `git diff`) showing exactly what would
        change. Always call with dry_run=True first, show the user the
        resulting diff, and only call again with dry_run=False after
        they've confirmed they want the change applied.
      - False: actually perform the write, including the automatic
        backup step described below.

    Before overwriting or appending to a file that ALREADY EXISTS (and
    only when dry_run=False), this tool automatically saves a backup
    copy alongside it as "<name>.bak" (only if a .bak doesn't already
    exist for that file, so the backup always reflects the earliest
    version from this session). Creating a brand-new file skips the
    backup step, since there's nothing to back up yet.

    Only files within the allowed base directory can be written. Requests
    for anything outside it (including via "../" tricks) are rejected.
    Always read the current contents with read_file first if you need to
    know what's there before deciding what to write.
    """
    if mode not in ("overwrite", "append"):
        return f"Error: mode must be 'overwrite' or 'append', got '{mode}'."

    try:
        safe_path = _resolve_safe_path(path)
    except ValueError as e:
        return str(e)

    if safe_path.exists() and not safe_path.is_file():
        return f"Error: '{path}' is a directory, not a file."

    # Figure out the "before" and "after" text so we can diff or write it.
    if safe_path.exists():
        try:
            original_text = safe_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return (
                f"Error: '{path}' could not be read as UTF-8 text, so it "
                f"can't be safely diffed or edited by this tool."
            )
    else:
        original_text = ""

    new_text = original_text + content if mode == "append" else content

    if dry_run:
        diff_lines = list(
            unified_diff(
                original_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"{path} (current)",
                tofile=f"{path} (proposed)",
            )
        )
        if not diff_lines:
            return f"No changes: the proposed content for '{path}' is identical to what's already there."
        header = f"PREVIEW ONLY — nothing written yet. Diff for '{path}':\n\n"
        return header + "".join(diff_lines)

    # dry_run is False from here on — actually write to disk.
    backup_note = ""
    if safe_path.exists():
        backup_path = safe_path.with_suffix(safe_path.suffix + ".bak")
        if not backup_path.exists():
            backup_path.write_bytes(safe_path.read_bytes())
            backup_note = f" (original backed up to '{backup_path.name}')"

    try:
        safe_path.write_text(new_text, encoding="utf-8")
        action = "Appended to" if mode == "append" else "Wrote"
    except OSError as e:
        return f"Error writing '{path}': {e}"

    return f"{action} '{path}'.{backup_note}"


# -----------------------------------------------------------------------
# TOOL 6: get_entity_state
#
# The first DOMAIN-SPECIFIC tool — it doesn't know about files at all.
# Instead it talks to Home Assistant's own REST API to ask "what is this
# entity's value RIGHT NOW", which is different from what read_file on a
# template YAML gives you (the LOGIC that computes the value, not the
# live result).
#
# Key differences from the filesystem tools above:
#   - No _resolve_safe_path() involved — there's no path here, just an
#     entity_id string, and Home Assistant's own API decides what's
#     valid, not this server.
#   - Requires authentication (the Bearer token), unlike the Samba share
#     which just relied on your existing Windows session.
#   - Talks over HTTP to a live service that might be down, slow, or
#     unreachable — so this needs its own error handling for network
#     problems, separate from file-not-found style errors.
# -----------------------------------------------------------------------
@mcp.tool()
def get_entity_state(entity_id: str) -> str:
    """Get the current live state and attributes of a Home Assistant entity.

    entity_id is the full Home Assistant entity ID, e.g.
    "switch.washer_power_2" or "sensor.dryer_current_status".

    Returns the entity's current state value (e.g. "running", "on",
    "off") plus its attributes, as reported live by Home Assistant right
    now. This is different from read_file on a template YAML file, which
    shows the LOGIC that computes a value, not the current live result.
    Use this when you need to know what's happening right now; use
    read_file/list_directory when you need to see or edit configuration.
    """
    if not HA_URL or not HA_TOKEN:
        return (
            "Error: HA_URL or HA_TOKEN is not configured. Check that "
            ".env exists in the project folder and HA_TOKEN has been "
            "filled in with a real long-lived access token."
        )

    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.exceptions.ConnectionError:
        return (
            f"Error: could not reach Home Assistant at {HA_URL}. "
            f"Confirm it's running and the address is correct."
        )
    except requests.exceptions.Timeout:
        return f"Error: request to Home Assistant timed out after 10 seconds."

    if response.status_code == 401:
        return "Error: authentication failed (401). The token in .env may be invalid or expired."
    if response.status_code == 404:
        return f"Error: entity '{entity_id}' was not found in Home Assistant."
    if response.status_code != 200:
        return f"Error: Home Assistant returned status {response.status_code}."

    data = response.json()
    state = data.get("state", "unknown")
    attributes = data.get("attributes", {})

    lines = [f"{entity_id}: {state}"]
    if attributes:
        lines.append("Attributes:")
        for key, value in attributes.items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)


# -----------------------------------------------------------------------
# _check_ha_config
#
# Shared helper used by BOTH reload tools below. Calls Home Assistant's
# config validation endpoint, which checks your YAML for errors WITHOUT
# applying anything — same idea as write_file's dry_run, just on HA's
# own config instead of a single file. Returns (is_valid, message) so
# each calling tool can decide what to do with the result.
# -----------------------------------------------------------------------
def _check_ha_config() -> tuple[bool, str]:
    if not HA_URL or not HA_TOKEN:
        return False, "Error: HA_URL or HA_TOKEN is not configured in .env."

    url = f"{HA_URL}/api/config/core/check_config"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, timeout=30)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return False, f"Error: could not reach Home Assistant at {HA_URL}."

    if response.status_code != 200:
        return False, f"Error: config check request failed with status {response.status_code}."

    data = response.json()
    if data.get("result") == "valid":
        return True, "Config check passed: no errors found."

    errors = data.get("errors") or "(no error detail returned)"
    return False, f"Config check FAILED. Errors:\n{errors}"


# -----------------------------------------------------------------------
# TOOL 7: reload_ha_templates
#
# Narrower reload — only re-reads template entities, not the whole
# config. Faster, but you've found it sometimes doesn't pick up every
# change, which is why reload_ha_full_config (below) also exists as a
# fallback.
#
# Both reload tools share the SAME shape: check config first via
# _check_ha_config(), and only call the actual reload service if that
# check passed. This mirrors write_file's "never act on an unvalidated
# change" philosophy — just applied to Home Assistant's live config
# instead of a single file on disk.
# -----------------------------------------------------------------------
@mcp.tool()
def reload_ha_templates() -> str:
    """Check Home Assistant's config for errors, then reload template entities.

    Always validates the current YAML config first (the same check Home
    Assistant's UI runs before a reload). If the check finds errors, this
    tool stops and returns those errors WITHOUT reloading anything —
    reloading invalid config could break running entities. Only if the
    check passes does it call the template.reload service.

    This is narrower/faster than reload_ha_full_config, but only affects
    template-defined entities. If a change doesn't seem to take effect
    after this, try reload_ha_full_config instead.
    """
    is_valid, message = _check_ha_config()
    if not is_valid:
        return message

    url = f"{HA_URL}/api/services/template/reload"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, timeout=30)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return f"Error: config check passed, but could not reach Home Assistant to reload."

    if response.status_code != 200:
        return f"Error: config check passed, but reload request failed with status {response.status_code}."

    return f"{message}\nTemplates reloaded successfully."


# -----------------------------------------------------------------------
# TOOL 8: reload_ha_full_config
#
# Broader reload — homeassistant.reload_core_config re-reads the entire
# YAML configuration tree, affecting more integrations than just
# templates. Use when a narrower reload (like reload_ha_templates) isn't
# enough to pick up a change. Same check-then-act shape as above.
# -----------------------------------------------------------------------
@mcp.tool()
def reload_ha_full_config() -> str:
    """Check Home Assistant's config for errors, then reload the FULL configuration.

    Always validates the current YAML config first, exactly like
    reload_ha_templates. If the check finds errors, this tool stops and
    returns those errors WITHOUT reloading anything.

    This is broader than reload_ha_templates — it calls
    homeassistant.reload_core_config, which re-reads the entire YAML
    config tree rather than just template entities. Use this when a
    narrower reload doesn't seem to pick up a change.
    """
    is_valid, message = _check_ha_config()
    if not is_valid:
        return message

    url = f"{HA_URL}/api/services/homeassistant/reload_core_config"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, timeout=30)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return f"Error: config check passed, but could not reach Home Assistant to reload."

    if response.status_code != 200:
        return f"Error: config check passed, but reload request failed with status {response.status_code}."

    return f"{message}\nFull configuration reloaded successfully."


# -----------------------------------------------------------------------
# TOOL 9: test_ha_template
#
# The tool most directly aimed at your actual workflow: iterating on
# Jinja2 template logic BEFORE it goes anywhere near a real file. Home
# Assistant has a dedicated /api/template endpoint that renders a raw
# template string against LIVE entity states and returns the result —
# nothing is saved, nothing is deployed. It's the templating equivalent
# of write_file's dry_run: try it, see the result, iterate, and only
# once it's right does the finished template go into a real file via
# write_file.
#
# This closes the loop on template development:
#   1. get_entity_state / ha://entities — see what data is available
#   2. test_ha_template — iterate on the Jinja2 logic against that data
#   3. write_file (dry_run, then committed) — save the finished template
#   4. reload_ha_templates — apply it live
# -----------------------------------------------------------------------
@mcp.tool()
def test_ha_template(template: str) -> str:
    """Render a Jinja2 template string against live Home Assistant data.

    Pass a raw Jinja2 template, e.g.
    "{{ states('sensor.washer_current_status') }}" or something more
    complex using states, is_state, or any Jinja2 logic Home Assistant
    templates support. It's evaluated against LIVE current entity states
    and the rendered result is returned as plain text.

    Nothing is saved or deployed — this is purely for testing template
    logic before putting it into a real file with write_file. Use this
    to iterate on tricky template logic (e.g. mapping a raw status value
    to a friendly label) and confirm it produces the expected output
    before committing it to templates/sensor/*.yaml.
    """
    if not HA_URL or not HA_TOKEN:
        return "Error: HA_URL or HA_TOKEN is not configured in .env."

    url = f"{HA_URL}/api/template"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, json={"template": template}, timeout=10)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return f"Error: could not reach Home Assistant at {HA_URL}."

    if response.status_code == 400:
        # HA returns 400 with the Jinja2 error message in the body text
        # when the template itself has a syntax error — surface that
        # directly, since it's exactly the debugging info you'd want.
        return f"Template error:\n{response.text}"
    if response.status_code == 401:
        return "Error: authentication failed (401). The token in .env may be invalid or expired."
    if response.status_code != 200:
        return f"Error: Home Assistant returned status {response.status_code}."

    return f"Rendered result:\n{response.text}"


# -----------------------------------------------------------------------
# _BREAKING_CHANGE_KEYWORDS
#
# Home Assistant usually logs deprecation and breaking-change notices at
# WARNING level, so a severity filter alone catches most of them. But
# not always — some advisory messages don't follow that convention. This
# keyword list is a safety net: lines containing any of these phrases are
# ALWAYS included, regardless of severity, so a plain-INFO notice about
# an upcoming breaking change doesn't get silently filtered out.
# -----------------------------------------------------------------------
_BREAKING_CHANGE_KEYWORDS = (
    "deprecat",
    "will be removed",
    "will stop working",
    "breaking change",
    "no longer",
    "migrat",
)


# -----------------------------------------------------------------------
# TOOL 10: get_ha_error_log
#
# Fills the gap the other HA tools leave open: check_config catches YAML
# SYNTAX errors before a reload, but it can't catch RUNTIME problems —
# e.g. a template that's valid Jinja2 but references an entity that
# doesn't exist, an integration that failed to connect, or a warning
# that never surfaces anywhere in the HA UI. Those only show up here, in
# the actual log.
#
# NOTE: this originally called Home Assistant's /api/error_log REST
# endpoint, but that endpoint returns 404 as of HA 2025.11+ — HA removed
# home-assistant.log by default and the REST API was never updated to
# match (a known, widely-reported HA bug, not something wrong with this
# server). The fix: HA has a CLI option (`ha core options
# --duplicate-log-file=true`) that restores the log FILE even though the
# API stays broken. So this tool now reads that file directly off the
# Samba share instead of calling HTTP — same _resolve_safe_path pattern
# as read_file/list_directory, not the requests+Bearer-token pattern the
# other HA tools use. If HA ever fixes the REST endpoint, this could be
# switched back; for now the filesystem route is simpler AND more
# reliable, since it's the exact same proven pattern as read_file.
#
# Filtering logic is unchanged: WARNING/ERROR/CRITICAL lines plus any
# line matching _BREAKING_CHANGE_KEYWORDS, regardless of severity level.
# Pass level="all" to see the full unfiltered file.
# -----------------------------------------------------------------------
@mcp.tool()
def get_ha_error_log(lines: int = 40, level: str = "important") -> str:
    """Get recent lines from Home Assistant's log file, filtered to what matters.

    Reads home-assistant.log directly from the config folder (requires
    the HA CLI option "duplicate-log-file" to be enabled, since HA
    disabled this log file by default in recent versions).

    lines controls how many matching lines to return (default 40, taken
    from the end of the filtered log — the most recent matches).

    level controls filtering:
      - "important" (default): only WARNING/ERROR/CRITICAL lines, PLUS
        any line mentioning a deprecation or breaking change (even at
        INFO level) so upcoming issues aren't missed.
      - "all": no filtering — return the raw log as-is.

    This tool only returns log data; it doesn't analyze it. After calling
    this, review the returned lines and propose fixes for anything that
    looks like a real problem — that's a reasoning step, not something
    this tool does on its own.
    """
    if level not in ("important", "all"):
        return f"Error: level must be 'important' or 'all', got '{level}'."

    try:
        safe_path = _resolve_safe_path("home-assistant.log")
    except ValueError as e:
        return str(e)

    if not safe_path.exists():
        return (
            "Error: home-assistant.log not found in the config folder. "
            "This file requires the HA CLI option to be enabled: run "
            "'ha core options --duplicate-log-file=true', then "
            "'ha core rebuild' and 'ha core restart'."
        )

    try:
        log_text = safe_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error reading home-assistant.log: {e}"

    all_lines = log_text.splitlines()
    if not all_lines:
        return "Log is empty."

    if level == "all":
        matched = all_lines
    else:
        matched = [
            line for line in all_lines
            if " WARNING " in line
            or " ERROR " in line
            or " CRITICAL " in line
            or any(keyword in line.lower() for keyword in _BREAKING_CHANGE_KEYWORDS)
        ]
        if not matched:
            return (
                "No WARNING/ERROR/CRITICAL or breaking-change lines found "
                "in the current log. Pass level=\"all\" to see the full "
                "unfiltered log instead."
            )

    recent = matched[-lines:]
    label = "matching" if level == "important" else "raw"
    return f"Last {len(recent)} {label} line(s) of home-assistant.log:\n\n" + "\n".join(recent)


# -----------------------------------------------------------------------
# _SEARCH_SKIP_DIRS / _SEARCH_EXTENSIONS
#
# search_entity_references originally walked EVERY file under the config
# folder with Path.rglob("*"), which timed out in practice — a real HA
# config folder contains large, irrelevant subtrees (.storage internal
# state, deps, tts cache, backups, custom_components caches) that were
# all being read over the network as text before being discarded. Two
# fixes, applied together:
#   1. Skip these known-noise directories ENTIRELY, before descending
#      into them — not after. This requires os.walk (which allows
#      pruning dirnames in place) instead of rglob (which doesn't).
#   2. Only read files with an extension that could plausibly contain a
#      readable entity_id reference — YAML by default. This alone
#      eliminates images, databases, and binary caches without needing
#      to open them and fail on decode.
# -----------------------------------------------------------------------
_SEARCH_SKIP_DIRS = {
    ".cloud", ".git", "__pycache__",
    "deps", "tts", "backups", "media", "www",
}
_SEARCH_EXTENSIONS = {".yaml", ".yml"}

# .storage is normally noise (auth, registries, cloud, energy, traces —
# often large, never hand-editable) so it's deliberately NOT in
# _SEARCH_SKIP_DIRS. Dashboards built through the HA UI (rather than
# written as YAML) are stored here as extension-less JSON files named
# "lovelace" or "lovelace.<dashboard_id>" — exactly the kind of place an
# entity_id can be referenced (in a card config) without ever appearing
# in a .yaml file at all. So instead of skipping .storage wholesale, only
# files starting with "lovelace" inside it are read; everything else in
# that folder is still skipped for speed.
_STORAGE_DIR_NAME = ".storage"
_STORAGE_FILE_PREFIX = "lovelace"


def _search_lovelace_json(node, entity_id: str, view_title=None, card=None, key_path="", results=None):
    """Recursively walk a parsed lovelace dashboard JSON structure for entity_id.

    Unlike the plain-text line search used for YAML files, this actually
    understands the dashboard's shape well enough to say WHAT kind of
    thing referenced the entity_id, not just that the string appeared
    somewhere in a JSON blob.

    Tracks three pieces of "where am I" context while descending:
      - view_title: updates whenever we enter a dict that has a "cards"
        key (that's what makes a dict a "view" in lovelace's schema).
      - card: updates whenever we enter a dict that has a "type" key
        (that's what makes a dict a "card" — including nested cards
        inside layout cards like vertical-stack, which have their own
        "type" and their own nested "cards" list).
      - key_path: the dotted/bracketed JSON path to the CURRENT node,
        e.g. "views[0].cards[3].entity" — this is what lets two matches
        inside the same card (e.g. "entity" and
        "tap_action.target.entity_id") show up as distinguishable
        results instead of two identical-looking lines.

    When entity_id is found inside a string value, the result includes
    the exact key_path AND a compact JSON dump of the enclosing card (or
    the whole node, if no card) so you can visually recognize it by its
    icon/name/other properties even when it has no title at all.
    """
    if results is None:
        results = []

    if isinstance(node, dict):
        next_view_title = node.get("title") if "cards" in node else view_title
        next_card = node if "type" in node else card
        for key, value in node.items():
            next_path = f"{key_path}.{key}" if key_path else key
            _search_lovelace_json(value, entity_id, next_view_title, next_card, next_path, results)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            next_path = f"{key_path}[{index}]"
            _search_lovelace_json(item, entity_id, view_title, card, next_path, results)
    elif isinstance(node, str):
        if entity_id in node:
            if card is not None:
                card_type = card.get("type", "unknown")
                card_title = card.get("title") or card.get("name")
                card_desc = f"{card_type} card" + (f" (title: '{card_title}')" if card_title else "")
                # Compact JSON snippet of the whole card so it's visually
                # identifiable even with no title — e.g. by its icon.
                # Truncated so one giant card doesn't blow up the output.
                snippet = json.dumps(card, ensure_ascii=False)
                if len(snippet) > 200:
                    snippet = snippet[:200] + "..."
            else:
                card_desc = "dashboard config (outside any card)"
                snippet = None
            view_desc = f"View '{view_title}' \u2192 " if view_title else ""
            snippet_desc = f" | card contents: {snippet}" if snippet else ""
            results.append(f"{view_desc}{card_desc} | matched field: {key_path}{snippet_desc}")

    return results


# -----------------------------------------------------------------------
# TOOL 11: search_entity_references
#
# A different kind of filesystem tool from read_file/list_directory: this
# one searches ACROSS every file in the config tree at once, rather than
# operating on one file or folder you already know the name of. Useful
# for a very concrete real-world need: before renaming or retiring an
# entity_id, find every YAML file that references it, so you don't miss
# one and leave a dangling reference behind.
#
# Uses os.walk (not Path.rglob) specifically so _SEARCH_SKIP_DIRS can be
# pruned BEFORE descending into them, which is what actually fixes the
# timeout — rglob has no equivalent pruning mechanism.
#
# Capped at a maximum number of results (documented in the docstring
# below) so a very common entity_id doesn't return an unreadable wall of
# text.
# -----------------------------------------------------------------------
@mcp.tool()
def search_entity_references(entity_id: str, subfolder: str = "", max_results: int = 50) -> str:
    """Search config files and dashboards for lines/cards referencing an entity_id.

    entity_id is the text to search for, e.g. "switch.washer_power_2".

    Two kinds of sources are searched, and results are labeled so you can
    tell them apart at a glance:
      - "[Config] path:line: <text>" — a plain-text match in a .yaml/.yml
        file (entity definitions, automations, templates, etc.)
      - "[Dashboard card] path: View 'X' -> <type> card (title: 'Y')" —
        a match found by actually parsing a UI-built dashboard's JSON
        structure (stored in .storage/lovelace*), identifying which view
        and which card (by type and title) referenced the entity.

    Known noise folders (deps, tts, backups, media, www, etc.) are
    skipped for speed, since they can't meaningfully contain a
    hand-written or UI-configured entity reference anyway.

    subfolder optionally limits the search to one part of the config
    tree, e.g. "templates", instead of searching everything. Leave empty
    to search the whole config folder.

    max_results caps how many matches are returned (default 50). If the
    entity_id is very common, results may be truncated — the output will
    say so explicitly if that happens.

    Use this before renaming or retiring an entity_id, to find every
    place that references it — including dashboard cards that would
    otherwise silently break — so nothing gets missed.
    """
    try:
        search_root = _resolve_safe_path(subfolder)
    except ValueError as e:
        return str(e)

    if not search_root.exists():
        return f"Error: '{subfolder}' does not exist in the allowed directory."

    if not search_root.is_dir():
        return f"Error: '{subfolder}' is a file, not a directory."

    matches = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(search_root):
        # Prune skip-listed and hidden directories BEFORE os.walk
        # descends into them — this is what avoids the timeout, since
        # it never even lists what's inside e.g. deps or backups.
        # .storage is a special case: it starts with "." (which the
        # general hidden-dir filter would otherwise block) but it's
        # explicitly allowed through, since that's where lovelace
        # dashboard files live (see _STORAGE_DIR_NAME above).
        dirnames[:] = [
            d for d in dirnames
            if d not in _SEARCH_SKIP_DIRS
            and (d == _STORAGE_DIR_NAME or not d.startswith("."))
        ]

        for filename in sorted(filenames):
            file_path = Path(dirpath) / filename
            is_yaml = file_path.suffix.lower() in _SEARCH_EXTENSIONS
            is_lovelace_storage = (
                Path(dirpath).name == _STORAGE_DIR_NAME
                and filename.startswith(_STORAGE_FILE_PREFIX)
            )
            if not (is_yaml or is_lovelace_storage):
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # skip unreadable files silently

            rel_path = file_path.relative_to(ALLOWED_BASE_DIR)

            if is_lovelace_storage:
                # Try real JSON parsing so results say WHAT kind of thing
                # referenced the entity (a card, and what type/title),
                # not just that the string appeared somewhere. Falls
                # back to a generic label if the file isn't valid JSON
                # (e.g. a future HA version changes the format) rather
                # than silently skipping a real match.
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    if entity_id in text:
                        matches.append(f"[Dashboard] {rel_path}: (could not parse as JSON; entity_id found in raw file)")
                        if len(matches) >= max_results:
                            truncated = True
                            break
                    continue

                for description in _search_lovelace_json(data, entity_id):
                    matches.append(f"[Dashboard card] {rel_path}: {description}")
                    if len(matches) >= max_results:
                        truncated = True
                        break
                if truncated:
                    break
                continue

            # Plain YAML: line-based search, labeled [Config] so it's
            # visually distinct from [Dashboard card] results — this is
            # the actual answer to "how do I know this is a real entity
            # definition/reference vs. just a dashboard card": the label
            # itself, driven by which branch of this code found it.
            for line_number, line in enumerate(text.splitlines(), start=1):
                if entity_id in line:
                    matches.append(f"[Config] {rel_path}:{line_number}: {line.strip()}")
                    if len(matches) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    if not matches:
        return f"No references to '{entity_id}' found under '{subfolder or '.'}'."

    header = f"Found {len(matches)} reference(s) to '{entity_id}':\n\n"
    footer = (
        f"\n\n(Results capped at {max_results}; there may be more matches "
        f"not shown.)" if truncated else ""
    )
    return header + "\n".join(matches) + footer


# -----------------------------------------------------------------------
# TOOL 12: list_ha_dashboards
#
# Discovers real dashboard url_path values through the API, rather than
# guessing from .storage filenames. The two don't always match exactly
# (e.g. underscores in the filename vs. dashes in the actual url_path),
# so this is the reliable way to find the right identifier to pass to
# get_ha_dashboard_config / save_ha_dashboard_config below.
# -----------------------------------------------------------------------
@mcp.tool()
def list_ha_dashboards() -> str:
    """List all Home Assistant dashboards, their url_path, and edit mode.

    Returns each dashboard's title, url_path, and mode ("storage" or
    "yaml"). This distinction matters:
      - mode "storage": UI-managed dashboards. Use get_ha_dashboard_config
        and save_ha_dashboard_config (this server's WebSocket-based
        tools) to view/edit them.
      - mode "yaml": file-managed dashboards. save_ha_dashboard_config
        will be REJECTED by Home Assistant for these — use read_file,
        write_file, and search_entity_references on the underlying YAML
        file instead (check configuration.yaml's "lovelace:" section for
        the exact filename).

    Use the url_path value (not a guessed filename) with
    get_ha_dashboard_config and save_ha_dashboard_config.

    Note: this lists dashboards registered as separate entries. The
    default/main dashboard (if you haven't created others) may not
    appear here — pass an empty url_path to the other tools to target it.
    """
    success, data = _ha_ws_call({"type": "lovelace/dashboards/list"})
    if not success:
        return f"Error: {data}"

    if not data:
        return "No additional dashboards found (only the default dashboard exists)."

    lines = []
    for dashboard in data:
        title = dashboard.get("title", "(untitled)")
        url_path = dashboard.get("url_path", "(unknown)")
        mode = dashboard.get("mode", "unknown")
        lines.append(f"{title}  (url_path: {url_path}, mode: {mode})")
    return "\n".join(lines)


# -----------------------------------------------------------------------
# TOOL 13: get_ha_dashboard_config
#
# The API-based equivalent of read_file, but for dashboards specifically
# — fetches the LIVE config Home Assistant is actually serving right
# now, through the proper WebSocket command, rather than reading the
# .storage file directly (which risks reading a stale copy if HA hasn't
# flushed its in-memory state to disk yet).
# -----------------------------------------------------------------------
@mcp.tool()
def get_ha_dashboard_config(url_path: str = "") -> str:
    """Get a Home Assistant dashboard's current configuration as JSON.

    url_path identifies which dashboard (from list_ha_dashboards). Leave
    empty for the default/main dashboard.

    Returns the full dashboard config (views, cards, everything) as
    pretty-printed JSON. Use this to see the current structure before
    proposing changes with save_ha_dashboard_config.
    """
    command = {"type": "lovelace/config"}
    if url_path:
        command["url_path"] = url_path

    success, data = _ha_ws_call(command)
    if not success:
        return f"Error: {data}"

    return json.dumps(data, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------
# TOOL 14: save_ha_dashboard_config
#
# This is the tool that actually replaces the "just edit the JSON file
# directly" approach you were (rightly) uncomfortable with. Same
# dry_run-by-default philosophy as write_file, but two real advantages
# over a raw file write:
#   1. LIVE APPLY — HA's lovelace/config/save command updates the
#      running dashboard immediately. No restart needed, unlike editing
#      .storage/lovelace* directly.
#   2. SERVER-SIDE VALIDATION — if the config you pass is structurally
#      invalid, HA's own save command rejects it with an error message
#      BEFORE anything changes, the same safety net check_config gives
#      YAML edits. A raw file write has no equivalent check at all.
#
# dry_run=True (the default) fetches the CURRENT config, diffs it
# against your proposed config using the same unified_diff pattern as
# write_file, and returns that diff WITHOUT saving anything. Only
# dry_run=False actually calls the save command.
# -----------------------------------------------------------------------
@mcp.tool()
def save_ha_dashboard_config(url_path: str, config: str, dry_run: bool = True) -> str:
    """Save a Home Assistant dashboard's configuration.

    ONLY works for "storage"-mode dashboards (check with
    list_ha_dashboards first). "yaml"-mode dashboards are managed as
    plain files and will be rejected here — use read_file, write_file,
    and search_entity_references on the underlying YAML file instead.

    url_path identifies which dashboard (from list_ha_dashboards). Use
    an empty string for the default/main dashboard.

    config is the COMPLETE dashboard configuration as a JSON string
    (views, cards, everything) — this replaces the whole dashboard, the
    same way write_file's "overwrite" mode replaces a whole file. Always
    fetch the current config with get_ha_dashboard_config first, modify
    it, and pass the complete result back here — don't try to pass a
    partial config.

    dry_run controls whether this actually saves anything:
      - True (default): fetch the current config, compute a diff against
        your proposed config, and return that diff WITHOUT saving.
        Always call with dry_run=True first and confirm the diff looks
        right before saving for real.
      - False: actually save. Applies LIVE immediately (no HA restart
        needed) through Home Assistant's own save command, which
        validates the structure and rejects it with a clear error if
        something is malformed — this is real validation, not just a
        raw file write.
    """
    try:
        proposed = json.loads(config)
    except json.JSONDecodeError as e:
        return f"Error: config is not valid JSON: {e}"

    # Preemptively check the dashboard's mode before doing anything
    # else. Home Assistant's own save command WOULD reject a yaml-mode
    # dashboard anyway, but its raw error message is generic — this
    # gives a clear, actionable answer up front instead: use the file
    # tools, not this one, for that dashboard.
    success, dashboards = _ha_ws_call({"type": "lovelace/dashboards/list"})
    if success:
        for dashboard in dashboards:
            if dashboard.get("url_path") == (url_path or None) and dashboard.get("mode") == "yaml":
                return (
                    f"Error: dashboard '{url_path or '(default)'}' is in YAML mode, "
                    f"not storage mode. Home Assistant does not allow saving YAML-mode "
                    f"dashboards through the API — they're managed as plain files instead. "
                    f"Use read_file / write_file / search_entity_references on the "
                    f"underlying .yaml file (check configuration.yaml's \"lovelace:\" "
                    f"section for the exact filename) instead of this tool."
                )

    fetch_command = {"type": "lovelace/config"}
    if url_path:
        fetch_command["url_path"] = url_path

    success, current = _ha_ws_call(fetch_command)
    if not success:
        return f"Error fetching current config for diff: {current}"

    if dry_run:
        current_text = json.dumps(current, indent=2, ensure_ascii=False)
        proposed_text = json.dumps(proposed, indent=2, ensure_ascii=False)
        diff_lines = list(
            unified_diff(
                current_text.splitlines(keepends=True),
                proposed_text.splitlines(keepends=True),
                fromfile=f"dashboard '{url_path or '(default)'}' (current)",
                tofile=f"dashboard '{url_path or '(default)'}' (proposed)",
            )
        )
        if not diff_lines:
            return "No changes: the proposed config is identical to the current one."
        header = f"PREVIEW ONLY — nothing saved yet. Diff for dashboard '{url_path or '(default)'}':\n\n"
        return header + "".join(diff_lines)

    save_command = {"type": "lovelace/config/save", "config": proposed}
    if url_path:
        save_command["url_path"] = url_path

    success, result = _ha_ws_call(save_command)
    if not success:
        return f"Error: Home Assistant rejected the save: {result}"

    return f"Dashboard '{url_path or '(default)'}' saved and applied live."


# -----------------------------------------------------------------------
# _ha_config_api
#
# Home Assistant has a REST endpoint pattern used by its OWN frontend
# editor for automations, scripts, and scenes: GET/POST/DELETE against
# /api/config/<domain>/config/<item_id>. Unlike dashboards, this is
# plain REST (no WebSocket needed) — but it's also not in the official
# REST API docs; it's stable and widely relied-upon (the HA UI itself
# uses it), just undocumented. Worth knowing if HA ever changes it.
#
# One helper, shared by every automation/script tool below, parameterized
# by domain ("automation" or "script") and HTTP method — same spirit as
# _ha_ws_call being shared by the dashboard tools.
#
# IMPORTANT BEHAVIOR: unlike write_file or save_ha_dashboard_config, a
# POST to this endpoint both SAVES and APPLIES LIVE in one step — no
# separate reload call needed. If a change doesn't seem to take effect,
# reload_ha_full_config is a manual fallback, but shouldn't normally be
# necessary.
# -----------------------------------------------------------------------
def _ha_config_api(method: str, domain: str, item_id: str, json_body: dict = None, timeout: int = 15):
    if not HA_URL or not HA_TOKEN:
        return False, "HA_URL or HA_TOKEN is not configured in .env."

    url = f"{HA_URL}/api/config/{domain}/config/{item_id}"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return False, f"Could not reach Home Assistant at {HA_URL}."

    if response.status_code == 401:
        return False, "Authentication failed (401). The token in .env may be invalid or expired."
    if response.status_code == 404:
        return False, f"No {domain} found with id '{item_id}'."
    if response.status_code not in (200, 201):
        # HA typically returns a JSON body with validation error detail
        # on a bad POST (e.g. malformed automation structure) — surface
        # it directly, since it's exactly the debugging info needed.
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        return False, f"Home Assistant returned status {response.status_code}: {detail}"

    if method == "DELETE":
        return True, None

    try:
        return True, response.json()
    except ValueError:
        return False, f"Home Assistant returned a non-JSON response: {response.text}"


# -----------------------------------------------------------------------
# TOOL 15: list_ha_automations
#
# There's no "list all automations" version of the config endpoint above
# — you have to already know an automation's config id to fetch it. This
# tool bridges that gap using the REGULAR states API (already used by
# get_entity_state) filtered to the "automation." domain, extracting the
# config id from each entity's "id" attribute (Home Assistant exposes it
# there specifically so tools like this can look it up).
# -----------------------------------------------------------------------
@mcp.tool()
def list_ha_automations() -> str:
    """List all automations with their entity_id, name, current on/off state, and config id.

    The config id (from each automation's "id" attribute) is what
    get_ha_automation_config / save_ha_automation_config /
    remove_ha_automation actually need — NOT the entity_id. Automations
    without an "id" attribute (rare, usually very old ones) can't be
    edited through the config API; note that in the output rather than
    guessing.

    Use this first whenever you need to find or confirm an automation's
    config id before calling the other automation tools.
    """
    if not HA_URL or not HA_TOKEN:
        return "Error: HA_URL or HA_TOKEN is not configured in .env."

    url = f"{HA_URL}/api/states"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return f"Error: could not reach Home Assistant at {HA_URL}."

    if response.status_code != 200:
        return f"Error: Home Assistant returned status {response.status_code}."

    automations = [e for e in response.json() if e["entity_id"].startswith("automation.")]
    if not automations:
        return "No automations found."

    lines = []
    for entity in automations:
        name = entity["attributes"].get("friendly_name", entity["entity_id"])
        state = entity["state"]
        config_id = entity["attributes"].get("id")
        id_note = f"config id: {config_id}" if config_id else "no config id — not editable via config API"
        lines.append(f"{name} ({entity['entity_id']}, {state}) — {id_note}")
    return "\n".join(sorted(lines))


# -----------------------------------------------------------------------
# TOOL 16: get_ha_automation_config
# -----------------------------------------------------------------------
@mcp.tool()
def get_ha_automation_config(automation_id: str) -> str:
    """Get an automation's full configuration as JSON, by its config id.

    automation_id is the CONFIG id (from list_ha_automations' output),
    not the entity_id — e.g. "1733024053997", not "automation.my_alarm".

    Returns the automation's trigger/condition/action structure as
    pretty-printed JSON. Use this to see the current definition before
    proposing changes with save_ha_automation_config.
    """
    success, data = _ha_config_api("GET", "automation", automation_id)
    if not success:
        return f"Error: {data}"
    return json.dumps(data, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------
# TOOL 17: save_ha_automation_config
#
# Same dry_run-by-default diff-preview philosophy as write_file and
# save_ha_dashboard_config. One difference worth knowing: this single
# tool handles BOTH creating a new automation and updating an existing
# one — Home Assistant's config API doesn't distinguish, it just
# upserts to whatever id you POST to. To CREATE a new automation,
# generate a fresh unique id yourself (a timestamp-like string, e.g.
# str(int(time.time() * 1000)) works fine) and POST the full config to
# that id — there's no separate "create" endpoint.
# -----------------------------------------------------------------------
@mcp.tool()
def save_ha_automation_config(automation_id: str, config: str, dry_run: bool = True) -> str:
    """Create or update an automation. Applies live immediately — no reload needed.

    automation_id: the config id. For an EXISTING automation, get this
    from list_ha_automations. For a NEW automation, make up a fresh
    unique id (e.g. a millisecond timestamp string) — there's no
    separate "create" call, POSTing to an id that doesn't exist yet
    creates it.

    config: the COMPLETE automation definition as a JSON string —
    alias, trigger, condition, action, mode, etc. This replaces the
    whole automation; always fetch the current config with
    get_ha_automation_config first if you're editing an existing one.

    dry_run controls whether this actually saves anything:
      - True (default): fetch the current config (empty if this is a
        new automation) and return a diff against your proposed config,
        WITHOUT saving. Always call with dry_run=True first.
      - False: actually save. Home Assistant validates the automation
        structure server-side and rejects it with a clear error if
        malformed — real validation, not just a raw file write. Applies
        LIVE immediately; no separate reload call is needed.
    """
    try:
        proposed = json.loads(config)
    except json.JSONDecodeError as e:
        return f"Error: config is not valid JSON: {e}"

    # Fetch current state for the diff. A 404 here just means this is a
    # brand-new automation — treat "current" as empty rather than
    # treating the fetch failure as a hard error.
    success, current = _ha_config_api("GET", "automation", automation_id)
    current_text = json.dumps(current, indent=2, ensure_ascii=False) if success else ""

    if dry_run:
        proposed_text = json.dumps(proposed, indent=2, ensure_ascii=False)
        diff_lines = list(
            unified_diff(
                current_text.splitlines(keepends=True),
                proposed_text.splitlines(keepends=True),
                fromfile=f"automation '{automation_id}' (current{'​' if success else ', does not exist yet'})",
                tofile=f"automation '{automation_id}' (proposed)",
            )
        )
        if not diff_lines:
            return "No changes: the proposed config is identical to the current one."
        header = f"PREVIEW ONLY — nothing saved yet. Diff for automation '{automation_id}':\n\n"
        return header + "".join(diff_lines)

    success, result = _ha_config_api("POST", "automation", automation_id, json_body=proposed)
    if not success:
        return f"Error: Home Assistant rejected the save: {result}"

    return f"Automation '{automation_id}' saved and applied live."


# -----------------------------------------------------------------------
# TOOL 18: remove_ha_automation
#
# Same dry_run gating as the write/save tools above, but there's no
# "diff" for a deletion — dry_run instead fetches and echoes back what
# WOULD be deleted, so you can confirm it's the right one before it's
# gone. Unlike write_file, there's no automatic backup here (HA's config
# API has no equivalent of a .bak file) — the dry_run preview is the
# only safety net, so treat it as mandatory in practice, not optional.
# -----------------------------------------------------------------------
@mcp.tool()
def remove_ha_automation(automation_id: str, dry_run: bool = True) -> str:
    """Delete an automation by its config id. No undo — always dry_run first.

    automation_id: the config id (from list_ha_automations), not the
    entity_id.

    dry_run controls whether this actually deletes anything:
      - True (default): fetch and return the automation's current
        config so you can confirm it's the right one, WITHOUT deleting.
      - False: actually delete it. There is no automatic backup for
        this operation (unlike write_file's .bak) — if you might want
        the config back, save it with get_ha_automation_config BEFORE
        deleting.
    """
    success, current = _ha_config_api("GET", "automation", automation_id)
    if not success:
        return f"Error: {current}"

    if dry_run:
        current_text = json.dumps(current, indent=2, ensure_ascii=False)
        return (
            f"PREVIEW ONLY — nothing deleted yet. This automation would be removed:\n\n"
            f"{current_text}"
        )

    success, _ = _ha_config_api("DELETE", "automation", automation_id)
    if not success:
        return f"Error: Home Assistant rejected the delete."

    return f"Automation '{automation_id}' deleted."


# -----------------------------------------------------------------------
# TOOL 19: list_ha_scripts
#
# Scripts work slightly differently from automations: a script's config
# id IS the same as its entity_id's object_id (the part after "script."),
# not a separate hidden attribute. So this tool is simpler than
# list_ha_automations — no need to dig through attributes.
# -----------------------------------------------------------------------
@mcp.tool()
def list_ha_scripts() -> str:
    """List all scripts with their entity_id, name, and config id.

    Unlike automations, a script's config id is always the part of its
    entity_id after "script." — e.g. entity_id "script.movie_mode" has
    config id "movie_mode". This tool extracts that for convenience so
    you don't have to do the string split yourself.
    """
    if not HA_URL or not HA_TOKEN:
        return "Error: HA_URL or HA_TOKEN is not configured in .env."

    url = f"{HA_URL}/api/states"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return f"Error: could not reach Home Assistant at {HA_URL}."

    if response.status_code != 200:
        return f"Error: Home Assistant returned status {response.status_code}."

    scripts = [e for e in response.json() if e["entity_id"].startswith("script.")]
    if not scripts:
        return "No scripts found."

    lines = []
    for entity in scripts:
        name = entity["attributes"].get("friendly_name", entity["entity_id"])
        state = entity["state"]
        config_id = entity["entity_id"].split(".", 1)[1]
        lines.append(f"{name} ({entity['entity_id']}, {state}) — config id: {config_id}")
    return "\n".join(sorted(lines))


# -----------------------------------------------------------------------
# TOOL 20: get_ha_script_config
# -----------------------------------------------------------------------
@mcp.tool()
def get_ha_script_config(script_id: str) -> str:
    """Get a script's full configuration as JSON, by its config id.

    script_id is the part of the entity_id after "script." — e.g.
    "movie_mode" for entity_id "script.movie_mode" (see
    list_ha_scripts).

    Returns the script's sequence/fields structure as pretty-printed
    JSON. Use this to see the current definition before proposing
    changes with save_ha_script_config.
    """
    success, data = _ha_config_api("GET", "script", script_id)
    if not success:
        return f"Error: {data}"
    return json.dumps(data, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------
# TOOL 21: save_ha_script_config
#
# Same shape as save_ha_automation_config — same dry_run-by-default
# diff preview, same create-or-update-via-upsert behavior. For a NEW
# script, script_id can be any valid slug you choose (it becomes the
# script's object_id / the last part of its entity_id) — no timestamp
# needed like automations, since scripts are identified by a meaningful
# name rather than a generated id.
# -----------------------------------------------------------------------
@mcp.tool()
def save_ha_script_config(script_id: str, config: str, dry_run: bool = True) -> str:
    """Create or update a script. Applies live immediately — no reload needed.

    script_id: for an EXISTING script, get this from list_ha_scripts.
    For a NEW script, choose a slug-style id yourself (e.g.
    "movie_mode") — it becomes the script's entity_id
    (script.movie_mode). POSTing to an id that doesn't exist yet
    creates it; no separate "create" call.

    config: the COMPLETE script definition as a JSON string — alias,
    sequence, fields, mode, etc. This replaces the whole script; always
    fetch the current config with get_ha_script_config first if editing
    an existing one.

    dry_run controls whether this actually saves anything:
      - True (default): fetch the current config (empty if this is a
        new script) and return a diff against your proposed config,
        WITHOUT saving. Always call with dry_run=True first.
      - False: actually save. Home Assistant validates the script
        structure server-side and rejects it with a clear error if
        malformed. Applies LIVE immediately.
    """
    try:
        proposed = json.loads(config)
    except json.JSONDecodeError as e:
        return f"Error: config is not valid JSON: {e}"

    success, current = _ha_config_api("GET", "script", script_id)
    current_text = json.dumps(current, indent=2, ensure_ascii=False) if success else ""

    if dry_run:
        proposed_text = json.dumps(proposed, indent=2, ensure_ascii=False)
        diff_lines = list(
            unified_diff(
                current_text.splitlines(keepends=True),
                proposed_text.splitlines(keepends=True),
                fromfile=f"script '{script_id}' (current{'' if success else ', does not exist yet'})",
                tofile=f"script '{script_id}' (proposed)",
            )
        )
        if not diff_lines:
            return "No changes: the proposed config is identical to the current one."
        header = f"PREVIEW ONLY — nothing saved yet. Diff for script '{script_id}':\n\n"
        return header + "".join(diff_lines)

    success, result = _ha_config_api("POST", "script", script_id, json_body=proposed)
    if not success:
        return f"Error: Home Assistant rejected the save: {result}"

    return f"Script '{script_id}' saved and applied live."


# -----------------------------------------------------------------------
# TOOL 22: remove_ha_script
#
# Same dry_run-gated preview-then-delete shape as remove_ha_automation.
# -----------------------------------------------------------------------
@mcp.tool()
def remove_ha_script(script_id: str, dry_run: bool = True) -> str:
    """Delete a script by its config id. No undo — always dry_run first.

    script_id: from list_ha_scripts (the part of the entity_id after
    "script.").

    dry_run controls whether this actually deletes anything:
      - True (default): fetch and return the script's current config so
        you can confirm it's the right one, WITHOUT deleting.
      - False: actually delete it. No automatic backup — save the
        config with get_ha_script_config first if you might want it back.
    """
    success, current = _ha_config_api("GET", "script", script_id)
    if not success:
        return f"Error: {current}"

    if dry_run:
        current_text = json.dumps(current, indent=2, ensure_ascii=False)
        return (
            f"PREVIEW ONLY — nothing deleted yet. This script would be removed:\n\n"
            f"{current_text}"
        )

    success, _ = _ha_config_api("DELETE", "script", script_id)
    if not success:
        return f"Error: Home Assistant rejected the delete."

    return f"Script '{script_id}' deleted."


# -----------------------------------------------------------------------
# _fetch_traces / _fetch_trace_detail
#
# Traces are Home Assistant's step-by-step execution record for a single
# automation or script run — which trigger fired, which conditions
# passed/failed, what each action did, with timestamps and errors. This
# answers a genuinely different question than get_ha_error_log: the log
# tells you something went wrong somewhere; a trace tells you EXACTLY
# what happened during one specific run of one specific automation.
#
# Only available over the WebSocket API ("trace/list" and "trace/get"
# commands) — reuses _ha_ws_call, same as the dashboard tools.
#
# KNOWN GOTCHA, deliberately avoided here: trace/list returns traces in
# CHRONOLOGICAL order (oldest first). A naive `traces[:limit]` therefore
# returns the OLDEST runs, not the most recent ones — exactly backwards
# from what "show me the last N runs" should mean. This is a real bug
# that shipped in at least one other Home Assistant MCP server. Fixed
# here by slicing from the end (`traces[-limit:]`) and reversing so the
# most recent run is listed first.
# -----------------------------------------------------------------------
def _fetch_traces(domain: str, item_id: str, limit: int):
    success, data = _ha_ws_call({"type": "trace/list", "domain": domain, "item_id": item_id})
    if not success:
        return False, data

    # Oldest-first from HA — take the last `limit` (the most recent),
    # then reverse so the newest run appears first in our output.
    recent = data[-limit:] if limit else data
    recent.reverse()
    return True, recent


def _fetch_trace_detail(domain: str, item_id: str, run_id: str):
    return _ha_ws_call({"type": "trace/get", "domain": domain, "item_id": item_id, "run_id": run_id})


def _format_trace_summary_line(trace: dict) -> str:
    """One line per trace for list output: run_id, when, and outcome."""
    run_id = trace.get("run_id", "unknown")
    timestamp = trace.get("timestamp", {})
    started = timestamp.get("start", "unknown time")
    state = trace.get("state", "unknown")
    script_execution = trace.get("script_execution")
    outcome = f"state: {state}"
    if script_execution:
        outcome += f", result: {script_execution}"
    if trace.get("error"):
        outcome += f", ERROR: {trace['error']}"
    return f"run_id: {run_id} | started: {started} | {outcome}"


# -----------------------------------------------------------------------
# TOOL 23: get_ha_automation_traces
# -----------------------------------------------------------------------
@mcp.tool()
def get_ha_automation_traces(automation_id: str, limit: int = 5) -> str:
    """Get recent execution traces for an automation — the most recent runs first.

    automation_id: the config id (from list_ha_automations), not the
    entity_id.

    limit: how many of the MOST RECENT runs to return (default 5).

    Each result line shows the run_id, when it started, and its outcome
    (state, result, and any error). Use a run_id from this list with
    get_ha_automation_trace_detail to see the full step-by-step record
    of that specific run — which trigger fired, which conditions passed
    or failed, and what each action did.

    Use this to debug "why didn't my automation fire the way I
    expected" — a genuinely different question than get_ha_error_log,
    which only shows general system warnings/errors, not a per-run
    execution history.
    """
    success, traces = _fetch_traces("automation", automation_id, limit)
    if not success:
        return f"Error: {traces}"

    if not traces:
        return f"No traces found for automation '{automation_id}' (it may not have run yet, or traces have expired)."

    lines = [_format_trace_summary_line(t) for t in traces]
    return f"Most recent {len(lines)} run(s) of automation '{automation_id}':\n\n" + "\n".join(lines)


# -----------------------------------------------------------------------
# TOOL 24: get_ha_automation_trace_detail
# -----------------------------------------------------------------------
@mcp.tool()
def get_ha_automation_trace_detail(automation_id: str, run_id: str) -> str:
    """Get the full step-by-step execution detail for one automation run.

    automation_id: the config id. run_id: from get_ha_automation_traces'
    output for the specific run you want to inspect.

    Returns the complete trace as pretty-printed JSON — trigger data,
    each condition's pass/fail result, each action's execution detail,
    timing, and any errors. This is the deep-dive equivalent of clicking
    a specific run in Home Assistant's own trace viewer.
    """
    success, data = _fetch_trace_detail("automation", automation_id, run_id)
    if not success:
        return f"Error: {data}"
    return json.dumps(data, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------
# TOOL 25: get_ha_script_traces
# TOOL 26: get_ha_script_trace_detail
#
# Scripts support traces too, same shape as automations — identical
# tools, just pointed at the "script" domain instead.
# -----------------------------------------------------------------------
@mcp.tool()
def get_ha_script_traces(script_id: str, limit: int = 5) -> str:
    """Get recent execution traces for a script — the most recent runs first.

    script_id: from list_ha_scripts (the part of the entity_id after
    "script.").

    limit: how many of the MOST RECENT runs to return (default 5).

    Same shape as get_ha_automation_traces, applied to scripts instead.
    Use a run_id from this list with get_ha_script_trace_detail to see
    the full step-by-step record of that specific run.
    """
    success, traces = _fetch_traces("script", script_id, limit)
    if not success:
        return f"Error: {traces}"

    if not traces:
        return f"No traces found for script '{script_id}' (it may not have run yet, or traces have expired)."

    lines = [_format_trace_summary_line(t) for t in traces]
    return f"Most recent {len(lines)} run(s) of script '{script_id}':\n\n" + "\n".join(lines)


@mcp.tool()
def get_ha_script_trace_detail(script_id: str, run_id: str) -> str:
    """Get the full step-by-step execution detail for one script run.

    script_id: from list_ha_scripts. run_id: from get_ha_script_traces'
    output for the specific run you want to inspect.

    Returns the complete trace as pretty-printed JSON, same shape as
    get_ha_automation_trace_detail.
    """
    success, data = _fetch_trace_detail("script", script_id, run_id)
    if not success:
        return f"Error: {data}"
    return json.dumps(data, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------
# _node_red_login
#
# Node-RED's admin API auth is a resource-owner-password OAuth2-style
# flow: POST username/password to /auth/token, get back a short-lived
# access_token, use it as a Bearer header on subsequent calls. No
# permanent token to cache here (unlike HA_TOKEN) — this runs fresh
# every time a Node-RED tool is called, same "no persistent connection
# between calls" philosophy as _ha_ws_call.
# -----------------------------------------------------------------------
def _node_red_login():
    if not NODE_RED_URL or not NODE_RED_USERNAME or not NODE_RED_PASSWORD:
        return False, "NODE_RED_URL, NODE_RED_USERNAME, or NODE_RED_PASSWORD is not configured in .env."

    url = f"{NODE_RED_URL}/auth/token"
    payload = {
        "client_id": "node-red-editor",
        "grant_type": "password",
        "scope": "*",
        "username": NODE_RED_USERNAME,
        "password": NODE_RED_PASSWORD,
    }

    try:
        response = requests.post(url, data=payload, timeout=15)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return False, f"Could not reach Node-RED at {NODE_RED_URL}."

    if response.status_code != 200:
        return False, (
            f"Node-RED login failed (status {response.status_code}). "
            f"Check NODE_RED_USERNAME/NODE_RED_PASSWORD in .env are correct."
        )

    token = response.json().get("access_token")
    if not token:
        return False, "Node-RED login succeeded but no access_token was returned."

    return True, token


def _node_red_request(method: str, path: str, json_body=None, extra_headers: dict = None):
    """Shared request helper: call one Node-RED admin API endpoint.

    AUTH HISTORY (in case this needs revisiting): two earlier approaches
    were tried and both failed with 401 — (1) Node-RED's own
    username/password /auth/token OAuth-style login (_node_red_login
    above, now unused), and (2) reusing HA_TOKEN as a Bearer header,
    under the theory that this add-on proxies auth through Home
    Assistant. Neither matched what the browser actually does: capturing
    the real browser login in dev tools showed NO POST/fetch request at
    all when logging in — consistent with the browser's NATIVE HTTP
    Basic Auth prompt (handled at the protocol level, before any page
    JavaScript runs, so it never shows as an XHR/fetch in the Network
    tab). This sends the same NODE_RED_USERNAME/PASSWORD as standard
    HTTP Basic Auth on every request instead — no separate login step,
    since Basic Auth is sent with each individual request by design.

    CONTENT-NEGOTIATION GOTCHA (found while debugging list_node_red_nodes):
    some Node-RED Admin API endpoints — confirmed for /nodes — are
    DUAL-PURPOSE: the browser editor calls the same URL to fetch raw
    .html node-definition templates (to build the palette UI), and that
    HTML response is the DEFAULT unless the request explicitly asks for
    JSON. Without an "Accept: application/json" header, GET /nodes
    returned ~1.86MB of concatenated HTML instead of the expected JSON
    module list — which then failed JSON parsing and got embedded
    whole into an error message, masquerading as a "result too large"
    error with no obvious cause. Sending this header on every request
    avoids the whole class of bug, not just this one instance of it.
    """
    if not NODE_RED_URL or not NODE_RED_USERNAME or not NODE_RED_PASSWORD:
        return False, "NODE_RED_URL, NODE_RED_USERNAME, or NODE_RED_PASSWORD is not configured in .env."

    url = f"{NODE_RED_URL}{path}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)

    try:
        response = requests.request(
            method, url, headers=headers, json=json_body,
            auth=(NODE_RED_USERNAME, NODE_RED_PASSWORD), timeout=30,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return False, f"Could not reach Node-RED at {NODE_RED_URL}."

    if response.status_code == 401 or response.status_code == 403:
        return False, f"Node-RED rejected the request (status {response.status_code}). Check NODE_RED_USERNAME/NODE_RED_PASSWORD in .env."
    if response.status_code not in (200, 204):
        return False, f"Node-RED returned status {response.status_code}: {response.text}"

    if response.status_code == 204 or not response.text:
        return True, None

    try:
        return True, response.json()
    except ValueError:
        return False, f"Node-RED returned a non-JSON response: {response.text}"


# -----------------------------------------------------------------------
# TOOL 27: get_node_red_flows
#
# Node-RED's /flows endpoint returns the ENTIRE flow set as one big JSON
# array — every tab, every node, everything. There's no per-flow fetch
# in the base API. This is the read-only starting point before searching
# or editing anything.
# -----------------------------------------------------------------------
@mcp.tool()
def get_node_red_flows() -> str:
    """Get the complete Node-RED flow configuration as JSON.

    Returns every flow tab and every node, pretty-printed. This is the
    full picture Node-RED itself works from — there's no way to fetch
    just one flow through the base API. For finding something specific
    (like a node referencing a particular entity_id or device name),
    search_node_red_flows is usually more useful than reading this whole
    thing by hand.
    """
    success, data = _node_red_request("GET", "/flows")
    if not success:
        return f"Error: {data}"
    return json.dumps(data, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------
# TOOL 28: search_node_red_flows
#
# Reuses the exact same trick as search_entity_references: this is really
# just a substring search, so it works for entity IDs, device names,
# friendly names — anything appearing as text somewhere in a node's
# JSON. Directly useful for the scenario that prompted building this:
# finding every node that references an old/renamed device so nothing
# gets missed after a rename.
# -----------------------------------------------------------------------
@mcp.tool()
def search_node_red_flows(text: str, max_results: int = 50) -> str:
    """Search all Node-RED flows for nodes containing specific text.

    text is any substring to search for — an entity_id, a device name,
    a friendly name, anything. This is a plain substring search across
    every node's JSON representation (name, properties, everything), not
    limited to a specific field.

    Each result shows the node's id, type, name (if it has one), which
    flow tab it's on, and a compact snippet of the node's JSON so you
    can recognize it even without a name.

    Use this before renaming a device or entity in Home Assistant to
    find every Node-RED node that references the old name/id, so
    nothing breaks silently — this was built specifically because a
    device rename (AFTMM -> a new friendly name) broke a flow that was
    hard to track down by hand.
    """
    success, data = _node_red_request("GET", "/flows")
    if not success:
        return f"Error: {data}"

    # Build a quick tab-id -> tab-name lookup first, so results can show
    # a human-readable flow name instead of just an opaque tab id.
    tab_names = {
        node["id"]: node.get("label", node["id"])
        for node in data
        if node.get("type") == "tab"
    }

    matches = []
    for node in data:
        node_json = json.dumps(node, ensure_ascii=False)
        if text not in node_json:
            continue

        node_id = node.get("id", "unknown")
        node_type = node.get("type", "unknown")
        node_name = node.get("name") or "(unnamed)"
        tab_id = node.get("z")
        tab_name = tab_names.get(tab_id, tab_id or "(no tab / global)")

        snippet = node_json
        if len(snippet) > 200:
            snippet = snippet[:200] + "..."

        matches.append(
            f"node id: {node_id} | type: {node_type} | name: {node_name} | "
            f"tab: {tab_name} | contents: {snippet}"
        )
        if len(matches) >= max_results:
            break

    if not matches:
        return f"No nodes found containing '{text}'."

    header = f"Found {len(matches)} node(s) containing '{text}':\n\n"
    footer = f"\n\n(Results capped at {max_results}; there may be more matches not shown.)" if len(matches) >= max_results else ""
    return header + "\n".join(matches) + footer


# -----------------------------------------------------------------------
# TOOL 29: save_node_red_flows
#
# Same dry_run-by-default diff-preview philosophy as every other write
# tool in this server. One structural difference from the HA tools:
# Node-RED's full-deploy endpoint replaces EVERY flow at once — there's
# no way to update just one node through the base admin API. So this
# always operates on the complete flow set, same as get_node_red_flows
# returns; fetch, modify the specific node(s) you care about, and pass
# the WHOLE array back.
#
# The "Node-RED-Deployment-Type: full" header tells Node-RED to fully
# stop and restart all flows with the new configuration (as opposed to
# quicker partial-restart deploy modes) — the safest, most predictable
# option for a change made outside the editor.
# -----------------------------------------------------------------------
@mcp.tool()
def save_node_red_flows(flows_json: str, dry_run: bool = True) -> str:
    """Save the complete Node-RED flow configuration. Applies live immediately.

    flows_json: the COMPLETE flow array as a JSON string — every tab,
    every node. This replaces everything at once; Node-RED's base API
    has no way to update a single node in isolation. Always fetch the
    current flows with get_node_red_flows first, make your specific
    edit, and pass the complete modified array back here.

    dry_run controls whether this actually deploys anything:
      - True (default): fetch the current flows, diff them against your
        proposed flows, and return that diff WITHOUT deploying. Always
        call with dry_run=True first and check the diff only shows the
        change you intended — since this replaces everything, a mistake
        here could affect flows you didn't mean to touch.
      - False: actually deploy. Applies LIVE immediately using a full
        redeploy (all flows stop and restart with the new config) — the
        safest, most predictable deploy mode for a change made outside
        the Node-RED editor.
    """
    try:
        proposed = json.loads(flows_json)
    except json.JSONDecodeError as e:
        return f"Error: flows_json is not valid JSON: {e}"

    success, current = _node_red_request("GET", "/flows")
    if not success:
        return f"Error fetching current flows for diff: {current}"

    if dry_run:
        current_text = json.dumps(current, indent=2, ensure_ascii=False)
        proposed_text = json.dumps(proposed, indent=2, ensure_ascii=False)
        diff_lines = list(
            unified_diff(
                current_text.splitlines(keepends=True),
                proposed_text.splitlines(keepends=True),
                fromfile="Node-RED flows (current)",
                tofile="Node-RED flows (proposed)",
            )
        )
        if not diff_lines:
            return "No changes: the proposed flows are identical to the current ones."
        header = "PREVIEW ONLY — nothing deployed yet. Diff for Node-RED flows:\n\n"
        return header + "".join(diff_lines)

    success, result = _node_red_request(
        "POST", "/flows", json_body=proposed, extra_headers={"Node-RED-Deployment-Type": "full"}
    )
    if not success:
        return f"Error: Node-RED rejected the deploy: {result}"

    return "Node-RED flows saved and deployed live."


# -----------------------------------------------------------------------
# TOOL 30: list_node_red_flows
#
# Node-RED's Admin API has no way to look up a flow by name/label —
# only by id (a known, longstanding gap people have requested and never
# gotten, confirmed via a Node-RED forum thread). This tool works
# around that by fetching everything via get_node_red_flows and
# filtering down to just the "type": "tab" entries — id + label pairs,
# nothing else — so finding a tab's id by its name doesn't require
# wading through the full multi-thousand-line flow dump. Use the id
# this returns with get_node_red_flow / save_node_red_flow below.
# -----------------------------------------------------------------------
@mcp.tool()
def list_node_red_flows() -> str:
    """List every Node-RED flow (tab) by name, with its id.

    Returns each tab's label and id — nothing else. Node-RED has no
    built-in way to look up a flow by name, only by id, so use this
    first to find the id for a flow you know by name (e.g. "Sleep
    Bedroom TV"), then pass that id to get_node_red_flow or
    save_node_red_flow.
    """
    success, data = _node_red_request("GET", "/flows")
    if not success:
        return f"Error: {data}"

    tabs = [node for node in data if node.get("type") == "tab"]
    if not tabs:
        return "No flows found."

    lines = [f"{tab.get('label', '(untitled)')}  (id: {tab['id']})" for tab in tabs]
    return "\n".join(sorted(lines))


# -----------------------------------------------------------------------
# TOOL 31: get_node_red_flow
# TOOL 32: save_node_red_flow
#
# The safer alternative to get_node_red_flows / save_node_red_flows:
# Node-RED's Admin API has a SINGULAR /flow/:id endpoint (note: "flow",
# not "flows") that operates on exactly ONE tab, not the whole system.
# Per Node-RED's own docs, a PUT to /flow/:id stops only that flow's
# nodes before starting the new version — every other tab keeps running
# completely untouched.
#
# This matters a lot for risk: save_node_red_flows requires fetching,
# diffing, and resending the ENTIRE flow set (every tab, every node)
# just to change one thing — a much bigger blast radius if a mistake
# slips through. These two tools scope every read and write to a single
# tab instead, the same way search_entity_references / write_file are
# scoped to one file rather than your whole HA config.
#
# Use list_node_red_flows (or search_node_red_flows) first to find the
# tab id you want — then use these two for the actual edit.
# -----------------------------------------------------------------------
@mcp.tool()
def get_node_red_flow(tab_id: str) -> str:
    """Get ONE Node-RED flow (tab) by its id, not the whole flow set.

    tab_id is a flow tab's id (from get_node_red_flows — look for
    entries with "type": "tab" to find id/label pairs).

    Returns just that tab's nodes as pretty-printed JSON — much smaller
    and safer to work with than the full multi-tab result from
    get_node_red_flows when you only care about one flow.
    """
    success, data = _node_red_request("GET", f"/flow/{tab_id}")
    if not success:
        return f"Error: {data}"
    return json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
def save_node_red_flow(tab_id: str, flow_json: str, dry_run: bool = True) -> str:
    """Save ONE Node-RED flow (tab) by its id. Applies live immediately.

    tab_id: the flow tab's id (from get_node_red_flows or
    get_node_red_flow).

    flow_json: the COMPLETE configuration for THIS ONE TAB as a JSON
    string — {"id": ..., "label": ..., "nodes": [...]}. This replaces
    only this tab's nodes; every other tab in Node-RED is completely
    unaffected, unlike save_node_red_flows which touches everything at
    once. Always fetch the current tab with get_node_red_flow first,
    make your specific edit, and pass the complete modified object back
    here.

    dry_run controls whether this actually saves anything:
      - True (default): fetch the current tab, diff it against your
        proposed version, and return that diff WITHOUT saving. Always
        call with dry_run=True first.
      - False: actually save. Node-RED stops only this tab's nodes,
        applies the new configuration, and restarts them — every other
        flow keeps running the whole time.
    """
    try:
        proposed = json.loads(flow_json)
    except json.JSONDecodeError as e:
        return f"Error: flow_json is not valid JSON: {e}"

    success, current = _node_red_request("GET", f"/flow/{tab_id}")
    if not success:
        return f"Error fetching current flow for diff: {current}"

    if dry_run:
        current_text = json.dumps(current, indent=2, ensure_ascii=False)
        proposed_text = json.dumps(proposed, indent=2, ensure_ascii=False)
        diff_lines = list(
            unified_diff(
                current_text.splitlines(keepends=True),
                proposed_text.splitlines(keepends=True),
                fromfile=f"flow '{tab_id}' (current)",
                tofile=f"flow '{tab_id}' (proposed)",
            )
        )
        if not diff_lines:
            return "No changes: the proposed flow is identical to the current one."
        header = f"PREVIEW ONLY — nothing saved yet. Diff for flow '{tab_id}':\n\n"
        return header + "".join(diff_lines)

    success, result = _node_red_request("PUT", f"/flow/{tab_id}", json_body=proposed)
    if not success:
        return f"Error: Node-RED rejected the save: {result}"

    return f"Flow '{tab_id}' saved and applied live (other flows untouched)."


# -----------------------------------------------------------------------
# TOOL 33: create_node_red_flow
#
# save_node_red_flow (PUT /flow/:id) is for UPDATING an existing tab —
# it always fetches the current version first, which would fail for a
# tab that doesn't exist yet. Creating a genuinely NEW flow needs
# Node-RED's separate POST /flow endpoint instead. One real quirk to
# know: per Node-RED's own docs, POST /flow ALWAYS assigns a fresh id
# itself — any id you might try to specify is discarded and replaced.
# So there's no "choose your own id" option here, unlike automations/
# scripts elsewhere in this server.
#
# No natural "diff" exists for something that doesn't exist yet, so
# dry_run here just returns the proposed flow for review rather than a
# unified_diff against nothing.
# -----------------------------------------------------------------------
@mcp.tool()
def create_node_red_flow(label: str, nodes_json: str, dry_run: bool = True) -> str:
    """Create a brand-new Node-RED flow (tab). Applies live immediately.

    label: the new tab's display name.

    nodes_json: a JSON array of node objects for this new tab (the
    "nodes" list only — not wrapped in {"id":..., "nodes":...}). Each
    node needs at minimum a unique "id" and "type". Node-RED will assign
    the TAB's own id itself; any tab-level id you might include is
    discarded, per Node-RED's own API behavior.

    dry_run controls whether this actually creates anything:
      - True (default): return the proposed flow (label + nodes) for
        review WITHOUT creating it. There's nothing existing to diff
        against, so this just echoes back what would be sent.
      - False: actually create it. Applies live immediately. Returns
        the new tab's id — save it if you'll need to edit this flow
        again with get_node_red_flow/save_node_red_flow.
    """
    try:
        nodes = json.loads(nodes_json)
    except json.JSONDecodeError as e:
        return f"Error: nodes_json is not valid JSON: {e}"

    if not isinstance(nodes, list):
        return "Error: nodes_json must be a JSON array of node objects."

    proposed = {"label": label, "nodes": nodes}

    if dry_run:
        return (
            f"PREVIEW ONLY — nothing created yet. This flow would be created:\n\n"
            f"{json.dumps(proposed, indent=2, ensure_ascii=False)}"
        )

    success, result = _node_red_request("POST", "/flow", json_body=proposed)
    if not success:
        return f"Error: Node-RED rejected the create: {result}"

    new_id = result.get("id") if isinstance(result, dict) else result
    return f"Flow '{label}' created and applied live. New tab id: {new_id}"


# -----------------------------------------------------------------------
# TOOL 34: remove_node_red_flow
#
# Same dry_run-gated preview-then-delete shape as remove_ha_automation.
# No automatic backup exists for this either — the dry_run preview
# (echoing back the full flow that would be deleted) is the only safety
# net, so treat it as mandatory in practice. If you might want the flow
# back later, get_node_red_flow first and save the JSON somewhere before
# deleting.
# -----------------------------------------------------------------------
@mcp.tool()
def remove_node_red_flow(tab_id: str, dry_run: bool = True) -> str:
    """Delete a Node-RED flow (tab) by its id. No undo — always dry_run first.

    tab_id: from list_node_red_flows or get_node_red_flows.

    dry_run controls whether this actually deletes anything:
      - True (default): fetch and return the flow's current nodes so
        you can confirm it's the right one, WITHOUT deleting.
      - False: actually delete it. There is no automatic backup — use
        get_node_red_flow to save the JSON first if you might want the
        flow back later.
    """
    success, current = _node_red_request("GET", f"/flow/{tab_id}")
    if not success:
        return f"Error: {current}"

    if dry_run:
        current_text = json.dumps(current, indent=2, ensure_ascii=False)
        return (
            f"PREVIEW ONLY — nothing deleted yet. This flow would be removed:\n\n"
            f"{current_text}"
        )

    success, _ = _node_red_request("DELETE", f"/flow/{tab_id}")
    if not success:
        return f"Error: Node-RED rejected the delete."

    return f"Flow '{tab_id}' deleted."


# -----------------------------------------------------------------------
# TOOL 35: list_node_red_nodes
#
# Lists installed node modules with name, version, enabled state, and a
# node-type count. Two real bugs got fixed here, in sequence:
#   1. GET /nodes defaulted to returning ~1.86MB of raw HTML (the
#      editor's palette templates) instead of JSON, unless the request
#      sent "Accept: application/json" — now fixed in _node_red_request
#      above, shared by every Node-RED tool.
#   2. Once that was fixed, the REAL response shape turned out to differ
#      from what the POST /nodes (single-install) docs example implied.
#      GET /nodes (list-all) returns a FLAT array where each entry is
#      already one individual node "set" (e.g. "inject", "debug",
#      "switch" each as their own entry) with "enabled" and "types"
#      directly on it — not grouped under a nested "nodes" list the way
#      a single-module install response is. Confirmed by seeing real
#      output: every row showed "enabled: ?" and "0 node type(s)"
#      because the code was looking one level too deep.
# -----------------------------------------------------------------------
@mcp.tool()
def list_node_red_nodes() -> str:
    """List all installed node sets with their type name, module, version, and enabled state.

    Each Node-RED "node set" is roughly one node type (e.g. "inject",
    "debug", "switch") — several sets are often provided by the same
    underlying npm module (shown as "module:"). Use this before
    designing a new flow to confirm a node type is installed, or before
    install_node_red_node to check if a module is already there. For
    the complete raw detail of one entry, use
    get_node_red_node_info(module) with its module name.
    """
    success, data = _node_red_request("GET", "/nodes")
    if not success:
        return f"Error: {data}"

    if not data or not isinstance(data, list):
        return "No node modules found."

    lines = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "unknown")
        module = entry.get("module", name)
        version = entry.get("version", "?")
        enabled = entry.get("enabled", "?")
        types = entry.get("types", [])
        type_desc = ", ".join(types) if types else "(no types listed)"
        lines.append(f"{name} (module: {module}, v{version}, enabled: {enabled}) — {type_desc}")
    return "\n".join(sorted(lines))


# -----------------------------------------------------------------------
# TOOL 35b: get_node_red_node_info
#
# The companion to list_node_red_nodes' condensed output: that tool only
# shows a TYPE COUNT per module (to stay under the response size limit),
# not the individual type names. This drills into ONE module to see its
# full type list and detail — safe to return in full since it's scoped
# to a single module, not all of them at once.
# -----------------------------------------------------------------------
@mcp.tool()
def get_node_red_node_info(module: str) -> str:
    """Get a node module's full information, including every type it provides.

    module: the module name (from list_node_red_nodes).

    Returns the complete detail for just this one module as pretty-
    printed JSON, including every individual node type name — safe to
    return in full since it's scoped to one module, unlike
    list_node_red_nodes' condensed summary of everything at once.
    """
    success, data = _node_red_request("GET", f"/nodes/{module}")
    if not success:
        return f"Error: {data}"
    return json.dumps(data, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------
# TOOL 36: install_node_red_node
#
# Genuinely different in kind from every other write tool in this server:
# this triggers a real `npm install` on the Node-RED host, downloading
# and running third-party code — not just a config change. There's
# nothing meaningful to "diff" against (nothing existed before), so
# dry_run here just confirms the module name back to you before
# committing to an actual install, which can take a while and may fail
# for reasons outside this server's control (network, bad package name,
# incompatible Node-RED version).
# -----------------------------------------------------------------------
@mcp.tool()
def install_node_red_node(module: str, dry_run: bool = True) -> str:
    """Install a new node module from npm into Node-RED. Applies live.

    module: the npm package name, e.g. "node-red-node-suncalc" (check
    flows.nodered.org or npm directly for the exact name first).

    dry_run controls whether this actually installs anything:
      - True (default): just confirm the module name back to you
        WITHOUT installing. There's nothing to diff against for a new
        install, so this is mainly a chance to catch a typo'd package
        name before it runs.
      - False: actually install. This runs a real npm install on the
        Node-RED host — can take anywhere from seconds to a couple of
        minutes, and can fail for reasons outside this server's control
        (bad package name, network issue, version incompatibility).
    """
    if dry_run:
        return f"PREVIEW ONLY — nothing installed yet. Would install npm module '{module}' into Node-RED."

    success, result = _node_red_request("POST", "/nodes", json_body={"module": module})
    if not success:
        return f"Error: Node-RED rejected the install: {result}"

    return f"Installed '{module}'. Result: {json.dumps(result, ensure_ascii=False)}"


# -----------------------------------------------------------------------
# TOOL 37: set_node_red_node_enabled
# -----------------------------------------------------------------------
@mcp.tool()
def set_node_red_node_enabled(module: str, enabled: bool, dry_run: bool = True) -> str:
    """Enable or disable an installed node module (without uninstalling it).

    module: the module name (from list_node_red_nodes).
    enabled: True to enable, False to disable.

    dry_run controls whether this actually changes anything:
      - True (default): confirm what would change WITHOUT applying it.
      - False: actually apply it. Disabled node types stop appearing in
        the palette and any flow already using them will fail to
        deploy/start until re-enabled.
    """
    if dry_run:
        action = "enable" if enabled else "disable"
        return f"PREVIEW ONLY — nothing changed yet. Would {action} module '{module}'."

    success, result = _node_red_request("PUT", f"/nodes/{module}", json_body={"enabled": enabled})
    if not success:
        return f"Error: Node-RED rejected the change: {result}"

    return f"Module '{module}' {'enabled' if enabled else 'disabled'}."


# -----------------------------------------------------------------------
# TOOL 38: remove_node_red_node
#
# Uninstalls a module entirely (removes the npm package) — a step
# further than disabling. Same dry_run-gated preview pattern as every
# other delete tool in this server.
# -----------------------------------------------------------------------
@mcp.tool()
def remove_node_red_node(module: str, dry_run: bool = True) -> str:
    """Uninstall a node module from Node-RED entirely. No undo — always dry_run first.

    module: the module name (from list_node_red_nodes).

    dry_run controls whether this actually removes anything:
      - True (default): confirm which module would be removed WITHOUT
        removing it.
      - False: actually uninstall it. Any flow using a node type from
        this module will fail to deploy/start afterward.
    """
    if dry_run:
        return f"PREVIEW ONLY — nothing removed yet. Would uninstall module '{module}'."

    success, _ = _node_red_request("DELETE", f"/nodes/{module}")
    if not success:
        return f"Error: Node-RED rejected the removal."

    return f"Module '{module}' uninstalled."


# -----------------------------------------------------------------------
# TOOL 39: get_node_red_diagnostics
# -----------------------------------------------------------------------
@mcp.tool()
def get_node_red_diagnostics() -> str:
    """Get Node-RED's own runtime diagnostics report (versions, platform, stats).

    Returns Node-RED's built-in health/diagnostic snapshot as
    pretty-printed JSON — a deeper equivalent of get_server_status,
    specific to Node-RED itself rather than this MCP server.
    """
    success, data = _node_red_request("GET", "/diagnostics")
    if not success:
        return f"Error: {data}"
    return json.dumps(data, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------
# TOOL 40: get_node_red_settings
#
# Not a feature tool — a sanity-check tool. Everything else in this
# section assumes a particular Node-RED version, a particular API base
# path, and a "flows.json"-style storage model (not the optional
# git-backed "Projects" mode, which changes how flows persist). This
# reads Node-RED's own reported runtime settings so those assumptions
# can be verified rather than discovered the hard way — the same
# instinct that led to catching the /context dead-end before building
# against it.
# -----------------------------------------------------------------------
@mcp.tool()
def get_node_red_settings() -> str:
    """Get Node-RED's runtime settings (version, editor theme, safe config subset).

    Returns Node-RED's own reported settings as pretty-printed JSON —
    NOT your actual settings.js file or credentials, just the safe
    subset the Admin API exposes (version number, enabled features,
    editor configuration, etc.). Useful for confirming assumptions the
    other Node-RED tools in this server depend on: which version is
    running, whether Projects mode is enabled (changes how flows are
    stored), and the API's actual base path.
    """
    success, data = _node_red_request("GET", "/settings")
    if not success:
        return f"Error: {data}"
    return json.dumps(data, indent=2, ensure_ascii=False)


# -----------------------------------------------------------------------
# RESOURCE 1: ha://entities
#
# This is the first RESOURCE in this server, not a tool — an important
# distinction. A tool is an action Claude decides to invoke mid-conversation
# ("I should call get_entity_state right now"). A resource is more like a
# document: reference content a client can browse or load into context,
# identified by a URI instead of called with arguments.
#
# The @mcp.resource() decorator takes a URI (or URI template, for
# parameterized resources — not used here since this one takes no
# arguments) instead of relying on the function being "called" the way a
# tool is. "ha://" is just a made-up scheme name for this server, the
# same way a real webpage uses "https://".
#
# This resource reuses HA_URL/HA_TOKEN and the same auth pattern as
# get_entity_state, but calls Home Assistant's /api/states with no
# entity_id — which returns EVERY entity at once, not just one.
# -----------------------------------------------------------------------
@mcp.resource("ha://entities")
def ha_entities_resource() -> str:
    """A live list of every Home Assistant entity and its current state.

    Reference data, not a targeted lookup — use get_entity_state instead
    when you already know the specific entity_id you want.
    """
    if not HA_URL or not HA_TOKEN:
        return "Error: HA_URL or HA_TOKEN is not configured in .env."

    url = f"{HA_URL}/api/states"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return f"Error: could not reach Home Assistant at {HA_URL}."

    if response.status_code != 200:
        return f"Error: Home Assistant returned status {response.status_code}."

    entities = response.json()
    lines = [f"{e['entity_id']}: {e['state']}" for e in entities]
    return "\n".join(sorted(lines))


# -----------------------------------------------------------------------
# RESOURCE 2: ha://conventions
#
# A STATIC resource — no API call, no filesystem read, just fixed text
# baked into this file. This is where project-specific domain knowledge
# lives, so that ANY client talking to this server (not just a
# conversation where you happened to mention it) automatically has this
# context available. Update this text directly as you learn more quirks
# about your own Home Assistant setup.
# -----------------------------------------------------------------------
@mcp.resource("ha://conventions")
def ha_conventions_resource() -> str:
    """Project-specific conventions and quirks for this Home Assistant setup.

    Read this before proposing changes to templates.yaml, entity IDs, or
    dashboard YAML, so suggestions match how this particular instance is
    actually configured rather than generic Home Assistant assumptions.
    """
    return """\
Home Assistant setup conventions for this project:

- Templates are kept in a separate templates.yaml-style structure under
  templates/<domain>/ (e.g. templates/sensor/), not inline in
  configuration.yaml. Files use top-level list blocks like "- sensor:",
  "- binary_sensor:", "- trigger: / sensor:".

- LG ThinQ washer/dryer entities were migrated from the old SmartThinQ
  LGE Sensors integration to the native LG ThinQ integration. The old
  integration's entities were never removed from the registry, so new
  entities collide on name and get a "_2" suffix. Decision made: use the
  "_2"-suffixed entity IDs going forward rather than cleaning up the
  registry. Known _2 entities: switch.washer_power_2,
  sensor.washer_time_display_2, switch.dryer_power_2,
  sensor.dryer_time_display_2, binary_sensor.washer_run_completed_2,
  binary_sensor.dryer_run_completed_2.

- Known raw state values for sensor.dryer_current_status: running,
  pause, wrinkle_care, end, initial, error, power_off, cooling.

- Known raw state values for sensor.washer_current_status: rinsing,
  add_drain, frozen_prevent_pause, initial, refreshing, spinning,
  detecting, reserved, prewash, frozen_prevent_running, power_off,
  running, frozen_prevent_initial, error, detergent_amount, pause,
  drying, rinse_hold, end.

- Main dashboard is registered as a STORAGE-mode dashboard (url_path
  "dashboard-main", title "Main") — editable via get_ha_dashboard_config
  / save_ha_dashboard_config, same as "Appliances", "Map", and
  "Overview". Do not assume it's YAML-mode just because ULM is
  installed — always confirm mode with list_ha_dashboards first.

- Separately, there are TWO yaml-mode dashboard systems installed:
    - UI Lovelace Minimalist (ULM), url_paths "ui-lovelace-minimalist"
      and "adaptive-dash". The "main" folder
      (ui_lovelace_minimalist/dashboard/main/) is the PRIMARY, actively-
      used ULM location — "adaptive-dash" is a separate, secondary ULM
      instance. Do not confuse this with the storage-mode dashboard
      titled "Main" (url_path "dashboard-main") described above; they
      are unrelated despite the similar name. Root file:
      ui_lovelace_minimalist/dashboard/ui-lovelace.yaml, which pulls in
      card templates via !include_dir_merge_named from
      custom_components/ui_lovelace_minimalist/__ui_minimalist__/ulm_templates/
      (this is the ULM framework's OWN template library — part of the
      installed integration, not user dashboard content; avoid editing
      it directly since a HACS update could overwrite changes) and
      views via individual !include lines pointing at
      ui_lovelace_minimalist/dashboard/main/*.yaml and
      ui_lovelace_minimalist/dashboard/main/rooms/*.yaml (several are
      commented out — check the root file for which are currently
      active before assuming a room view exists; disabled room
      includes no longer have corresponding files in rooms/ at all,
      confirming they're genuinely retired, not just toggled off).

      main/home.yaml is the overview/landing view and itself includes
      three more files as cards: main/sidebar.yaml, main/appt.yaml,
      main/header.yaml. main/sidebar.yaml in turn includes all six
      files under main/popups/ (security.yaml, lights.yaml,
      sockets.yaml, doors.yaml, motion.yaml, battery.yaml) via
      "popup_content: !include" lines — these render as clickable
      sidebar menu items that open an overlay popup, NOT separate
      dashboard tabs. This means popups/sockets.yaml, doors.yaml,
      motion.yaml, and battery.yaml are still actively reachable
      through the sidebar even though their corresponding top-level
      VIEWS are commented out/retired in the root file — don't assume
      a commented-out view means that content is fully inaccessible.
    - Dwains Dashboard, url_path "dwains-dashboard" — structure not yet
      explored.
  Edit yaml-mode dashboards with read_file/write_file/
  search_entity_references on their actual files, never
  save_ha_dashboard_config (HA rejects that for yaml-mode dashboards).

- The root-level ui-lovelace.yaml (directly in the config folder, NOT
  inside ui_lovelace_minimalist/) is EMPTY — a harmless leftover from
  before dashboards were split out; not in active use.
"""


# -----------------------------------------------------------------------
# Entry point.
#
# transport="stdio" means this process communicates with its client
# (Claude Desktop) by reading/writing JSON-RPC messages over stdin/stdout.
# That's the standard transport for local MCP servers. There's also an
# HTTP/SSE transport for remote servers, which is a natural "next step"
# once stdio makes sense.
# -----------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
