"""Tests for Hermes Kanban MCP Server (scripts/mcp/hermes_kanban_mcp.py)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.mcp.hermes_kanban_mcp import (
    DEFAULT_BOARD,
    execute_hermes_kanban,
    get_hermes_bin,
    kanban_block,
    kanban_boards,
    kanban_comment,
    kanban_complete,
    kanban_create,
    kanban_diagnostics,
    kanban_link,
    kanban_list,
    kanban_request_review,
    kanban_show,
    kanban_stats,
    kanban_unblock,
    kanban_unlink,
    run_hermes_json,
)


def test_get_hermes_bin():
    """Verify hermes binary resolution."""
    with patch("os.environ.get", return_value="/custom/bin/hermes"), patch(
        "os.path.exists", return_value=True
    ):
        assert get_hermes_bin() == "/custom/bin/hermes"

    with patch("os.environ.get", return_value=None), patch(
        "os.path.exists", side_effect=lambda p: p.endswith(".local/bin/hermes")
    ):
        assert get_hermes_bin().endswith(".local/bin/hermes")

    with patch("os.environ.get", return_value=None), patch(
        "os.path.exists", return_value=False
    ), patch("shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError):
            get_hermes_bin()


def test_execute_hermes_kanban_success():
    """Verify command assembly and execution."""
    mock_run = MagicMock()
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "Success output\n"
    mock_run.return_value.stderr = ""

    with patch("scripts.mcp.hermes_kanban_mcp.get_hermes_bin", return_value="/bin/hermes"), patch(
        "subprocess.run", mock_run
    ):
        res = execute_hermes_kanban(["stats"], board="test-board")

    assert res["ok"] is True
    assert res["stdout"] == "Success output"
    mock_run.assert_called_once()
    called_cmd = mock_run.call_args[0][0]
    assert called_cmd == ["/bin/hermes", "kanban", "--board", "test-board", "stats"]


def test_execute_hermes_kanban_failure():
    """Verify failure handling on non-zero exit code."""
    mock_run = MagicMock()
    mock_run.return_value.returncode = 1
    mock_run.return_value.stdout = ""
    mock_run.return_value.stderr = "Task not found\n"

    with patch("scripts.mcp.hermes_kanban_mcp.get_hermes_bin", return_value="/bin/hermes"), patch(
        "subprocess.run", mock_run
    ):
        res = execute_hermes_kanban(["show", "invalid_id"])

    assert res["ok"] is False
    assert res["exit_code"] == 1
    assert "Task not found" in res["error"]


def test_run_hermes_json_parsing():
    """Verify JSON parsing helper."""
    with patch(
        "scripts.mcp.hermes_kanban_mcp.execute_hermes_kanban",
        return_value={"ok": True, "stdout": json.dumps([{"id": "t_1", "status": "todo"}])},
    ):
        res = run_hermes_json(["list"])
        assert res["ok"] is True
        assert res["data"] == [{"id": "t_1", "status": "todo"}]


def test_kanban_list_tool():
    """Verify kanban_list argument construction and limit slicing."""
    fake_tasks = [{"id": f"t_{i}"} for i in range(10)]
    with patch(
        "scripts.mcp.hermes_kanban_mcp.run_hermes_json",
        return_value={"ok": True, "data": fake_tasks},
    ) as mock_json:
        res = kanban_list(status="ready", assignee="homelab-build", archived=True, sort="priority", limit=3)
        assert res["ok"] is True
        assert len(res["tasks"]) == 3
        mock_json.assert_called_once()
        args = mock_json.call_args[0][0]
        assert "--status" in args and "ready" in args
        assert "--assignee" in args and "homelab-build" in args
        assert "--archived" in args
        assert "--sort" in args and "priority" in args


def test_kanban_show_tool():
    """Verify kanban_show."""
    with patch(
        "scripts.mcp.hermes_kanban_mcp.run_hermes_json",
        return_value={"ok": True, "data": {"id": "t_100", "title": "Test Task"}},
    ) as mock_json:
        res = kanban_show("t_100", board="custom-board")
        assert res["ok"] is True
        assert res["data"]["id"] == "t_100"
        mock_json.assert_called_once_with(["show", "t_100"], board="custom-board")


def test_kanban_create_tool():
    """Verify kanban_create parameter flags."""
    with patch(
        "scripts.mcp.hermes_kanban_mcp.run_hermes_json",
        return_value={"ok": True, "data": {"id": "t_new"}},
    ) as mock_json:
        res = kanban_create(
            title="Implement feature",
            body="Task body details",
            assignee="homelab-build",
            priority=10,
            triage=True,
            workspace="worktree",
            branch="task/feature",
            initial_status="blocked",
            model="gemini-3.8-flash",
            provider="google",
        )
        assert res["ok"] is True
        mock_json.assert_called_once()
        args = mock_json.call_args[0][0]
        assert args[0] == "create"
        assert "--body" in args and "Task body details" in args
        assert "--assignee" in args and "homelab-build" in args
        assert "--priority" in args and "10" in args
        assert "--triage" in args
        assert "--workspace" in args and "worktree" in args
        assert "--branch" in args and "task/feature" in args
        assert "--initial-status" in args and "blocked" in args
        assert "--model" in args and "gemini-3.8-flash" in args
        assert "--provider" in args and "google" in args
        assert args[-1] == "Implement feature"


def test_kanban_lifecycle_tools():
    """Verify comment, block, unblock, request_review, complete, link, unlink."""
    with patch(
        "scripts.mcp.hermes_kanban_mcp.execute_hermes_kanban",
        return_value={"ok": True, "stdout": "Action performed"},
    ) as mock_exec:
        # comment
        res = kanban_comment("t_1", "Looking good", author="tester")
        assert res["ok"] is True
        args = mock_exec.call_args[0][0]
        assert args == ["comment", "--author", "tester", "t_1", "Looking good"]

        # block
        res = kanban_block("t_1", "Waiting on OTP", kind="needs_input")
        assert res["ok"] is True
        args = mock_exec.call_args[0][0]
        assert args == ["block", "--kind", "needs_input", "t_1", "Waiting on OTP"]

        # unblock
        res = kanban_unblock("t_1", reason="OTP resolved")
        assert res["ok"] is True
        args = mock_exec.call_args[0][0]
        assert args == ["unblock", "--reason", "OTP resolved", "t_1"]

        # request_review
        res = kanban_request_review("t_1", summary="All tests pass", reviewer="homelab-review", force=True)
        assert res["ok"] is True
        args = mock_exec.call_args[0][0]
        assert args == [
            "request-review",
            "--summary",
            "All tests pass",
            "--reviewer",
            "homelab-review",
            "--force",
            "t_1",
        ]

        # complete
        res = kanban_complete("t_1", result="PASS", summary="Finished cleanly", force=True)
        assert res["ok"] is True
        args = mock_exec.call_args[0][0]
        assert args == [
            "complete",
            "--result",
            "PASS",
            "--summary",
            "Finished cleanly",
            "--force",
            "t_1",
        ]

        # link & unlink
        res = kanban_link("t_parent", "t_child")
        assert res["ok"] is True
        assert mock_exec.call_args[0][0] == ["link", "t_parent", "t_child"]

        res = kanban_unlink("t_parent", "t_child")
        assert res["ok"] is True
        assert mock_exec.call_args[0][0] == ["unlink", "t_parent", "t_child"]


def test_kanban_stats_and_diagnostics():
    """Verify stats and diagnostics tools."""
    with patch(
        "scripts.mcp.hermes_kanban_mcp.execute_hermes_kanban",
        return_value={"ok": True, "stdout": "By status:\n  done 20"},
    ):
        res = kanban_stats()
        assert res["ok"] is True
        assert "done 20" in res["stats"]

    with patch(
        "scripts.mcp.hermes_kanban_mcp.run_hermes_json",
        return_value={"ok": True, "data": []},
    ) as mock_json:
        res = kanban_diagnostics(severity="warning", task_id="t_1")
        assert res["ok"] is True
        args = mock_json.call_args[0][0]
        assert "--severity" in args and "warning" in args
        assert "--task" in args and "t_1" in args


@pytest.mark.skipif(
    not get_hermes_bin() if (lambda: True)() else True,
    reason="Only run live smoke test if hermes CLI is available",
)
def test_live_smoke():
    """Smoke test against live hermes CLI read-only operations."""
    res_boards = kanban_boards()
    assert res_boards["ok"] is True
    assert "investment-homelab" in res_boards.get("boards", "")

    res_stats = kanban_stats()
    assert res_stats["ok"] is True
    assert "status:" in res_stats.get("stats", "").lower()
