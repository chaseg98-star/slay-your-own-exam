"""SQLite persistence: predictions, proposals, trades, cost basis, settings."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .models import Decision, Fill, Prediction, RiskMode

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    product_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    horizon_hours REAL NOT NULL,
    thesis TEXT NOT NULL,
    decision_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    prediction_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    action TEXT NOT NULL,
    quote_size REAL NOT NULL,
    base_size REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    tech_json TEXT NOT NULL DEFAULT '{}',
    resolution TEXT,
    executed_order_id TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    order_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    prediction_id TEXT,
    product_id TEXT NOT NULL,
    side TEXT NOT NULL,
    base_size REAL NOT NULL,
    quote_size REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    realized_pnl REAL,
    counts_toward_cap INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS positions (
    product_id TEXT PRIMARY KEY,
    base_qty REAL NOT NULL,
    cost REAL NOT NULL,
    opened_at REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_product ON trades(product_id, created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at);
"""


def _utc_midnight_ts(now: float) -> float:
    day = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).date()
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp()


class Store:
    def __init__(self, path: Path | str):
        if isinstance(path, str) and path != ":memory:":
            path = Path(path)
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- settings ---------------------------------------------------------

    def _get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_risk_mode(self, default: RiskMode) -> RiskMode:
        raw = self._get_meta("risk_mode")
        return RiskMode(raw) if raw else default

    def set_risk_mode(self, mode: RiskMode) -> None:
        self._set_meta("risk_mode", mode.value)

    def trading_enabled(self) -> bool:
        return self._get_meta("trading_enabled", "1") == "1"

    def disabled_reason(self) -> str | None:
        return self._get_meta("disabled_reason") or None

    def set_trading_enabled(self, enabled: bool, reason: str = "") -> None:
        self._set_meta("trading_enabled", "1" if enabled else "0")
        self._set_meta("disabled_reason", "" if enabled else reason)

    def record_breaker_trip(self, now: float) -> None:
        self._set_meta("breaker_tripped_at", repr(now))

    def breaker_tripped_today(self, now: float) -> bool:
        """True if the daily-loss circuit breaker tripped in the current UTC day."""
        raw = self._get_meta("breaker_tripped_at")
        if not raw:
            return False
        return float(raw) >= _utc_midnight_ts(now)

    # -- predictions ------------------------------------------------------

    def record_prediction(self, pred: Prediction, decision: Decision) -> None:
        self._conn.execute(
            "INSERT INTO predictions(id, created_at, product_id, direction, confidence, "
            "horizon_hours, thesis, decision_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                pred.id,
                pred.created_at,
                pred.product_id,
                pred.direction.value,
                pred.confidence,
                pred.horizon_hours,
                pred.thesis,
                json.dumps(decision.as_dict()),
            ),
        )
        self._conn.commit()

    def get_prediction(self, prediction_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM predictions WHERE id = ?", (prediction_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["decision"] = json.loads(d.pop("decision_json"))
        return d

    def recent_predictions(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM predictions ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["decision"] = json.loads(d.pop("decision_json"))
            out.append(d)
        return out

    # -- proposals (two-phase execution) ---------------------------------

    def create_proposal(
        self,
        *,
        prediction_id: str,
        product_id: str,
        action: str,
        quote_size: float,
        base_size: float,
        ttl_minutes: float,
        tech: dict,
        now: float,
    ) -> dict:
        proposal_id = uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO proposals(id, created_at, expires_at, prediction_id, product_id, "
            "action, quote_size, base_size, status, tech_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                now,
                now + ttl_minutes * 60.0,
                prediction_id,
                product_id,
                action,
                quote_size,
                base_size,
                "proposed",
                json.dumps(tech),
            ),
        )
        self._conn.commit()
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tech"] = json.loads(d.pop("tech_json"))
        return d

    def pending_proposals(self, now: float) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE status = 'proposed' ORDER BY created_at"
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["tech"] = json.loads(d.pop("tech_json"))
            d["expired"] = now > d["expires_at"]
            out.append(d)
        return out

    def resolve_proposal(
        self, proposal_id: str, status: str, resolution: str, executed_order_id: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE proposals SET status = ?, resolution = ?, executed_order_id = ? WHERE id = ?",
            (status, resolution, executed_order_id, proposal_id),
        )
        self._conn.commit()

    def expire_stale_proposals(self, now: float) -> int:
        cur = self._conn.execute(
            "UPDATE proposals SET status = 'expired', resolution = 'TTL elapsed before confirmation' "
            "WHERE status = 'proposed' AND expires_at < ?",
            (now,),
        )
        self._conn.commit()
        return cur.rowcount

    # -- trades -----------------------------------------------------------

    def record_trade(
        self,
        fill: Fill,
        prediction_id: str | None,
        realized_pnl: float | None,
        counts_toward_cap: bool = True,
        note: str = "",
        now: float | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO trades(order_id, created_at, prediction_id, product_id, side, "
            "base_size, quote_size, price, fee, realized_pnl, counts_toward_cap, note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fill.order_id,
                now if now is not None else time.time(),
                prediction_id,
                fill.product_id,
                fill.side,
                fill.base_size,
                fill.quote_size,
                fill.price,
                fill.fee,
                realized_pnl,
                1 if counts_toward_cap else 0,
                note,
            ),
        )
        self._conn.commit()

    def get_trade(self, order_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM trades WHERE order_id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def trades_today(self, now: float | None = None, capped_only: bool = True) -> int:
        now = now if now is not None else time.time()
        query = "SELECT COUNT(*) AS n FROM trades WHERE created_at >= ?"
        if capped_only:
            query += " AND counts_toward_cap = 1"
        return int(self._conn.execute(query, (_utc_midnight_ts(now),)).fetchone()["n"])

    def realized_pnl_today(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM trades "
            "WHERE created_at >= ? AND realized_pnl IS NOT NULL",
            (_utc_midnight_ts(now),),
        ).fetchone()
        return float(row["pnl"])

    def last_trade_ts(self, product_id: str) -> float | None:
        row = self._conn.execute(
            "SELECT MAX(created_at) AS ts FROM trades WHERE product_id = ?", (product_id,)
        ).fetchone()
        return float(row["ts"]) if row and row["ts"] is not None else None

    def recent_trades(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- cost basis -------------------------------------------------------

    def get_position(self, product_id: str) -> tuple[float, float]:
        """Returns (base_qty, total_cost) tracked for the product."""
        row = self._conn.execute(
            "SELECT base_qty, cost FROM positions WHERE product_id = ?", (product_id,)
        ).fetchone()
        return (float(row["base_qty"]), float(row["cost"])) if row else (0.0, 0.0)

    def position_opened_at(self, product_id: str) -> float | None:
        row = self._conn.execute(
            "SELECT opened_at FROM positions WHERE product_id = ? AND base_qty > 0", (product_id,)
        ).fetchone()
        return float(row["opened_at"]) if row and row["opened_at"] is not None else None

    def _put_position(
        self, product_id: str, base_qty: float, cost: float, opened_at: float | None
    ) -> None:
        base_qty = max(0.0, base_qty)
        if base_qty == 0.0:
            opened_at = None  # position closed; next open restarts the clock
        self._conn.execute(
            "INSERT INTO positions(product_id, base_qty, cost, opened_at) VALUES(?,?,?,?) "
            "ON CONFLICT(product_id) DO UPDATE SET base_qty = excluded.base_qty, "
            "cost = excluded.cost, opened_at = excluded.opened_at",
            (product_id, base_qty, max(0.0, cost), opened_at),
        )
        self._conn.commit()

    def _current_opened_at(self, product_id: str) -> float | None:
        row = self._conn.execute(
            "SELECT opened_at FROM positions WHERE product_id = ?", (product_id,)
        ).fetchone()
        return float(row["opened_at"]) if row and row["opened_at"] is not None else None

    def apply_buy(
        self, product_id: str, base_size: float, quote_spent: float, now: float | None = None
    ) -> None:
        qty, cost = self.get_position(product_id)
        opened_at = self._current_opened_at(product_id)
        if qty <= 0 or opened_at is None:
            opened_at = now if now is not None else time.time()
        self._put_position(product_id, qty + base_size, cost + quote_spent, opened_at)

    def apply_sell(self, product_id: str, base_size: float, proceeds: float) -> float | None:
        """Reduce the tracked position; returns realized P&L for the tracked part
        (None when the sold coins had no tracked cost basis)."""
        qty, cost = self.get_position(product_id)
        if qty <= 0 or base_size <= 0:
            return None
        tracked = min(qty, base_size)
        avg_cost = cost / qty
        realized = (proceeds / base_size) * tracked - avg_cost * tracked
        remaining = qty - tracked
        # Exchange increments round sell sizes down, so a "full" exit can leave
        # a sub-increment sliver; treat it as closed so opened_at resets and the
        # max-hold clock doesn't leak onto the next position.
        if remaining <= max(1e-9, qty * 1e-6):
            remaining = 0.0
        self._put_position(
            product_id, remaining, cost - avg_cost * tracked if remaining else 0.0,
            self._current_opened_at(product_id),
        )
        return realized

    def adopt_position(
        self, product_id: str, extra_base: float, price: float, now: float | None = None
    ) -> None:
        """Fold pre-existing (untracked) holdings into the cost basis at today's price."""
        qty, cost = self.get_position(product_id)
        opened_at = self._current_opened_at(product_id)
        if qty <= 0 or opened_at is None:
            opened_at = now if now is not None else time.time()
        self._put_position(product_id, qty + extra_base, cost + extra_base * price, opened_at)

    def close_dust(self, product_id: str, price: float, dust_usd: float) -> None:
        """Zero out a position whose remaining value is dust, so the max-hold
        clock resets. Sub-increment residue after a quantized 'full' exit can be
        larger than apply_sell's sliver threshold; this catches it by notional."""
        qty, _ = self.get_position(product_id)
        if 0 < qty and qty * price < dust_usd:
            self._put_position(product_id, 0.0, 0.0, None)

    def reconcile_down(self, product_id: str, actual_qty: float) -> None:
        """Shrink tracked position to what the exchange actually holds (funds were
        sold outside the agent); cost shrinks proportionally, no P&L recorded."""
        qty, cost = self.get_position(product_id)
        if qty <= 0 or actual_qty >= qty:
            return
        ratio = max(0.0, actual_qty) / qty
        self._put_position(product_id, actual_qty, cost * ratio, self._current_opened_at(product_id))
