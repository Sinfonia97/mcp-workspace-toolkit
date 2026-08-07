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
