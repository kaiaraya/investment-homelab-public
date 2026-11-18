#!/usr/bin/env bash
# Quick git/worktree health summary for InvestmentHomelab (Antigravity session start & EOD).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

branch=$(git branch --show-current || echo "DETACHED")
head_sha=$(git rev-parse --short HEAD)
dirty_count=$(git status --porcelain | wc -l | tr -d ' ')

echo "=== Repo Status ==="
echo "Branch:    $branch @ $head_sha"
echo "Dirty:     $dirty_count file(s)"

if git show-ref --verify --quiet refs/heads/main; then
  read -r behind ahead <<<"$(git rev-list --left-right --count main..."$branch")"
  echo "vs main:   +$ahead / -$behind"
fi

if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}')
  read -r u_behind u_ahead <<<"$(git rev-list --left-right --count "$upstream"..."$branch")"
  echo "vs $upstream: +$u_ahead / -$u_behind"
else
  echo "Upstream:  (none — run git push -u origin $branch)"
fi

echo ""
echo "=== Worktrees ==="
git worktree list

echo ""
echo "=== Local Branches ==="
git branch --sort=-committerdate --format='  %(HEAD) %(refname:short) (%(committerdate:short)) %(subject)'

stash_count=$(git stash list | wc -l | tr -d ' ')
if [ "$stash_count" -gt 0 ]; then
  echo ""
  echo "=== Stashes ($stash_count) ==="
  git stash list
fi
