#!/usr/bin/env python3
"""
V6 Structured Logging System for Tail Experiment Bot.

Four independent logs, all JSONL:
  1. Position table  — one row per position, updated in place
  2. Event log       — one row per action (OPEN, CLOSE, PARTIAL, etc.)
  3. Market snapshots — one row per market evaluated per cycle
  4. Model snapshots  — one row per model evaluation per cycle
  5. Portfolio snapshots — one row per cron cycle

Design rule: every action is reconstructable from the event log alone.
The position table is a convenience view derived from events.
"""
import json
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent / "data" / "v6_logs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex[:16]


class V6Logger:
    """Thread-safe structured logger for v6 tail experiment."""

    def __init__(self, data_dir: Path = None):
        self.dir = data_dir or DATA_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._position_cache: dict[str, dict] = {}

    # ─── position table ───────────────────────────────────────────

    def _position_path(self) -> Path:
        return self.dir / "positions.jsonl"

    def load_positions(self) -> dict[str, dict]:
        """Load all positions indexed by position_id."""
        positions = {}
        path = self._position_path()
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    p = json.loads(line)
                    positions[p["position_id"]] = p
        return positions

    def save_position(self, pos: dict):
        """Write/update a position row (one per position_id, keyed)."""
        pos["last_update_time"] = _now()
        with self._lock:
            positions = self.load_positions()
            positions[pos["position_id"]] = pos
            # Rewrite entire file (small enough for paper trading)
            lines = [json.dumps(p, default=str) + "\n" for p in positions.values()]
            self._position_path().write_text("".join(lines))

    def get_position(self, position_id: str) -> Optional[dict]:
        positions = self.load_positions()
        return positions.get(position_id)

    # ─── event log ────────────────────────────────────────────────

    def _event_path(self) -> Path:
        return self.dir / "events.jsonl"

    def log_event(self, event: dict):
        """Append one event to the event log."""
        event.setdefault("event_id", _uid())
        event.setdefault("timestamp", _now())
        with self._lock:
            with open(self._event_path(), "a") as f:
                f.write(json.dumps(event, default=str) + "\n")

    def _make_event(self, event_type: str, position_id: str, market_id: str,
                    reason: str, price: float, shares: float,
                    cash_delta: float, bankroll_before: float, bankroll_after: float,
                    **extra) -> dict:
        return {
            "event_type": event_type,
            "position_id": position_id,
            "market_id": market_id,
            "reason": reason,
            "price": price,
            "shares": shares,
            "cash_delta": cash_delta,
            "bankroll_before": round(bankroll_before, 2),
            "bankroll_after": round(bankroll_after, 2),
            **extra,
        }

    def log_open(self, position_id: str, market_id: str, entry_price: float,
                 shares: float, cost: float, bankroll_before: float, bankroll_after: float,
                 model_fair_value: float = None, ev_ratio: float = None,
                 forecast_temp: float = None, threshold: float = None,
                 city: str = "", bucket_type: str = "", **extra):
        event = self._make_event("OPEN", position_id, market_id, "entry_signal",
                                 entry_price, shares, -cost, bankroll_before, bankroll_after,
                                 position_value_delta=cost, realized_pnl_delta=0.0,
                                 model_fair_value=model_fair_value, ev_ratio=ev_ratio,
                                 forecast_temp=forecast_temp, threshold=threshold,
                                 city=city, bucket_type=bucket_type, **extra)
        self.log_event(event)

    def log_partial_close(self, position_id: str, market_id: str, reason: str,
                          price: float, shares_sold: float, cash_delta: float,
                          realized_pnl_delta: float, remaining_shares: float,
                          bankroll_before: float, bankroll_after: float,
                          mfe_pct: float = None, **extra):
        event = self._make_event("PARTIAL_CLOSE", position_id, market_id, reason,
                                 price, shares_sold, cash_delta, bankroll_before, bankroll_after,
                                 realized_pnl_delta=realized_pnl_delta,
                                 remaining_shares=remaining_shares,
                                 mfe_pct=mfe_pct, **extra)
        self.log_event(event)

    def log_full_close(self, position_id: str, market_id: str, reason: str,
                       price: float, shares: float, cash_delta: float,
                       realized_pnl_delta: float, bankroll_before: float, bankroll_after: float,
                       mfe_pct: float = None, outcome: str = None, **extra):
        event = self._make_event("FULL_CLOSE", position_id, market_id, reason,
                                 price, shares, cash_delta, bankroll_before, bankroll_after,
                                 realized_pnl_delta=realized_pnl_delta,
                                 mfe_pct=mfe_pct, outcome=outcome, **extra)
        self.log_event(event)

    def log_signal(self, event_type: str, position_id: str, market_id: str,
                   reason: str, price: float = None, **extra):
        """Log non-cash events: EDGE_EXHAUSTED_SIGNAL, EDGE_EXIT_BLOCKED,
        TRAILING_ACTIVATED, TRAILING_STOP_TRIGGERED, etc."""
        event = {
            "event_type": event_type,
            "position_id": position_id,
            "market_id": market_id,
            "reason": reason,
            "price": price,
            **extra,
        }
        self.log_event(event)

    def log_upgrade(self, position_id: str, from_version: str, to_version: str,
                    price: float, shares: float, cash_delta: float,
                    bankroll_before: float, bankroll_after: float, **extra):
        event = self._make_event(f"{to_version}_UPGRADE", position_id, "", "version_upgrade",
                                 price, shares, cash_delta, bankroll_before, bankroll_after,
                                 from_version=from_version, **extra)
        self.log_event(event)

    def log_reconciliation(self, note: str, bankroll_before: float, bankroll_after: float,
                           drift: float, **extra):
        event = {
            "event_type": "RECONCILIATION",
            "timestamp": _now(),
            "bankroll_before": bankroll_before,
            "bankroll_after": bankroll_after,
            "drift": drift,
            "note": note,
            **extra,
        }
        self.log_event(event)

    # ─── market snapshots ─────────────────────────────────────────

    def _market_snapshot_path(self) -> Path:
        return self.dir / "market_snapshots.jsonl"

    def log_market_snapshot(self, snapshot: dict):
        snapshot.setdefault("timestamp", _now())
        with self._lock:
            with open(self._market_snapshot_path(), "a") as f:
                f.write(json.dumps(snapshot, default=str) + "\n")

    # ─── model snapshots ──────────────────────────────────────────

    def _model_snapshot_path(self) -> Path:
        return self.dir / "model_snapshots.jsonl"

    def log_model_snapshot(self, snapshot: dict):
        snapshot.setdefault("timestamp", _now())
        with self._lock:
            with open(self._model_snapshot_path(), "a") as f:
                f.write(json.dumps(snapshot, default=str) + "\n")

    # ─── portfolio snapshots ──────────────────────────────────────

    def _portfolio_snapshot_path(self) -> Path:
        return self.dir / "portfolio_snapshots.jsonl"

    def log_portfolio_snapshot(self, snapshot: dict):
        snapshot.setdefault("timestamp", _now())
        snapshot.setdefault("strategy_version", "tail-experiment-v6")
        with self._lock:
            with open(self._portfolio_snapshot_path(), "a") as f:
                f.write(json.dumps(snapshot, default=str) + "\n")

    # ─── helpers ──────────────────────────────────────────────────

    def load_events(self, position_id: str = None) -> list[dict]:
        """Load events, optionally filtered by position_id."""
        events = []
        path = self._event_path()
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    e = json.loads(line)
                    if position_id is None or e.get("position_id") == position_id:
                        events.append(e)
        return events

    def compute_position_pnl(self, position_id: str, current_price: float = None) -> dict:
        """Reconstruct position PnL from event log alone — source of truth."""
        events = self.load_events(position_id)
        total_cost = 0.0
        total_cash_received = 0.0  # from partial + full closes
        current_shares = 0.0
        is_open = True

        for e in events:
            if e["event_type"] == "OPEN":
                total_cost += abs(e.get("cash_delta", 0))
                current_shares += e.get("shares", 0)
            elif e["event_type"] == "PARTIAL_CLOSE":
                total_cash_received += e.get("cash_delta", 0)
                current_shares -= e.get("shares", 0)
            elif e["event_type"] == "FULL_CLOSE":
                total_cash_received += e.get("cash_delta", 0)
                current_shares -= e.get("shares", 0)
                is_open = False
            elif "_UPGRADE" in e["event_type"]:
                total_cash_received += e.get("cash_delta", 0)
                current_shares -= e.get("shares", 0)
                is_open = False

        current_value = current_shares * (current_price or 0)
        realized_pnl = total_cash_received - total_cost
        total_pnl = realized_pnl + current_value if is_open else realized_pnl

        return {
            "position_id": position_id,
            "is_open": is_open,
            "total_cost": round(total_cost, 2),
            "total_cash_received": round(total_cash_received, 2),
            "current_shares": round(current_shares, 4),
            "current_value": round(current_value, 2),
            "realized_pnl": round(realized_pnl, 2),
            "total_pnl": round(total_pnl, 2),
        }


# Singleton
logger = V6Logger()
