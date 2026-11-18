"""Tests for scripts/dev/sanitize_mirror.py (public mirror redaction & fail-closed gate)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.dev import sanitize_mirror as sm


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@local"], check=True)
    return repo


def test_build_mirror_redacts_secrets_and_filters_dirs(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / ".env").write_text(
        'LIBERATOR_PIN="987654"\nLIBERATOR_USERNAME="1007777"\nAPI_V1_STR="/api/v1"\n',
        encoding="utf-8",
    )
    (repo / ".mirror-redact").write_text(r"\b30249\d{2}\b=<REDACTED_KSS_ACCT>" + "\n", encoding="utf-8")

    (repo / "app").mkdir()
    (repo / "app" / "sample.py").write_text(
        'URL = "/api/v1/orders"\nPIN = "987654"\nUSER = "1007777"\nKSS = "<REDACTED_KSS_ACCT>"\n',
        encoding="utf-8",
    )
    (repo / "docs" / "handoff").mkdir(parents=True)
    (repo / "docs" / "handoff" / "secret_capture.jsonl").write_text("secret stuff\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")

    subprocess.run(["git", "-C", str(repo), "add", "app/sample.py", "docs/handoff/secret_capture.jsonl", "AGENTS.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    dest = tmp_path / "mirror"
    count, _ = sm.build_mirror(repo, dest)

    assert count == 3  # app/sample.py, AGENTS.md, REPO_MANIFEST.md
    assert not (dest / "docs" / "handoff" / "secret_capture.jsonl").exists()
    assert not (dest / ".env").exists()

    sanitized = (dest / "app" / "sample.py").read_text(encoding="utf-8")
    assert 'URL = "/api/v1/orders"' in sanitized
    assert "987654" not in sanitized
    assert "1007777" not in sanitized
    assert "<REDACTED_KSS_ACCT>" not in sanitized
    assert "<REDACTED_LIBERATOR_PIN>" in sanitized
    assert "<REDACTED_LIBERATOR_USERNAME>" in sanitized
    assert "<REDACTED_KSS_ACCT>" in sanitized
    assert (dest / "REPO_MANIFEST.md").is_file()


def test_verify_clean_detects_unredacted_secret(tmp_path: Path):
    dest = tmp_path / "dirty_dest"
    dest.mkdir()
    (dest / "leak.py").write_text('token = "' + "AIza" + "SyDummyGoogleKey1234567890123456789" + '"\n', encoding="utf-8")
    hits = sm.verify_clean(dest, sm.GENERIC_REGEX_RULES)
    assert hits == ["leak.py"]


def test_check_staged_secrets_blocks_forbidden_file_and_added_secret(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / ".env").write_text('LIBERATOR_PIN="987654"\n', encoding="utf-8")
    (repo / ".mirror-redact").write_text(r"\b30249\d{2}\b=<REDACTED_KSS_ACCT>" + "\n", encoding="utf-8")
    (repo / "clean.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "clean.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
    assert sm.check_staged_secrets(repo) == []

    # Staging a newly added line with a real .env secret + forbidden file must fail closed
    (repo / "clean.py").write_text('x = 1\npin = "987654"\n', encoding="utf-8")
    (repo / "secret.db").write_text("db", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-f", "clean.py", "secret.db"], check=True)
    violations = sm.check_staged_secrets(repo)
    assert any("forbidden sensitive file staged: secret.db" in v for v in violations)
    assert any("<REDACTED_LIBERATOR_PIN>" in v and "clean.py" in v for v in violations)


def test_check_staged_secrets_ignores_untouched_context_lines(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / ".env").write_text('LIBERATOR_PIN="987654"\n', encoding="utf-8")
    (repo / "legacy.py").write_text('OLD = "987654"\nOTHER = 1\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "legacy.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "legacy"], check=True, capture_output=True)

    # Editing an unrelated line in legacy.py should pass pre-commit without blocking
    (repo / "legacy.py").write_text('OLD = "987654"\nOTHER = 2\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "legacy.py"], check=True)
    assert sm.check_staged_secrets(repo) == []
