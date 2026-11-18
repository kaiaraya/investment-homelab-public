#!/usr/bin/env bash
# One-click safe sync: pre-commit secret check -> commit dirty (if any) -> push Private -> sync Public Mirror.
# Usage:
#   bash scripts/dev/sync_repo.sh          # interactive (asks optional commit msg if dirty, pauses at end)
#   bash scripts/dev/sync_repo.sh --auto   # non-interactive (auto WIP message, no pause)
set -uo pipefail

AUTO=0
if [[ "${1:-}" == "--auto" ]]; then
  AUTO=1
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo /home/araya-sawasdichai/Documents/antigravity/InvestmentHomelab)"
cd "$REPO_ROOT" || exit 1
PY="$REPO_ROOT/.venv/bin/python"

notify() {
  local urgency="$1" title="$2" body="$3"
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -u "$urgency" "$title" "$body" >/dev/null 2>&1 || true
  fi
}

finish() {
  local rc="$1"
  if [[ "$AUTO" == "0" ]]; then
    echo ""
    read -rp "กด Enter เพื่อปิดหน้าต่าง..." _ || true
  fi
  exit "$rc"
}

branch="$(git branch --show-current || true)"
if [[ -z "$branch" ]]; then
  echo "[ERROR] Detached HEAD — กรุณา checkout branch ก่อน sync"
  notify critical "InvestmentHomelab Sync ล้มเหลว" "Detached HEAD: ไม่สามารถ sync ได้"
  finish 1
fi

echo "=========================================================="
echo " 🔄 Sync InvestmentHomelab ($branch)"
echo "=========================================================="

dirty="$(git status --porcelain)"
if [[ -n "$dirty" ]]; then
  echo "พบไฟล์ที่ยังไม่ commit:"
  git status -sb
  echo ""
  git add -A
  if ! "$PY" "$REPO_ROOT/scripts/dev/sanitize_mirror.py" --check-staged --repo "$REPO_ROOT"; then
    git reset >/dev/null 2>&1 || true
    echo "[ABORT] พบค่าลับหรือไฟล์ต้องห้าม — ยกเลิกการ commit เพื่อความปลอดภัย"
    notify critical "InvestmentHomelab Sync ถูกบล็อก" "ตรวจพบค่าลับในไฟล์ที่กำลังจะ commit (ยกเลิกแล้ว)"
    finish 1
  fi

  default_msg="chore(sync): WIP snapshot $(date '+%Y-%m-%d %H:%M')"
  if [[ "$AUTO" == "1" ]]; then
    msg="$default_msg"
  else
    read -rp "ข้อความ commit (กด Enter ใช้ '$default_msg'): " input_msg || true
    msg="${input_msg:-$default_msg}"
  fi

  if ! git commit -m "$msg"; then
    echo "[ERROR] git commit ล้มเหลว"
    notify critical "InvestmentHomelab Sync ล้มเหลว" "git commit ไม่ผ่าน (ตรวจ pre-commit / git status)"
    finish 1
  fi
else
  echo "✅ Working tree สะอาด (ไม่มีไฟล์ค้าง)"
fi

echo ""
echo "--- [1/2] Push Private (origin/$branch) ---"
if ! git fetch origin "$branch" >/dev/null 2>&1; then
  echo "ℹ️  Remote branch ยังไม่มีหรือเช็คไม่ได้ — จะ push สร้าง/อัปเดตต่อไป"
else
  read -r behind ahead <<<"$(git rev-list --left-right --count "origin/$branch...$branch" 2>/dev/null || echo '0 0')"
  if [[ "${behind:-0}" -gt 0 ]]; then
    echo "[ABORT] origin/$branch มี commit นำหน้าอยู่ $behind commit — กรุณา pull/rebase ก่อน"
    notify critical "InvestmentHomelab Sync หยุดรอ" "origin/$branch นำหน้าอยู่ $behind commit"
    finish 1
  fi
fi

if ! git push -u origin "$branch"; then
  echo "[ERROR] Push Private ล้มเหลว"
  notify critical "InvestmentHomelab Sync ล้มเหลว" "Push ขึ้น Private GitHub ไม่สำเร็จ"
  finish 1
fi

echo ""
echo "--- [2/2] Sync Public Mirror (Argon) ---"
if ! bash "$REPO_ROOT/scripts/dev/sync-public-mirror.sh"; then
  echo "[ERROR] Public mirror sync ล้มเหลว"
  notify critical "InvestmentHomelab Public Mirror ล้มเหลว" "Private push สำเร็จ แต่ Public mirror ไม่ผ่าน"
  finish 1
fi

sha="$(git rev-parse --short HEAD)"
origin_url="$(git remote get-url origin 2>/dev/null || true)"
public_https="$(echo "$origin_url" | sed -E 's#^git@github\.com:#https://github.com/#; s#\.git$#-public#')"
echo ""
echo "✅ Sync ครบทั้ง Private และ Public Mirror ที่ $branch @ $sha"
echo "👉 ใน Gemini Web (Argon): เปิดแชทใหม่แล้ว Import code ${public_https:-<public-mirror-url>}"
notify normal "InvestmentHomelab Sync สำเร็จ" "Private ($branch@$sha) และ Public Mirror ตรงกันแล้ว"
finish 0
