#!/usr/bin/env python3
"""Hermes Kanban MCP Server.

Provides a structured Model Context Protocol (MCP) toolset interface
over the Hermes Kanban CLI (`hermes kanban`) for task orchestration.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

logger = logging.getLogger("hermes-kanban-mcp")

DEFAULT_BOARD = os.environ.get("HERMES_KANBAN_BOARD", "investment-homelab")


def get_hermes_bin() -> str:
    """Resolve the path to the hermes CLI executable."""
    candidate = os.environ.get("HERMES_BIN")
    if candidate and os.path.exists(candidate):
        return candidate

    standard_path = os.path.expanduser("~/.local/bin/hermes")
    if os.path.exists(standard_path):
        return standard_path

    which_path = shutil.which("hermes")
    if which_path:
        return which_path

    raise FileNotFoundError(
        "Could not find 'hermes' executable. Please ensure it is installed in ~/.local/bin/hermes or on PATH."
    )


def execute_hermes_kanban(
    args: List[str], board: Optional[str] = None
) -> Dict[str, Any]:
    """Execute a hermes kanban command and return structured outcome."""
    target_board = board or DEFAULT_BOARD
    try:
        bin_path = get_hermes_bin()
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}

    cmd = [bin_path, "kanban"]
    if target_board:
        cmd.extend(["--board", target_board])
    cmd.extend(args)

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception as e:
        return {"ok": False, "error": f"Subprocess execution failed: {e}", "cmd": cmd}

    stdout = res.stdout.strip()
    stderr = res.stderr.strip()

    if res.returncode != 0:
        return {
            "ok": False,
            "exit_code": res.returncode,
            "error": stderr or stdout or "Command failed with non-zero exit code",
            "cmd": cmd,
        }

    return {"ok": True, "stdout": stdout, "stderr": stderr}


def run_hermes_json(
    args: List[str], board: Optional[str] = None
) -> Dict[str, Any]:
    """Execute hermes kanban command expecting JSON output."""
    full_args = list(args)
    if "--json" not in full_args:
        full_args.append("--json")

    outcome = execute_hermes_kanban(full_args, board=board)
    if not outcome["ok"]:
        return outcome

    raw = outcome.get("stdout", "")
    if not raw:
        return {"ok": True, "data": None}

    try:
        data = json.loads(raw)
        return {"ok": True, "data": data}
    except json.JSONDecodeError:
        return {"ok": True, "data": raw}


mcp = MCPServer(name="hermes-kanban")


@mcp.tool()
def kanban_boards() -> Dict[str, Any]:
    """List all available Hermes Kanban boards."""
    outcome = execute_hermes_kanban(["boards", "list"], board="")
    if not outcome["ok"]:
        return outcome
    return {"ok": True, "boards": outcome.get("stdout", "")}


@mcp.tool()
def kanban_list(
    board: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    archived: bool = False,
    sort: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """List tasks on the specified Hermes Kanban board.

    Args:
        board: Board slug (defaults to HERMES_KANBAN_BOARD or 'investment-homelab')
        status: Filter by status ('triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review', 'done', 'archived')
        assignee: Filter by assignee profile name (e.g. 'default', 'homelab-build', 'homelab-review')
        archived: Include archived tasks
        sort: Sort order ('priority', 'created', 'created-desc', 'status', 'title', 'updated', etc.)
        limit: Max number of tasks to return (default 50)
    """
    args = ["list"]
    if status:
        args.extend(["--status", status])
    if assignee:
        args.extend(["--assignee", assignee])
    if archived:
        args.append("--archived")
    if sort:
        args.extend(["--sort", sort])

    res = run_hermes_json(args, board=board)
    if not res["ok"]:
        return res

    data = res.get("data")
    if isinstance(data, list) and limit > 0:
        data = data[:limit]
    return {"ok": True, "tasks": data}


@mcp.tool()
def kanban_show(task_id: str, board: Optional[str] = None) -> Dict[str, Any]:
    """Show details, comments, and run history for a specific task.

    Args:
        task_id: Task identifier (e.g. 't_07f334f1')
        board: Board slug (defaults to 'investment-homelab')
    """
    return run_hermes_json(["show", task_id], board=board)


@mcp.tool()
def kanban_create(
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    board: Optional[str] = None,
    priority: Optional[int] = None,
    triage: bool = False,
    workspace: Optional[str] = None,
    branch: Optional[str] = None,
    initial_status: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new task on the Kanban board.

    Args:
        title: Task title
        body: Opening task specification or markdown body
        assignee: Profile name to assign (e.g. 'homelab-build', 'homelab-review', 'default')
        board: Board slug (defaults to 'investment-homelab')
        priority: Priority tiebreaker integer
        triage: If True, place in triage column for refinement
        workspace: Workspace kind ('scratch', 'worktree', etc.)
        branch: Git branch name for worktree tasks
        initial_status: Initial status ('blocked' or 'running')
        model: Model override (e.g. 'gemini-3.8-flash', 'gemini-3.1-pro')
        provider: Provider override
    """
    args = ["create"]
    if body:
        args.extend(["--body", body])
    if assignee:
        args.extend(["--assignee", assignee])
    if priority is not None:
        args.extend(["--priority", str(priority)])
    if triage:
        args.append("--triage")
    if workspace:
        args.extend(["--workspace", workspace])
    if branch:
        args.extend(["--branch", branch])
    if initial_status:
        args.extend(["--initial-status", initial_status])
    if model:
        args.extend(["--model", model])
    if provider:
        args.extend(["--provider", provider])

    args.append(title)
    return run_hermes_json(args, board=board)


@mcp.tool()
def kanban_comment(
    task_id: str,
    comment: str,
    board: Optional[str] = None,
    author: Optional[str] = None,
) -> Dict[str, Any]:
    """Append a comment to a Kanban task.

    Args:
        task_id: Task identifier
        comment: Comment body text
        board: Board slug (defaults to 'investment-homelab')
        author: Optional author name override
    """
    args = ["comment"]
    if author:
        args.extend(["--author", author])
    args.extend([task_id, comment])
    outcome = execute_hermes_kanban(args, board=board)
    if not outcome["ok"]:
        return outcome
    return {"ok": True, "message": outcome.get("stdout", "Comment added successfully")}


@mcp.tool()
def kanban_block(
    task_id: str,
    reason: str,
    kind: Optional[str] = None,
    board: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark a task as blocked with a reason.

    Args:
        task_id: Task identifier
        reason: Reason for blocking (also recorded as a comment)
        kind: Optional block type ('capability', 'dependency', 'needs_input', 'transient')
        board: Board slug (defaults to 'investment-homelab')
    """
    args = ["block"]
    if kind:
        args.extend(["--kind", kind])
    args.extend([task_id, reason])
    outcome = execute_hermes_kanban(args, board=board)
    if not outcome["ok"]:
        return outcome
    return {"ok": True, "message": outcome.get("stdout", "Task marked as blocked")}


@mcp.tool()
def kanban_unblock(
    task_id: str,
    reason: Optional[str] = None,
    board: Optional[str] = None,
) -> Dict[str, Any]:
    """Unblock a blocked or scheduled task, returning it to ready/todo.

    Args:
        task_id: Task identifier
        reason: Optional note explaining why the task was unblocked
        board: Board slug (defaults to 'investment-homelab')
    """
    args = ["unblock"]
    if reason:
        args.extend(["--reason", reason])
    args.append(task_id)
    outcome = execute_hermes_kanban(args, board=board)
    if not outcome["ok"]:
        return outcome
    return {"ok": True, "message": outcome.get("stdout", "Task unblocked")}


@mcp.tool()
def kanban_request_review(
    task_id: str,
    summary: Optional[str] = None,
    reviewer: Optional[str] = None,
    force: bool = False,
    board: Optional[str] = None,
) -> Dict[str, Any]:
    """Move a task to 'review' status (implementation complete, awaiting review).

    Args:
        task_id: Task identifier
        summary: What was implemented and how it was verified
        reviewer: Reviewer profile (e.g. 'homelab-review')
        force: Override claim guard if running
        board: Board slug (defaults to 'investment-homelab')
    """
    args = ["request-review"]
    if summary:
        args.extend(["--summary", summary])
    if reviewer:
        args.extend(["--reviewer", reviewer])
    if force:
        args.append("--force")
    args.append(task_id)
    outcome = execute_hermes_kanban(args, board=board)
    if not outcome["ok"]:
        return outcome
    return {"ok": True, "message": outcome.get("stdout", "Review requested")}


@mcp.tool()
def kanban_complete(
    task_id: str,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    force: bool = False,
    board: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark a task as completed (done).

    Args:
        task_id: Task identifier
        result: Result summary
        summary: Structured handoff summary
        force: Override claim guard if running
        board: Board slug (defaults to 'investment-homelab')
    """
    args = ["complete"]
    if result:
        args.extend(["--result", result])
    if summary:
        args.extend(["--summary", summary])
    if force:
        args.append("--force")
    args.append(task_id)
    outcome = execute_hermes_kanban(args, board=board)
    if not outcome["ok"]:
        return outcome
    return {"ok": True, "message": outcome.get("stdout", "Task completed")}


@mcp.tool()
def kanban_link(
    parent_id: str, child_id: str, board: Optional[str] = None
) -> Dict[str, Any]:
    """Add a parent->child dependency between two tasks.

    Args:
        parent_id: Parent task ID that must finish first
        child_id: Child task ID that depends on the parent
        board: Board slug (defaults to 'investment-homelab')
    """
    args = ["link", parent_id, child_id]
    outcome = execute_hermes_kanban(args, board=board)
    if not outcome["ok"]:
        return outcome
    return {"ok": True, "message": outcome.get("stdout", "Dependency linked")}


@mcp.tool()
def kanban_unlink(
    parent_id: str, child_id: str, board: Optional[str] = None
) -> Dict[str, Any]:
    """Remove a parent->child dependency between two tasks.

    Args:
        parent_id: Parent task ID
        child_id: Child task ID
        board: Board slug (defaults to 'investment-homelab')
    """
    args = ["unlink", parent_id, child_id]
    outcome = execute_hermes_kanban(args, board=board)
    if not outcome["ok"]:
        return outcome
    return {"ok": True, "message": outcome.get("stdout", "Dependency unlinked")}


@mcp.tool()
def kanban_stats(board: Optional[str] = None) -> Dict[str, Any]:
    """Show task status and assignee distribution statistics for the board.

    Args:
        board: Board slug (defaults to 'investment-homelab')
    """
    outcome = execute_hermes_kanban(["stats"], board=board)
    if not outcome["ok"]:
        return outcome
    return {"ok": True, "stats": outcome.get("stdout", "")}


@mcp.tool()
def kanban_diagnostics(
    severity: Optional[str] = None,
    task_id: Optional[str] = None,
    board: Optional[str] = None,
) -> Dict[str, Any]:
    """List active board diagnostics or warnings.

    Args:
        severity: Filter by severity ('warning', 'error', 'critical')
        task_id: Filter by specific task ID
        board: Board slug (defaults to 'investment-homelab')
    """
    args = ["diagnostics"]
    if severity:
        args.extend(["--severity", severity])
    if task_id:
        args.extend(["--task", task_id])
    return run_hermes_json(args, board=board)


def main() -> None:
    """Run the FastMCP server over stdio."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
