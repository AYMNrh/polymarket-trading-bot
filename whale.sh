#!/bin/bash
# 🐋 Whale Trading Platform — convenience launcher
cd "$(dirname "$0")"
PYTHON="/home/aymen/hermes-agent/venv/bin/python"
CMD="$PYTHON run.py"

case "${1:-help}" in
  scan|monitor|strategy|dashboard|full-cycle|daemon|summary)
    shift
    exec $CMD "$@" ;;
  daemon-bg)
    shift
    nohup $CMD daemon --no-dashboard "$@" > /tmp/whale-daemon.log 2>&1 &
    echo "Daemon started (PID $!)" ;;
  status)
    exec $CMD summary ;;
  *)
    echo "🐋 Polymarket Whale Trading Platform"
    echo ""
    echo "Usage: ./whale.sh <command> [options]"
    echo ""
    echo "Commands:"
    echo "  scan              Discover new whales from on-chain data"
    echo "  monitor           Check known whales for new trades"
    echo "  strategy          Analyze weather markets for opportunities"
    echo "  dashboard         Start web UI at http://localhost:9091"
    echo "  full-cycle        One complete cycle (scan+monitor+strategy)"
    echo "  daemon            Run everything continuously"
    echo "  daemon-bg         Run daemon in background"
    echo "  status            Show quick stats"
    echo ""
    echo "Examples:"
    echo "  ./whale.sh dashboard       # Start the web UI"
    echo "  ./whale.sh daemon          # Run forever"
    echo "  ./whale.sh scan            # Discover new whales"
    echo "  ./whale.sh status          # Show summary"
    ;;
esac
