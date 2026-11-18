#!/usr/bin/env bash
# Investment Homelab Stop Script
# Default: stops ONLY Chrome CDP browsers (:9333 KSS, :9334 Liberator) to free RAM,
# keeping FastAPI Gateway (:8000) alive 24/7 so LINE CLI (/status, /login) always works.
# Pass --all to also stop homelab-gateway.service for full maintenance.

STOP_ALL=0
if [[ "${1:-}" == "--all" ]]; then
    STOP_ALL=1
fi

echo "=========================================================="
if [[ "$STOP_ALL" -eq 1 ]]; then
    echo "🛑 Stopping ALL Investment Homelab Services (CDP + Gateway :8000)..."
else
    echo "🛑 Stopping Broker Chrome CDP Browsers (:9333 / :9334) to free RAM..."
    echo "   (Gateway :8000 remains UP 24/7 so LINE CLI /status & /login stay ready)"
fi
echo "=========================================================="

# 1. Stop systemd --user CDP units (and any legacy transient units)
systemctl --user stop homelab-kss-cdp.service homelab-lib-cdp.service kss-cdp.service lib-cdp.service 2>/dev/null || true

# 2. Clean up any standalone session_daemon.py or Chrome on 9333/9334
pkill -f "scripts/broker_cdp/[s]ession_daemon.py" 2>/dev/null || true
pkill -f "[c]hrome.*--remote-debugging-port=9333" 2>/dev/null || true
pkill -f "[c]hrome.*--remote-debugging-port=9334" 2>/dev/null || true
# Clean up any legacy TradeMan or orphan Playwright node drivers & temp profiles
pkill -f "[p]laywright.*driver.*package/cli.js" 2>/dev/null || true
rm -rf /tmp/playwright_chromiumdev_profile-* 2>/dev/null || true
rm -f data/daemon-kss.pid data/daemon-liberator.pid 2>/dev/null || true

if [[ "$STOP_ALL" -eq 1 ]]; then
    systemctl --user stop homelab-gateway.service 2>/dev/null || true
    pkill -f "[u]vicorn app.main:app" 2>/dev/null || true
    echo "✅ Gateway (:8000) and all Chrome CDP browsers stopped."
else
    echo "✅ Chrome CDP browsers (:9333, :9334) stopped. Gateway (:8000) is still active."
fi
