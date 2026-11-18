#!/usr/bin/env bash
# daily_close.sh — one command to close the trading day safely.
#   full suite -> regenerate board mirror -> reconcile (no uncommitted CODE) -> backup.
# Safe by design: never commits, never deletes, never touches git history.
# Usage: scripts/daily_close.sh [--no-tests] [--include-browser]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PY=".venv/bin/python"
RUN_TESTS=1
INCLUDE_BROWSER=0
for a in "$@"; do
  case "$a" in
    --no-tests) RUN_TESTS=0 ;;
    --include-browser) INCLUDE_BROWSER=1 ;;
  esac
done
fail=0
echo "===== DAILY CLOSE $(date '+%F %T %Z') ====="

# 1) batched full suite (run once at EOD outside task loops; faulthandler_timeout=60)
if [ "$RUN_TESTS" = "1" ]; then
  echo "--- [1/4] full test suite (batched) ---"
  if [ "$INCLUDE_BROWSER" = "1" ]; then
    FULL_FLAGS="--full --batch-size 12"
  else
    FULL_FLAGS="--full --batch-size 12 --exclude-browser"
  fi
  if $PY scripts/verify_changes.py $FULL_FLAGS; then echo "[OK] suite green"; else echo "[FAIL] suite RED"; fail=1; fi
else
  echo "--- [1/4] suite skipped (--no-tests) ---"
fi

# 2) repo & worktree hygiene snapshot
echo "--- [2/4] repo & worktree status ---"
if bash scripts/dev/repo-status.sh; then echo "[OK] repo status"; else echo "[WARN] repo status check returned non-zero"; fi

# 3) reconcile: CODE must be committed. Daily ledger docs are allowed to be dirty.
echo "--- [3/4] tree reconcile (code must be committed) ---"
dirty=$(git status --porcelain \
  | grep -v 'docs/handoff/DAILY-' || true)
if [ -n "$dirty" ]; then
  echo "[WARN] uncommitted code / unexpected files:"; echo "$dirty"; fail=1
else
  echo "[OK] no uncommitted code"
fi

# 4) backup tracked patch + untracked files (never destructive)
echo "--- [4/4] backup ---"
BK="$HOME/.cache/homelab-backup-$(date +%Y%m%d)"
mkdir -p "$BK"
git diff > "$BK/tracked.patch" 2>/dev/null || true
git ls-files --others --exclude-standard > "$BK/untracked.txt"
tar czf "$BK/untracked.tgz" -T "$BK/untracked.txt" 2>/dev/null || true
echo "[OK] backup -> $BK"

echo "===== DONE fail=$fail $(date '+%F %T %Z') ====="
if [ "$fail" = "0" ]; then echo "STATUS: CLEAN"; else echo "STATUS: REVIEW NEEDED"; fi
exit $fail
