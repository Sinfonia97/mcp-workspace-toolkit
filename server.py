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
