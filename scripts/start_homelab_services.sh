#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

WITH_CDP=0
for arg in "$@"; do
    case "$arg" in
        --with-cdp)
            WITH_CDP=1
            ;;
        -h|--help)
            echo "Usage: $0 [--with-cdp]"
            echo "  default     Start & enable Gateway :8000 only (for CF Tunnel + LINE webhook)"
            echo "  --with-cdp  Also start KSS (:9333) & Liberator (:9334) Chrome CDP temporarily (not enabled at boot)"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

mkdir -p data/logs "$HOME/.config/systemd/user"

echo "=========================================================="
if [[ "$WITH_CDP" -eq 1 ]]; then
    echo "  🚀 Starting Investment Homelab (Gateway + Temporary CDP)"
else
    echo "  🚀 Starting Investment Homelab (Gateway :8000 Only)"
fi
echo "=========================================================="

# Ensure broker CDP units are never enabled in default.target.wants on boot
# (disable before linking unit files, because `systemctl disable` removes external symlinks too)
systemctl --user disable homelab-kss-cdp.service homelab-lib-cdp.service >/dev/null 2>&1 || true
rm -f "$HOME/.config/systemd/user/default.target.wants/homelab-kss-cdp.service" \
      "$HOME/.config/systemd/user/default.target.wants/homelab-lib-cdp.service" 2>/dev/null || true

# Install / refresh systemd --user unit symlinks (Single Source of Truth)
for unit in homelab-gateway.service homelab-kss-cdp.service homelab-lib-cdp.service; do
    ln -sfn "$DIR/scripts/systemd/$unit" "$HOME/.config/systemd/user/$unit"
done
systemctl --user daemon-reload

# Stop any legacy transient units created by older Hermes crons so ports never collide
systemctl --user stop kss-cdp.service lib-cdp.service 2>/dev/null || true

# Clean up any legacy Playwright node drivers / temp profiles so TradeMan never lingers
pkill -f "[p]laywright.*driver.*package/cli.js" 2>/dev/null || true
rm -rf /tmp/playwright_chromiumdev_profile-* 2>/dev/null || true

# 1. FastAPI Gateway (:8000 — runs 24/7)
systemctl --user enable --now homelab-gateway.service >/dev/null 2>&1 || true
for _ in {1..20}; do
    if ss -ltn "sport = :8000" | grep -q ":8000"; then
        break
    fi
    sleep 0.5
done
if ss -ltn "sport = :8000" | grep -q ":8000"; then
    echo "✅ [1/1] Gateway :8000 active via homelab-gateway.service"
else
    echo "❌ Gateway :8000 failed to start. Check: systemctl --user status homelab-gateway"
    exit 1
fi

start_cdp_unit() {
    local unit="$1"
    local port="$2"
    local label="$3"
    if ! ss -ltn "sport = :$port" | grep -q ":$port"; then
        echo "▶️ Starting $label CDP :$port ($unit — temporary, not enabled at boot)..."
        systemctl --user start "$unit" >/dev/null 2>&1 || true
        for _ in {1..20}; do
            if ss -ltn "sport = :$port" | grep -q ":$port"; then
                break
            fi
            sleep 0.5
        done
    fi
    if ss -ltn "sport = :$port" | grep -q ":$port"; then
        echo "✅ $label CDP :$port active"
    else
        echo "⚠️ $label CDP :$port did not listen within 10s. Check: systemctl --user status $unit"
    fi
}

if [[ "$WITH_CDP" -eq 1 ]]; then
    start_cdp_unit "homelab-kss-cdp.service" 9333 "Krungsri"
    start_cdp_unit "homelab-lib-cdp.service" 9334 "Liberator"
    echo "----------------------------------------------------------"
    echo "🔍 Running Pure-CDP Daily Readiness (Streaming + SBL + Liberator)..."
    echo "----------------------------------------------------------"
    .venv/bin/python scripts/daily_readiness.py --no-ping
else
    echo "ℹ️ Broker CDP browsers (:9333, :9334) left stopped (open on-demand via LINE /login or --with-cdp)."
fi

echo "=========================================================="
echo "🎉 Gateway :8000 ready! Type /status in LINE to verify."
echo "=========================================================="
