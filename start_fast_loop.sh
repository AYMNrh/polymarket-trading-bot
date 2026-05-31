#!/usr/bin/env bash
cd /home/aymen/projects/scripts/trading-bot
exec nohup python3 -u scripts/fast_loop.py > fast_loop.log 2>&1 &
echo "started pid=$!"
