# -*- coding: utf-8 -*-
"""CDP broker session daemon launcher (unified startup / attach / restart).

Single control surface to ensure CDP broker daemons (:9333/:9334) are running
without duplicate logins, credential prompts, or SSO session decay.
"""
import asyncio
import contextvars
import fcntl
import inspect
import logging
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Set

from app.services.broker_cdp.login_progress import (
    clear_login_progress,
    get_login_progress,
    is_login_active,
    set_login_progress,
)
from app.services.broker_cdp.session_probe import cdp_session_status, get_cdp_daemon_pid
from scripts.broker_cdp.client import BROKER_CDP_PORTS

logger = logging.getLogger("broker_cdp_daemon_launcher")

REPO_ROOT = Path(__file__).resolve().parents[3]

BROKER_PORTS = {
    "kss": 9333,
    "krungsri": 9333,
    "liberator": 9334,
    "lib": 9334,
}

VALID_STATES = {
    "AUTHENTICATED",
    "LOGIN_PAGE",
    "OTP_PAGE",
    "NO_TARGET_TAB",
    "DAEMON_DOWN",
    "UNKNOWN",
}

_ACTIVE_LOCKS: contextvars.ContextVar[Optional[Set[str]]] = contextvars.ContextVar("_ACTIVE_LOCKS", default=None)
_ACTIVE_LOGIN_TASKS: Dict[str, asyncio.Task] = {}
_LAST_SPAWN_TS: Dict[str, float] = {}
_GUI_ENV_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_RUNTIME_DIR")


def _resolve_gui_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Resolve GUI session variables (DISPLAY/WAYLAND_DISPLAY/XAUTHORITY) for spawned Chrome.

    Systemd user services started at boot often miss dynamic Xwayland XAUTHORITY
    files created later by GNOME/Mutter; query systemd user env and /run/user/<uid>
    so Chrome can attach to the active desktop session without manual service restarts.
    """
    env = dict(base_env if base_env is not None else os.environ)

    def _xauth_valid(path: Optional[str]) -> bool:
        return bool(path and os.path.exists(path))

    if not _xauth_valid(env.get("XAUTHORITY")) or not env.get("WAYLAND_DISPLAY"):
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    key, sep, val = line.partition("=")
                    if sep and key in _GUI_ENV_KEYS and val:
                        if key == "XAUTHORITY":
                            if not _xauth_valid(env.get("XAUTHORITY")) and os.path.exists(val):
                                env[key] = val
                        elif not env.get(key):
                            env[key] = val
        except (OSError, subprocess.SubprocessError):
            pass

    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if os.path.isdir(runtime_dir):
        env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
        if not env.get("WAYLAND_DISPLAY") and os.path.exists(os.path.join(runtime_dir, "wayland-0")):
            env["WAYLAND_DISPLAY"] = "wayland-0"
        if not _xauth_valid(env.get("XAUTHORITY")):
            try:
                candidates = sorted(
                    Path(runtime_dir).glob(".mutter-Xwaylandauth.*"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if candidates:
                    env["XAUTHORITY"] = str(candidates[0])
            except OSError:
                pass

    if not _xauth_valid(env.get("XAUTHORITY")):
        home_xauth = os.path.expanduser("~/.Xauthority")
        if os.path.exists(home_xauth):
            env["XAUTHORITY"] = home_xauth

    env["DISPLAY"] = env.get("DISPLAY") or ":0"
    if env.get("WAYLAND_DISPLAY") and not env.get("XDG_SESSION_TYPE"):
        env["XDG_SESSION_TYPE"] = "wayland"
    return env


def _is_pytest_unmocked_cdp(connect_cdp_fn: Any) -> bool:
    """True when inside pytest and connect_cdp has not been mocked (guards live :9333/:9334)."""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if hasattr(connect_cdp_fn, "_mock_name") or hasattr(connect_cdp_fn, "assert_called"):
        return False
    return getattr(connect_cdp_fn, "__module__", "") == "scripts.broker_cdp.client"


def normalize_broker_slug(broker: str) -> str:
    """Map external/legacy broker slugs to canonical daemon names ('kss' | 'liberator')."""
    b = (broker or "").strip().lower()
    if b in ("lib", "liberator"):
        return "liberator"
    if b in ("kss", "krungsri"):
        return "kss"
    raise ValueError(f"Unknown broker slug '{broker}'. Expected 'kss' or 'liberator'")


def get_broker_port(broker: str) -> int:
    """Resolve CDP debugging port for broker."""
    b = (broker or "").strip().lower()
    if b in BROKER_PORTS:
        return BROKER_PORTS[b]
    return BROKER_CDP_PORTS.get(b, 0)


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Check whether a local TCP port is actively accepting connections."""
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError, socket.timeout):
        return False


def get_daemon_pid(broker: str) -> Optional[int]:
    """Find PID of the python session_daemon process for broker."""
    norm = normalize_broker_slug(broker)
    pid_file = REPO_ROOT / "data" / f"daemon-{norm}.pid"
    if pid_file.exists():
        try:
            with open(str(pid_file), "r", encoding="utf-8") as f:
                candidate = int(f.read().strip())
            with open(f"/proc/{candidate}/cmdline", "rb") as fh:
                cmdline = fh.read().decode(errors="replace")
            if "session_daemon.py" in cmdline and norm in cmdline:
                return candidate
        except (OSError, ValueError):
            pass

    # Fallback scan /proc
    try:
        proc_dir = "/proc"
        if os.path.exists(proc_dir):
            for entry in os.listdir(proc_dir):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as fh:
                        raw = fh.read().decode(errors="replace")
                    if "session_daemon.py" in raw and norm in raw:
                        return pid
                except (OSError, IOError):
                    continue
    except Exception:
        pass
    return None


def get_pid_holding_port(port: int) -> Optional[int]:
    """Find any process PID listening on the given port (ss / lsof fallback)."""
    if not port:
        return None
    try:
        out = subprocess.check_output(
            ["ss", "-ltnp", f"sport = :{port}"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        match = re.search(r"pid=(\d+)", out)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["lsof", "-t", f"-i:{port}"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        pids = [int(p) for p in out.strip().split() if p.isdigit()]
        if pids:
            return pids[0]
    except Exception:
        pass
    return None


def list_daemon_pids(broker: str) -> list[int]:
    """Return all live `session_daemon.py <norm>` PIDs from pidfile and `/proc`."""
    norm = normalize_broker_slug(broker)
    seen: list[int] = []
    primary = get_daemon_pid(norm)
    if primary:
        seen.append(primary)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return seen
    try:
        proc_dir = "/proc"
        if os.path.exists(proc_dir):
            for entry in os.listdir(proc_dir):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                if pid in seen:
                    continue
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as fh:
                        raw = fh.read().decode(errors="replace")
                    if "session_daemon.py" in raw and norm in raw:
                        seen.append(pid)
                except (OSError, IOError):
                    continue
    except Exception:
        pass
    return seen


def get_log_tail(broker: str, max_lines: int = 5) -> str:
    """Read the last N lines from the broker daemon log file (bounded read)."""
    try:
        norm = normalize_broker_slug(broker)
        log_file = REPO_ROOT / "data" / "logs" / f"daemon-{norm}.log"
        if not log_file.exists():
            return ""
        with open(log_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            buffer_size = min(size, 8192)
            f.seek(max(0, size - buffer_size))
            tail_bytes = f.read()
        lines = tail_bytes.decode(errors="replace").splitlines(keepends=True)
        return "".join(lines[-max_lines:])
    except Exception:
        return ""


class _DaemonFileLock:
    """Cross-process mutex via fcntl.flock on data/locks/daemon-<name>.lock."""

    def __init__(self, broker: str, timeout: float = 15.0, *, lock_name: Optional[str] = None):
        self.broker = broker
        self.timeout = timeout
        self.file_obj = None
        self.is_reentrant = False
        self.norm = normalize_broker_slug(broker)
        self.lock_key = (lock_name or self.norm).strip() or self.norm

    def _acquire(self):
        lock_dir = REPO_ROOT / "data" / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"daemon-{self.lock_key}.lock"
        self.file_obj = open(lock_path, "a+")
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(self.file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (BlockingIOError, OSError):
                if time.monotonic() - start > self.timeout:
                    raise TimeoutError(
                        f"Could not acquire daemon lock for {self.lock_key} within {self.timeout}s"
                    )
                time.sleep(0.1)

    def _release(self):
        if self.file_obj:
            try:
                fcntl.flock(self.file_obj.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self.file_obj.close()
            except OSError:
                pass
            self.file_obj = None

    async def __aenter__(self):
        active = _ACTIVE_LOCKS.get()
        if active is None:
            active = set()
            _ACTIVE_LOCKS.set(active)
        if self.lock_key in active:
            self.is_reentrant = True
            return self

        await asyncio.to_thread(self._acquire)
        active.add(self.lock_key)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.is_reentrant:
            return
        active = _ACTIVE_LOCKS.get()
        if active and self.lock_key in active:
            active.discard(self.lock_key)
        await asyncio.to_thread(self._release)


async def daemon_state(broker: str) -> Dict[str, Any]:
    """Inspect live daemon state for broker without modifying state or launching processes.

    Returns:
        {"state": str, "port": int, "page_url": Optional[str], "pid": Optional[int]}
    """
    try:
        norm = normalize_broker_slug(broker)
    except ValueError:
        return {
            "state": "UNKNOWN",
            "port": 0,
            "page_url": None,
            "pid": None,
        }

    port = get_broker_port(norm)
    tcp_open = await asyncio.to_thread(is_port_open, port)
    if not tcp_open:
        return {
            "state": "DAEMON_DOWN",
            "port": port,
            "page_url": None,
            "pid": None,
        }

    try:
        probe_info = await cdp_session_status(norm)
    except Exception as e:
        logger.warning(f"cdp_session_status failed for {norm}: {e}")
        probe_info = {"state": "UNKNOWN", "url": None, "pid": None}

    raw_state = probe_info.get("state", "UNKNOWN")
    page_url = probe_info.get("url")

    daemon_pid = await asyncio.to_thread(get_daemon_pid, norm)
    chrome_pid = probe_info.get("pid")
    if not chrome_pid:
        chrome_pid = await asyncio.to_thread(get_cdp_daemon_pid, port)
    pid = daemon_pid or chrome_pid

    # Distinguish OTP_PAGE from generic LOGIN_PAGE
    if raw_state == "LOGIN_PAGE":
        url_lower = (page_url or "").lower()
        if any(marker in url_lower for marker in ("/otp", "/twofa")):
            state = "OTP_PAGE"
        else:
            state = "LOGIN_PAGE"
    else:
        state = raw_state

    if state not in VALID_STATES:
        state = "UNKNOWN"

    return {
        "state": state,
        "port": port,
        "page_url": page_url,
        "pid": pid,
    }


async def ensure_daemon(broker: str, wait_s: float = 90.0) -> Dict[str, Any]:
    """Ensure the CDP daemon is ready without launching duplicate browsers.

    1. If port is already open and AUTHENTICATED -> status='already_up'.
    2. If port is open but not AUTHENTICATED -> fail-closed (return current state, do not spawn).
    3. If port is closed -> spawn daemon with start_new_session=True, poll until ready or timeout.

    Returns:
        {"status": str, "state": str, "port": int, "pid": Optional[int], "page_url": Optional[str], "log_tail": str}
    """
    norm = normalize_broker_slug(broker)
    port = get_broker_port(norm)

    async with _DaemonFileLock(norm):
        current = await daemon_state(norm)

        if current["state"] == "AUTHENTICATED":
            return {
                "status": "already_up",
                "state": "AUTHENTICATED",
                "port": port,
                "pid": current["pid"],
                "page_url": current["page_url"],
                "log_tail": await asyncio.to_thread(get_log_tail, norm),
            }

        if current["state"] != "DAEMON_DOWN":
            # Port is open but tab is in non-authenticated state (LOGIN_PAGE, OTP_PAGE, NO_TARGET_TAB, UNKNOWN)
            # Fail-closed: Never spawn a second daemon over an active port.
            return {
                "status": current["state"],
                "state": current["state"],
                "port": port,
                "pid": current["pid"],
                "page_url": current["page_url"],
                "log_tail": await asyncio.to_thread(get_log_tail, norm),
            }

        # If current state is DAEMON_DOWN, check if port is actually open (e.g. probe attach failed / timeout)
        # Fail-closed per contract C7: NEVER spawn if TCP port is active!
        if await asyncio.to_thread(is_port_open, port):
            return {
                "status": "DAEMON_DOWN",
                "state": "DAEMON_DOWN",
                "port": port,
                "pid": current["pid"],
                "page_url": current["page_url"],
                "log_tail": await asyncio.to_thread(get_log_tail, norm),
            }

        # Port is closed (DAEMON_DOWN and port not open) -> clear any zombie session_daemon first
        await _kill_orphan_daemons(norm)
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            set_login_progress(norm, "launching_chrome", f"กำลังเปิด {norm.upper()} Chrome...")

        log_dir = REPO_ROOT / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"daemon-{norm}.log"
        pid_file = REPO_ROOT / "data" / f"daemon-{norm}.pid"

        env = await asyncio.to_thread(_resolve_gui_env)

        log_fp = open(log_file, "a", encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "scripts/broker_cdp/session_daemon.py",
                norm,
                "300",
                cwd=str(REPO_ROOT),
                env=env,
                stdout=log_fp,
                stderr=log_fp,
                start_new_session=True,
            )
        finally:
            log_fp.close()

        try:
            with open(pid_file, "w", encoding="utf-8") as f:
                f.write(str(proc.pid))
        except OSError as e:
            logger.warning(f"Failed to write pidfile for {norm}: {e}")
        _LAST_SPAWN_TS[norm] = time.monotonic()

        # Poll until AUTHENTICATED or early exit on login/otp or timeout
        deadline = time.monotonic() + max(0.01, float(wait_s))
        poll_interval = min(1.0, max(0.01, float(wait_s) / 2))

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)

            st = await daemon_state(norm)
            if st["state"] == "AUTHENTICATED":
                return {
                    "status": "spawned",
                    "state": "AUTHENTICATED",
                    "port": port,
                    "pid": proc.pid,
                    "page_url": st["page_url"],
                    "log_tail": await asyncio.to_thread(get_log_tail, norm),
                }

            # Return early if interaction is required
            if st["state"] in ("OTP_PAGE", "LOGIN_PAGE"):
                return {
                    "status": st["state"],
                    "state": st["state"],
                    "port": port,
                    "pid": proc.pid,
                    "page_url": st["page_url"],
                    "log_tail": await asyncio.to_thread(get_log_tail, norm),
                }

            # Check if subprocess died
            if proc.returncode is not None:
                return {
                    "status": "crashed",
                    "state": "DAEMON_DOWN",
                    "port": port,
                    "pid": proc.pid,
                    "page_url": None,
                    "log_tail": await asyncio.to_thread(get_log_tail, norm),
                }

        # Timeout reached
        final_st = await daemon_state(norm)
        return {
            "status": "timeout",
            "state": final_st.get("state", "UNKNOWN"),
            "port": port,
            "pid": proc.pid,
            "page_url": final_st.get("page_url"),
            "log_tail": await asyncio.to_thread(get_log_tail, norm),
        }


async def _terminate_daemon_pid(daemon_pid: int) -> None:
    """Terminate a single session_daemon PID (process group when leader) with SIGTERM -> SIGKILL."""
    try:
        pgid = os.getpgid(daemon_pid)
        if pgid == daemon_pid:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(daemon_pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(daemon_pid, signal.SIGTERM)
        except OSError:
            pass

    for _ in range(20):
        await asyncio.sleep(0.5)
        try:
            os.kill(daemon_pid, 0)
        except OSError:
            return

    try:
        pgid = os.getpgid(daemon_pid)
        if pgid == daemon_pid:
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(daemon_pid, signal.SIGKILL)
    except OSError:
        pass


async def _kill_orphan_daemons(norm: str) -> None:
    """Clean up any lingering session_daemon.py processes before spawning a fresh one."""
    for d_pid in list_daemon_pids(norm):
        await _terminate_daemon_pid(d_pid)


async def _kill_daemon_and_chrome(norm: str, port: int) -> None:
    """Execute contract C6 shutdown order: daemon first -> Chrome -> port close wait -> unlink pidfile."""
    daemon_pids = await asyncio.to_thread(list_daemon_pids, norm)
    chrome_pid = await asyncio.to_thread(get_cdp_daemon_pid, port)
    if not chrome_pid:
        chrome_pid = await asyncio.to_thread(get_pid_holding_port, port)

    # 1. Kill all matching daemon python processes first
    for d_pid in daemon_pids:
        await _terminate_daemon_pid(d_pid)

    # 2. Kill Chrome if still listening
    if not chrome_pid:
        chrome_pid = await asyncio.to_thread(get_cdp_daemon_pid, port)
        if not chrome_pid:
            chrome_pid = await asyncio.to_thread(get_pid_holding_port, port)

    if chrome_pid:
        try:
            os.kill(chrome_pid, signal.SIGTERM)
        except OSError:
            pass

        for _ in range(6):
            await asyncio.sleep(0.5)
            try:
                os.kill(chrome_pid, 0)
            except OSError:
                chrome_pid = None
                break

        if chrome_pid:
            try:
                os.kill(chrome_pid, signal.SIGKILL)
            except OSError:
                pass

    # 3. Wait until port is closed
    for _ in range(10):
        if not await asyncio.to_thread(is_port_open, port):
            break
        await asyncio.sleep(0.5)

    # 4. Remove stale pidfile & login progress
    pid_file = REPO_ROOT / "data" / f"daemon-{norm}.pid"
    if pid_file.exists():
        try:
            pid_file.unlink()
        except OSError:
            pass
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        clear_login_progress(norm)


async def restart_daemon(broker: str, wait_s: int = 90) -> Dict[str, Any]:
    """Safely kill existing daemon and Chrome instances holding the port, then ensure daemon.

    Kill order per contract C6:
    1. Kill daemon first via SIGTERM -> wait up to 10s -> SIGKILL (prevents keepalive loop from surviving).
    2. If port is still held by Chrome, send SIGTERM -> wait -> SIGKILL to Chrome.
    3. Clear pidfile, wait until port is closed, then call ensure_daemon.
    """
    norm = normalize_broker_slug(broker)
    port = get_broker_port(norm)

    async with _DaemonFileLock(norm):
        await _kill_daemon_and_chrome(norm, port)
        return await ensure_daemon(norm, wait_s=wait_s)


async def stop_daemon(broker: str) -> Dict[str, Any]:
    """Stop systemd user unit (if present) and cleanly terminate CDP daemon + Chrome for broker."""
    norm = normalize_broker_slug(broker)
    port = get_broker_port(norm)
    unit_name = "homelab-kss-cdp.service" if norm == "kss" else "homelab-lib-cdp.service"

    async with _DaemonFileLock(norm):
        active_task = _ACTIVE_LOGIN_TASKS.pop(norm, None)
        if active_task is not None and not active_task.done():
            active_task.cancel()

        try:
            await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "--user", "stop", unit_name],
                timeout=10,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.debug(f"systemctl --user stop {unit_name} skipped: {e}")

        await _kill_daemon_and_chrome(norm, port)
        return {
            "status": "stopped",
            "state": "DAEMON_DOWN",
            "broker": norm,
            "port": port,
        }


async def ensure_kss_tabs_complete() -> Dict[str, Any]:
    """Ensure both Streaming and SBL Real Time tabs are open on KSS (:9333) without firing OTP."""
    from scripts.broker_cdp.brokers import is_authenticated_path, kss_open_tabs, kss_try_sso_heal
    from scripts.broker_cdp.client import cdp_url_for, connect_cdp

    if _is_pytest_unmocked_cdp(connect_cdp):
        logger.debug("Skipping live CDP connect in ensure_kss_tabs_complete under pytest")
        return {
            "status": "SKIPPED_PYTEST",
            "state": "UNKNOWN",
            "streaming_state": "UNKNOWN",
            "sbl_state": "UNKNOWN",
            "port": get_broker_port("kss"),
        }

    pw = None
    try:
        from app.services.broker_cdp.login_progress import is_login_active

        if is_login_active("kss"):
            logger.debug("Skipping ensure_kss_tabs_complete: KSS daemon login/tab setup still active")
        else:
            async with _DaemonFileLock("kss", timeout=25.0, lock_name="kss-tabs"):
                if is_login_active("kss"):
                    logger.debug("Skipping ensure_kss_tabs_complete after lock: KSS login still active")
                else:
                    pw, _browser, ctx = await connect_cdp(cdp_url_for("kss"), broker="kss")
                    has_streaming = any(
                        is_authenticated_path(
                            "kss",
                            "streaming",
                            p.url if isinstance(getattr(p, "url", None), str) else "",
                        )
                        for p in ctx.pages
                    )
                    has_sbl = any(
                        is_authenticated_path(
                            "kss",
                            "sbl",
                            p.url if isinstance(getattr(p, "url", None), str) else "",
                        )
                        for p in ctx.pages
                    )
                    if has_streaming and has_sbl:
                        logger.debug("KSS Streaming & SBL tabs already open; skipping duplicate tab open")
                    elif has_streaming:
                        await kss_open_tabs(ctx)
                    else:
                        await kss_try_sso_heal(ctx)
    except Exception as e:
        logger.warning(f"ensure_kss_tabs_complete CDP error: {e}")
    finally:
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass

    str_probe = await cdp_session_status("kss", purpose="streaming")
    sbl_probe = await cdp_session_status("kss", purpose="sbl")
    str_state = str_probe.get("state", "UNKNOWN")
    sbl_state = sbl_probe.get("state", "UNKNOWN")
    both_ok = str_state == "AUTHENTICATED" and sbl_state == "AUTHENTICATED"
    return {
        "status": "AUTHENTICATED" if both_ok else str_state,
        "state": "AUTHENTICATED" if both_ok else str_state,
        "streaming_state": str_state,
        "sbl_state": sbl_state,
        "streaming": str_probe,
        "sbl": sbl_probe,
        "port": get_broker_port("kss"),
    }


async def _build_login_state_response(norm: str) -> Dict[str, Any]:
    st = await daemon_state(norm)
    lp = get_login_progress(norm)
    out: Dict[str, Any] = {
        "status": st["state"],
        "state": st["state"],
        "port": st["port"],
        "pid": st.get("pid"),
        "page_url": st.get("page_url"),
        "login_progress": lp,
    }
    if lp and lp.get("error"):
        out["error"] = lp.get("error")
    if norm == "kss":
        try:
            sbl_probe = await cdp_session_status("kss", purpose="sbl")
            out["sbl_state"] = sbl_probe.get("state", "UNKNOWN")
        except Exception:
            out["sbl_state"] = "UNKNOWN"
    return out


async def trigger_cdp_login(
    broker: str,
    wait_for_prompt_s: float = 15.0,
    wait_until_complete: bool = False,
    timeout_s: int = 300,
) -> Dict[str, Any]:
    """Attach over CDP to an active (or freshly ensured) daemon and drive interactive login."""
    from app.services.otp_relay_registry import (
        broker_matches,
        clear_pending_request,
        resolve_pending_request,
    )
    from scripts.broker_cdp.brokers import BROKERS
    from scripts.broker_cdp.client import cdp_url_for, connect_cdp

    norm = normalize_broker_slug(broker)
    port = get_broker_port(norm)

    if _is_pytest_unmocked_cdp(connect_cdp):
        logger.debug("Skipping live CDP connect in trigger_cdp_login(%s) under pytest", norm)
        return await _build_login_state_response(norm)

    existing_task = _ACTIVE_LOGIN_TASKS.get(norm)
    if existing_task is None or existing_task.done():
        if not await asyncio.to_thread(is_port_open, port):
            ens = await ensure_daemon(norm, wait_s=min(float(wait_for_prompt_s), 30.0))
            if ens.get("state") == "AUTHENTICATED":
                if norm == "kss":
                    await ensure_kss_tabs_complete()
                return await _build_login_state_response(norm)
            if not await asyncio.to_thread(is_port_open, port):
                return ens

    async def _run_login() -> bool:
        pw = None
        try:
            cfg = BROKERS[norm]
            pw, _browser, ctx = await connect_cdp(cdp_url_for(norm), broker=norm)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            login_fn = cfg["login"]
            try:
                sig = inspect.signature(login_fn)
                kwargs: Dict[str, Any] = {}
                has_varkw = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
                if "timeout_seconds" in sig.parameters or has_varkw:
                    kwargs["timeout_seconds"] = timeout_s
                if "resume_only" in sig.parameters or has_varkw:
                    kwargs["resume_only"] = False
                logged_in = await login_fn(ctx, page, **kwargs)
            except TypeError:
                logged_in = await login_fn(ctx, page)

            if logged_in:
                await cfg["open_tabs"](ctx)
                try:
                    from app.services.broker_cdp.sync_service import sync_portfolio_from_cdp
                    await sync_portfolio_from_cdp(norm)
                except Exception as e:
                    logger.warning(f"Post-login portfolio sync skipped for {norm}: {e}")
                try:
                    from app.services.broker_cdp.sync_service import sync_orders_from_cdp
                    await sync_orders_from_cdp(norm)
                except Exception as e:
                    logger.warning(f"Post-login order sync skipped for {norm}: {e}")
            return bool(logged_in)
        except Exception as e:
            logger.warning(f"trigger_cdp_login background task failed for {norm}: {e}")
            return False
        finally:
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:
                    pass
            cur = asyncio.current_task()
            if _ACTIVE_LOGIN_TASKS.get(norm) is cur:
                _ACTIVE_LOGIN_TASKS.pop(norm, None)

    existing_task = _ACTIVE_LOGIN_TASKS.get(norm)
    if existing_task is not None and not existing_task.done():
        task = existing_task
    elif not os.environ.get("PYTEST_CURRENT_TEST") and is_login_active(norm):
        # Another process (e.g. freshly spawned session_daemon.py) is actively driving
        # login on this Chrome tab — never attach a competing login driver on top of it.
        deadline_active = time.monotonic() + min(3.0, max(0.05, float(wait_for_prompt_s)))
        while time.monotonic() < deadline_active:
            pending = await resolve_pending_request()
            if pending is not None and not pending.is_expired and broker_matches(norm, pending.broker):
                st = await daemon_state(norm)
                return {
                    "status": "OTP_PAGE",
                    "state": "OTP_PAGE",
                    "port": port,
                    "pid": st.get("pid"),
                    "ref": pending.ref,
                    "page_url": st.get("page_url"),
                    "login_in_progress": True,
                    "login_progress": get_login_progress(norm),
                }
            st = await daemon_state(norm)
            if st["state"] == "AUTHENTICATED":
                if norm == "kss":
                    await ensure_kss_tabs_complete()
                return await _build_login_state_response(norm)
            if not is_login_active(norm):
                break
            await asyncio.sleep(0.5)
        res_active = await _build_login_state_response(norm)
        res_active["login_in_progress"] = True
        return res_active
    else:
        clear_pending_request(norm)
        task = asyncio.create_task(_run_login())
        _ACTIVE_LOGIN_TASKS[norm] = task

    if wait_until_complete:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"trigger_cdp_login await error for {norm}: {e}")
        return await _build_login_state_response(norm)

    deadline = time.monotonic() + max(0.05, float(wait_for_prompt_s))
    poll_step = min(0.5, max(0.05, float(wait_for_prompt_s) / 2))

    while time.monotonic() < deadline:
        if task.done():
            return await _build_login_state_response(norm)

        pending = await resolve_pending_request()
        if pending is not None and not pending.is_expired and broker_matches(norm, pending.broker):
            st = await daemon_state(norm)
            return {
                "status": "OTP_PAGE",
                "state": "OTP_PAGE",
                "port": port,
                "pid": st.get("pid"),
                "ref": pending.ref,
                "page_url": st.get("page_url"),
            }

        st = await daemon_state(norm)
        if st["state"] == "AUTHENTICATED":
            if norm == "kss":
                await ensure_kss_tabs_complete()
            return await _build_login_state_response(norm)

        if st["state"] == "OTP_PAGE":
            pending = await resolve_pending_request()
            ref_val = (
                pending.ref
                if (pending is not None and not pending.is_expired and broker_matches(norm, pending.broker))
                else None
            )
            return {
                "status": "OTP_PAGE",
                "state": "OTP_PAGE",
                "port": port,
                "pid": st.get("pid"),
                "ref": ref_val,
                "page_url": st.get("page_url"),
            }

        await asyncio.sleep(poll_step)

    if task.done():
        return await _build_login_state_response(norm)

    pending = await resolve_pending_request()
    st = await daemon_state(norm)
    if pending is not None and not pending.is_expired and broker_matches(norm, pending.broker):
        return {
            "status": "OTP_PAGE",
            "state": "OTP_PAGE",
            "port": port,
            "pid": st.get("pid"),
            "ref": pending.ref,
            "page_url": st.get("page_url"),
        }
    return await _build_login_state_response(norm)
