#!/usr/bin/env python3
"""Repair strategy state files from append-only trade logs.

This fixes older strategy state files that reused condition_id-side as the
position key and therefore overwrote prior closed entries on re-entry.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from paper_trader import (
    STRATEGY1_STATE_FILE,
    STRATEGY1_TRADES_LOG,
    STRATEGY2_STATE_FILE,
    STRATEGY2_TRADES_LOG,
    STRATEGY3_STATE_FILE,
    STRATEGY3_TRADES_LOG,
    PaperTrader,
)


def _safe_ts(ts: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", ts or datetime.now(timezone.utc).isoformat())


def _base_key(entry: dict) -> str:
    condition = entry.get("condition_id") or ""
    side = entry.get("side") or "BUY"
    return f"{condition}-{side}" if condition else entry.get("event_slug", "unknown")


def _net_pnl(pos: dict) -> float:
    exits = pos.get("exits", []) or []
    return float(pos.get("pnl", 0) or 0) + sum(float(e.get("pnl", 0) or 0) for e in exits if isinstance(e, dict))


def _classify(pos: dict) -> None:
    pnl = _net_pnl(pos)
    if pnl > 0.05:
        pos["outcome_class"] = "real_win"
        pos["resolved_outcome"] = "win"
    elif pnl >= -0.05:
        pos["outcome_class"] = "flat"
        pos["resolved_outcome"] = "win"
    else:
        pos["outcome_class"] = "loss"
        pos["resolved_outcome"] = "loss"


def repair_state(state_path: Path, log_path: Path, mode: str) -> dict:
    if not log_path.exists():
        raise FileNotFoundError(log_path)

    old_state = json.loads(state_path.read_text()) if state_path.exists() else {}
    positions: dict[str, dict] = {}
    active_by_base: dict[str, list[str]] = defaultdict(list)
    latest_by_base: dict[str, str] = {}
    open_count = 0

    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        action = entry.get("action")
        base = entry.get("market_side_key") or _base_key(entry)
        position_id = entry.get("position_id")

        if action == "OPEN":
            open_count += 1
            if not position_id:
                position_id = f"{base}-{open_count:06d}-{_safe_ts(entry.get('entry_ts'))}"
            entry["position_id"] = position_id
            entry["market_side_key"] = base
            positions[position_id] = {k: v for k, v in entry.items() if k != "action"}
            active_by_base[base].append(position_id)
            latest_by_base[base] = position_id
            continue

        target = position_id
        if not target:
            active = active_by_base.get(base, [])
            target = active[-1] if active else latest_by_base.get(base)
        if not target or target not in positions:
            continue

        pos = positions[target]
        payload = {k: v for k, v in entry.items() if k != "action"}
        pos.update(payload)
        pos["position_id"] = target
        pos["market_side_key"] = base
        positions[target] = pos
        latest_by_base[base] = target

        if action == "CLOSE":
            pos["status"] = "closed"
            _classify(pos)
            if target in active_by_base.get(base, []):
                active_by_base[base].remove(target)

    repaired = old_state.copy()
    repaired["positions"] = positions
    repaired["total_trades"] = open_count
    repaired["repaired_at"] = datetime.now(timezone.utc).isoformat()
    repaired["repair_source_log"] = str(log_path)

    wins = losses = wins_real = wins_flat = 0
    for pos in positions.values():
        if pos.get("status") != "closed":
            continue
        _classify(pos)
        outcome = pos.get("outcome_class")
        if outcome == "real_win":
            wins += 1
            wins_real += 1
        elif outcome == "flat":
            wins_flat += 1
        else:
            losses += 1
    repaired["wins"] = wins
    repaired["losses"] = losses
    repaired["wins_real"] = wins_real
    repaired["wins_flat"] = wins_flat

    # Preserve the live bankroll because older logs do not include enough cash
    # events to safely reconstruct partial proceeds for every historical mode.
    repaired["bankroll"] = old_state.get("bankroll", repaired.get("starting_bankroll", 100.0))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if state_path.exists():
        backup = state_path.with_suffix(state_path.suffix + f".bak-{stamp}")
        shutil.copy2(state_path, backup)
        repaired["repair_backup"] = str(backup)
    state_path.write_text(json.dumps(repaired, indent=2, default=str))
    return repaired


def main() -> int:
    jobs = [
        (STRATEGY1_STATE_FILE, STRATEGY1_TRADES_LOG, "strategy-1-middle"),
        (STRATEGY2_STATE_FILE, STRATEGY2_TRADES_LOG, "strategy-2-tail"),
        (STRATEGY3_STATE_FILE, STRATEGY3_TRADES_LOG, "strategy-3-compound-tail"),
    ]
    for state_path, log_path, mode in jobs:
        repaired = repair_state(state_path, log_path, mode)
        trader = PaperTrader(mode=mode)
        open_positions = [p for p in repaired["positions"].values() if p.get("status") == "open"]
        closed_positions = [p for p in repaired["positions"].values() if p.get("status") == "closed"]
        print(
            f"{mode}: total={repaired['total_trades']} "
            f"records={len(repaired['positions'])} open={len(open_positions)} "
            f"closed={len(closed_positions)} bankroll={trader.state.get('bankroll')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
