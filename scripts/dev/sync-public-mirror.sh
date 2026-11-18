#!/usr/bin/env bash
# Sync sanitized single-commit mirror of current HEAD to the Public GitHub repo for Gemini Web (Argon).
# Usage:
#   bash scripts/dev/sync-public-mirror.sh --dry-run
#   bash scripts/dev/sync-public-mirror.sh
set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
PUBLIC_REMOTE="${PUBLIC_MIRROR_REMOTE:-git@github.com:<REDACTED_KRUNGSRI_USERNAME>/investment-homelab-public.git}"
BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
STAGE_DIR="$(mktemp -d /tmp/homelab-public-mirror.XXXXXX)"
trap 'rm -rf "$STAGE_DIR"' EXIT

"$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/dev/sanitize_mirror.py" \
  --repo "$REPO_ROOT" \
  --dest "$STAGE_DIR"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[DRY-RUN] Manifest preview:"
  cat "$STAGE_DIR/REPO_MANIFEST.md"
  echo "[DRY-RUN] Skipping push to $PUBLIC_REMOTE"
  exit 0
fi

git -C "$STAGE_DIR" init -b main >/dev/null
git -C "$STAGE_DIR" config user.name "$(git -C "$REPO_ROOT" config user.name || echo 'Antigravity')"
git -C "$STAGE_DIR" config user.email "$(git -C "$REPO_ROOT" config user.email || echo 'noreply@localhost')"
git -C "$STAGE_DIR" add -A
git -C "$STAGE_DIR" commit -m "sync(mirror): ${BRANCH}@${SHA}" >/dev/null
git -C "$STAGE_DIR" push --force "$PUBLIC_REMOTE" HEAD:main
echo "[OK] Synced ${BRANCH}@${SHA} -> $PUBLIC_REMOTE (main)"
