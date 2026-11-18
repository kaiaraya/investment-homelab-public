#!/usr/bin/env python3
"""Build a sanitized, secret-free mirror directory of InvestmentHomelab for Public GitHub (Argon).

Usage:
    python3 scripts/dev/sanitize_mirror.py --dest /tmp/mirror_dir [--repo .]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

INCLUDE_PREFIXES = (
    "app/",
    "scripts/",
    "tests/",
    "data/bookmarklets/",
    "docs/agent/",
    "learned_workflows/",
    "rca/",
)
INCLUDE_ROOT_FILES = {
    ".env.example",
    ".gitignore",
    ".mirror-redact.example",
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "start_homelab.sh",
    "stop_homelab.sh",
}

NON_SECRET_SUFFIXES = (
    "_PATH",
    "_URL",
    "_MODE",
    "_ENABLED",
    "_SECONDS",
    "_MINUTES",
    "_FAILURES",
    "_EXECUTABLE",
    "_TTL",
)
SECRET_KEY_PARTS = (
    "PIN",
    "PASS",
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "API_KEY",
    "USERNAME",
    "ACCOUNT_NO",
    "USER_ID",
    "GROUP_ID",
)

GENERIC_REGEX_RULES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "<REDACTED_JWT>"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "<REDACTED_GOOGLE_API_KEY>"),
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{36}"), "<REDACTED_GITHUB_TOKEN>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "<REDACTED_API_KEY>"),
    (re.compile(r"\b[UC][0-9a-f]{32}\b"), "<REDACTED_LINE_ID>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "<REDACTED_PRIVATE_KEY>"),
]


def is_included(rel_path: str) -> bool:
    """Return True when a repo-relative tracked path belongs in the public mirror."""
    if rel_path in INCLUDE_ROOT_FILES:
        return True
    return any(rel_path.startswith(prefix) for prefix in INCLUDE_PREFIXES)


def _is_secret_env_key(key: str) -> bool:
    upper = key.upper()
    if any(upper.endswith(suf) for suf in NON_SECRET_SUFFIXES):
        return False
    return any(part in upper for part in SECRET_KEY_PARTS)


def load_env_secrets(env_path: Path) -> Dict[str, str]:
    """Load real credential values from .env keyed by env var name."""
    if not env_path.is_file():
        return {}
    secrets: Dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not v or len(v) < 6 or v.lower() in {"true", "false", "none", "null"}:
            continue
        if _is_secret_env_key(k):
            secrets[k] = v
    return secrets


def load_redact_rules(redact_path: Path) -> List[Tuple[re.Pattern[str], str]]:
    """Load custom `regex=replacement` rules from .mirror-redact."""
    if not redact_path.is_file():
        return []
    rules: List[Tuple[re.Pattern[str], str]] = []
    for raw in redact_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        pat, repl = line.split("=", 1)
        rules.append((re.compile(pat.strip()), repl.strip()))
    return rules


def build_rules(
    env_secrets: Dict[str, str],
    custom_rules: List[Tuple[re.Pattern[str], str]],
) -> List[Tuple[re.Pattern[str], str]]:
    """Build ordered redaction rules (longest literal values first)."""
    rules: List[Tuple[re.Pattern[str], str]] = []
    for k, v in sorted(env_secrets.items(), key=lambda kv: len(kv[1]), reverse=True):
        pat = re.compile(rf"(?<![0-9A-Za-z]){re.escape(v)}(?![0-9A-Za-z])")
        rules.append((pat, f"<REDACTED_{k}>"))
    rules.extend(custom_rules)
    rules.extend(GENERIC_REGEX_RULES)
    return rules


def redact_text(text: str, rules: Iterable[Tuple[re.Pattern[str], str]]) -> str:
    """Apply all redaction rules sequentially to text."""
    for pat, repl in rules:
        text = pat.sub(repl, text)
    return text


def verify_clean(dest: Path, rules: List[Tuple[re.Pattern[str], str]]) -> List[str]:
    """Fail-closed scan: return list of relative paths that still match any rule."""
    hits: List[str] = []
    for p in sorted(dest.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pat, _ in rules:
            if pat.search(content):
                hits.append(str(p.relative_to(dest)))
                break
    return hits


def git_tracked_files(repo: Path) -> List[str]:
    out = subprocess.check_output(["git", "-C", str(repo), "ls-files"], text=True)
    return [line for line in out.splitlines() if line and is_included(line)]


def git_head_info(repo: Path) -> Tuple[str, str]:
    branch = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], text=True
    ).strip()
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], text=True
    ).strip()
    return branch, sha


def write_manifest(
    dest: Path,
    branch: str,
    sha: str,
    file_count: int,
    total_bytes: int,
    rules: Iterable[Tuple[re.Pattern[str], str]] = (),
) -> None:
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S ICT")
    mb = total_bytes / (1024 * 1024)
    manifest = f"""# REPO_MANIFEST — Public Mirror for Gemini Web (Argon)

- **Source Repo:** `InvestmentHomelab` (Private)
- **Synced Branch:** `{branch}`
- **Synced Commit:** `{sha}`
- **Synced At:** `{now}`
- **Files:** {file_count} ({mb:.2f} MiB)

## Notes for Argon
- This repository is an automated, secret-sanitized single-commit mirror. Credentials and account numbers are replaced with `<REDACTED_...>` placeholders.
- Excluded from mirror: `docs/handoff/`, `reference/`, runtime DBs/logs, `.env`.
- Key entrypoints: `AGENTS.md`, `app/main.py`, `app/api/v1/api.py`, `app/miniapp/`, `scripts/daily_readiness.py`, `scripts/verify_changes.py`.
- If the commit SHA above differs from what Antigravity just pushed, start a new Gemini Web chat and re-import this repository.
"""
    (dest / "REPO_MANIFEST.md").write_text(redact_text(manifest, rules), encoding="utf-8")


def build_mirror(repo: Path, dest: Path) -> Tuple[int, int]:
    """Populate `dest` with sanitized files and fail closed if any secret remains."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    env_secrets = load_env_secrets(repo / ".env")
    custom_rules = load_redact_rules(repo / ".mirror-redact")
    rules = build_rules(env_secrets, custom_rules)

    files = git_tracked_files(repo)
    total_bytes = 0
    for rel in files:
        src = repo / rel
        if not src.is_file():
            continue
        out_path = dest / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = src.read_text(encoding="utf-8")
            sanitized = redact_text(text, rules)
            out_path.write_text(sanitized, encoding="utf-8")
            total_bytes += len(sanitized.encode("utf-8"))
        except UnicodeDecodeError:
            shutil.copy2(src, out_path)
            total_bytes += out_path.stat().st_size

    branch, sha = git_head_info(repo)
    write_manifest(dest, branch, sha, len(files) + 1, total_bytes, rules)

    leaked = verify_clean(dest, rules)
    if leaked:
        raise RuntimeError(f"Secret verification failed in {len(leaked)} file(s): {leaked[:10]}")
    return len(files) + 1, total_bytes


FORBIDDEN_STAGED_RE = re.compile(
    r"(^|/)\.env(\.[^/]+)?$|(^|/)\.mirror-redact$|\.(db|sqlite|sqlite3|pem|key|har)$|session_state|storage_state",
    re.IGNORECASE,
)


def check_staged_secrets(repo: Path) -> List[str]:
    """Scan staged files and added diff lines; return descriptive violation messages (never values)."""
    violations: List[str] = []
    names = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        text=True,
    ).splitlines()
    for name in names:
        if not name or name.endswith(".env.example"):
            continue
        if FORBIDDEN_STAGED_RE.search(name):
            violations.append(f"forbidden sensitive file staged: {name}")

    rules = build_rules(load_env_secrets(repo / ".env"), load_redact_rules(repo / ".mirror-redact"))
    diff = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"],
        text=True,
        errors="replace",
    )
    current_file = "?"
    flagged: set[Tuple[str, str]] = set()
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("+") and not line.startswith("+++ "):
            added = line[1:]
            for pat, repl in rules:
                if pat.search(added):
                    key = (current_file, repl)
                    if key not in flagged:
                        flagged.add(key)
                        violations.append(f"secret pattern {repl} staged in {current_file}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="Create sanitized public mirror tree or check staged secrets.")
    parser.add_argument("--repo", default=".", help="Source git repo path")
    parser.add_argument("--dest", help="Output staging directory")
    parser.add_argument("--check-staged", action="store_true", help="Verify staged git changes contain no secrets")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if args.check_staged:
        violations = check_staged_secrets(repo)
        if violations:
            for v in violations:
                print(f"[BLOCKED] {v}", file=sys.stderr)
            return 1
        print("[OK] Pre-commit secret check passed (0 secrets in staged diff)")
        return 0

    if not args.dest:
        parser.error("--dest is required unless --check-staged is set")

    dest = Path(args.dest).resolve()
    try:
        count, size = build_mirror(repo, dest)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] Sanitized mirror ready: {count} files, {size / (1024 * 1024):.2f} MiB (0 secrets detected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
