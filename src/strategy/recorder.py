
"""SQLite recorder ?  + ??/??"""

import json
import sqlite3
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _app_dir():
    """Application directory (where .env and data/ live)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


DB_DIR = _app_dir() / "data"
DB_PATH = DB_DIR / "trades.db"


class Recorder:
    """? SQLite"""

    def __init__(self, db_path: str | Path = DB_PATH) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._connect()

    def _connect(self) -> None:
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        cur = self._conn.cursor()

        # Create tables without decision_id first (for existing DB compatibility)
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                direction   TEXT NOT NULL,
                strategy    TEXT NOT NULL DEFAULT '',
                price       REAL,
                volume      REAL DEFAULT 0.01,
                order_id    INTEGER,
                reason      TEXT,
                indicators  TEXT,
                cooldown    TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                open_time       TEXT NOT NULL,
                close_time      TEXT,
                direction       TEXT NOT NULL,
                volume          REAL DEFAULT 0.01,
                open_price      REAL,
                close_price     REAL,
                profit          REAL,
                profit_pips     REAL,
                strategy        TEXT NOT NULL DEFAULT '',
                open_order_id   INTEGER,
                close_order_id  INTEGER,
                sl              REAL,
                tp              REAL,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_open  ON trades(open_time);
        """)
        self._conn.commit()

        # Migration: add decision_id column if missing (existing DB)
        try:
            self._conn.execute("SELECT decision_id FROM trades LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE trades ADD COLUMN decision_id INTEGER REFERENCES decisions(id)")
            self._conn.commit()

        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_decision ON trades(decision_id)")
        self._conn.commit()

    # ------------------------------------------------------------------
    # ?
    # ------------------------------------------------------------------

    def record_decision(
        self,
        direction: str,
        strategy: str = "",
        *,
        price: float | None = None,
        volume: float = 0.01,
        order_id: int | None = None,
        reason: str = "",
        indicators: dict | None = None,
        cooldown_until: str | None = None,
    ) -> int:
        if not self._conn:
            self._connect()
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            """INSERT INTO decisions
               (timestamp, direction, strategy, price, volume, order_id, reason, indicators, cooldown)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                direction,
                strategy,
                price,
                volume,
                order_id,
                reason,
                json.dumps(indicators, ensure_ascii=False) if indicators else None,
                cooldown_until,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    # ------------------------------------------------------------------
    # ??
    # ------------------------------------------------------------------

    def record_trade(
        self,
        direction: str,
        open_price: float,
        volume: float,
        *,
        strategy: str = "",
        open_order_id: int | None = None,
        decision_id: int | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> int:
        if not self._conn:
            self._connect()
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            """INSERT INTO trades
               (open_time, direction, volume, open_price, strategy, open_order_id, decision_id, sl, tp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, direction, volume, open_price, strategy, open_order_id, decision_id, sl, tp),
        )
        self._conn.commit()
        return cur.lastrowid

    def close_trade(self, trade_id: int, close_price: float, profit: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE trades
               SET close_time=?, close_price=?, profit=?
               WHERE id=?""",
            (now, close_price, round(profit, 2), trade_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # ??
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """??"""
        if not self._conn:
            self._connect()
        cur = self._conn.execute(
            """SELECT
                   COUNT(*) as total,
                   SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN profit < 0 THEN 1 ELSE 0 END) as losses,
                   COALESCE(SUM(profit), 0) as total_profit,
                   COALESCE(AVG(CASE WHEN profit > 0 THEN profit END), 0) as avg_win,
                   COALESCE(AVG(CASE WHEN profit < 0 THEN profit END), 0) as avg_loss
               FROM trades WHERE close_time IS NOT NULL"""
        )
        row = cur.fetchone()
        total = row["total"] or 0
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        win_rate = round(wins / total * 100, 1) if total > 0 else 0.0
        avg_win = row["avg_win"] or 0.0
        avg_loss = abs(row["avg_loss"]) or 1.0
        profit_ratio = round(avg_win / avg_loss, 2) if avg_win > 0 and avg_loss > 0 else 0.0

        # ?? 20
        cur2 = self._conn.execute(
            "SELECT timestamp, direction, price, reason, indicators FROM decisions ORDER BY id DESC LIMIT 20"
        )
        recent_decisions = [dict(r) for r in cur2.fetchall()]

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_profit": round(row["total_profit"] or 0.0, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(abs(row["avg_loss"] or 0.0), 2),
            "profit_ratio": profit_ratio,
            "recent_decisions": recent_decisions,
        }

    def get_recent_trades(self, limit: int = 10) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
