"""SQLite persistence.

**Storage shape.** Each entity table holds a ``data`` column containing the full
Pydantic model as JSON, plus a small number of real columns for the fields we
filter, sort, or aggregate on. The JSON is the source of truth; the columns are
indexes that happen to be readable.

That is a deliberate trade. A fully normalised schema would mean a migration
every time a model gains a field, and during alpha the models will churn. The
cost is that a field must be promoted to a real column before you can query it
efficiently — which is a small, obvious, localised change. Promotion is cheap;
denormalising a live journal is not.

**Full-text search** uses FTS5, which is what makes the playbook retrievable —
"have I traded this before?" is a text question over interview answers and setup
descriptions. FTS rows are maintained explicitly in the repository's save path
rather than by triggers, because a trigger cannot see inside a JSON blob without
becoming unreadable.

**Sync-readiness.** Every table carries ``updated_at`` and ``deleted_at``, and
:meth:`Repository.changed_since` implements the delta query a future mobile
client needs. Deletes are soft by default — see :mod:`shani.models`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel

from shani.models import (
    AuditEvent,
    Fill,
    Order,
    Position,
    Proposal,
    SetupCard,
    Signal,
    SyncRecord,
    Trade,
)

__all__ = ["Database", "Repository", "connect"]

T = TypeVar("T", bound=SyncRecord)

SCHEMA_VERSION = 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Orders ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    status          TEXT NOT NULL,
    broker          TEXT NOT NULL,
    parent_order_id TEXT,
    oco_group       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    deleted_at      TEXT,
    data            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_orders_symbol  ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_oco     ON orders(oco_group);
CREATE INDEX IF NOT EXISTS idx_orders_updated ON orders(updated_at);

-- Fills ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fills (
    id         TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    filled_at  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_order   ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_updated ON fills(updated_at);

-- Positions ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    id         TEXT PRIMARY KEY,
    symbol     TEXT NOT NULL UNIQUE,
    quantity   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_updated ON positions(updated_at);

-- Signals --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    received_at TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_received ON signals(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_updated  ON signals(updated_at);

-- Proposals ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proposals (
    id         TEXT PRIMARY KEY,
    signal_id  TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    decision   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_signal   ON proposals(signal_id);
CREATE INDEX IF NOT EXISTS idx_proposals_decision ON proposals(decision);
CREATE INDEX IF NOT EXISTS idx_proposals_updated  ON proposals(updated_at);

-- Trades ---------------------------------------------------------------------
-- The journal. Columns promoted here are exactly the ones the statistics layer
-- groups by, because those aggregations run on every portal page load.
CREATE TABLE IF NOT EXISTS trades (
    id                TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    contract          TEXT,
    side              TEXT NOT NULL,
    entry_at          TEXT NOT NULL,
    exit_at           TEXT,
    net_pnl           TEXT,
    r_multiple        REAL,
    session           TEXT,
    time_of_day       TEXT,
    setup_card_id     TEXT,
    followed_playbook INTEGER NOT NULL DEFAULT 0,
    broker            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    deleted_at        TEXT,
    data              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_entry    ON trades(entry_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol   ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_setup    ON trades(setup_card_id);
CREATE INDEX IF NOT EXISTS idx_trades_tod      ON trades(time_of_day);
CREATE INDEX IF NOT EXISTS idx_trades_session  ON trades(session);
CREATE INDEX IF NOT EXISTS idx_trades_open     ON trades(exit_at) WHERE exit_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_trades_updated  ON trades(updated_at);

-- Setup cards ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS setup_cards (
    id         TEXT PRIMARY KEY,
    slug       TEXT NOT NULL,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    supersedes TEXT,
    validated  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_setups_slug    ON setup_cards(slug);
CREATE INDEX IF NOT EXISTS idx_setups_updated ON setup_cards(updated_at);

-- Audit log ------------------------------------------------------------------
-- Append-only. Nothing in normal operation updates or deletes from this table.
CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL,
    signal_id   TEXT,
    proposal_id TEXT,
    order_id    TEXT,
    trade_id    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_type    ON audit_events(event_type);

-- Full-text search -----------------------------------------------------------
-- The retrieval substrate for "have I traded this before?". Contentless FTS5
-- tables keyed by record id; rows are maintained by Repository.save().
CREATE VIRTUAL TABLE IF NOT EXISTS trades_fts USING fts5(
    id UNINDEXED, symbol, notes, interview, tags, tokenize = 'porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS setups_fts USING fts5(
    id UNINDEXED, name, description, trigger_text, context, invalidation,
    tokenize = 'porter unicode61'
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the pragmas this workload needs.

    WAL matters here specifically: the FastAPI server reads while the broker
    writes, and the default rollback journal makes those block each other.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), detect_types=0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class Repository(Generic[T]):
    """Typed CRUD over one entity table.

    ``columns`` maps real column names to a callable extracting that value from
    the model. Everything else rides along in ``data`` as JSON.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        table: str,
        model: type[T],
        columns: dict[str, Any],
    ) -> None:
        self.conn = conn
        self.table = table
        self.model = model
        self.columns = columns

    # ── writes ───────────────────────────────────────────────────────────────

    def save(self, record: T) -> T:
        """Insert or replace, refreshing ``updated_at`` and any FTS row."""
        record.touch()
        cols = ["id", "created_at", "updated_at", "deleted_at", "data"]
        vals: list[Any] = [
            str(record.id),
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
            record.deleted_at.isoformat() if record.deleted_at else None,
            record.model_dump_json(),
        ]
        for name, extract in self.columns.items():
            cols.append(name)
            vals.append(extract(record))

        placeholders = ", ".join("?" for _ in cols)
        self.conn.execute(
            f"INSERT OR REPLACE INTO {self.table} ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self._index(record)
        return record

    def save_many(self, records: Sequence[T]) -> None:
        with self.transaction():
            for r in records:
                self.save(r)

    def _index(self, record: T) -> None:
        """Refresh the FTS row for this record, if its table has one."""
        if isinstance(record, Trade):
            self.conn.execute("DELETE FROM trades_fts WHERE id = ?", (str(record.id),))
            if not record.is_deleted:
                interview = " ".join(
                    f"{a.question} {a.answer}" for a in record.interview if a.answer
                )
                self.conn.execute(
                    "INSERT INTO trades_fts (id, symbol, notes, interview, tags) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(record.id), record.symbol, record.notes, interview,
                     " ".join(record.tags)),
                )
        elif isinstance(record, SetupCard):
            self.conn.execute("DELETE FROM setups_fts WHERE id = ?", (str(record.id),))
            if not record.is_deleted:
                self.conn.execute(
                    "INSERT INTO setups_fts "
                    "(id, name, description, trigger_text, context, invalidation) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(record.id), record.name, record.description, record.trigger,
                     record.context, record.invalidation),
                )

    def delete(self, record: T, *, hard: bool = False) -> None:
        """Soft-delete by default so the deletion can propagate to other devices."""
        if hard:
            self.conn.execute(f"DELETE FROM {self.table} WHERE id = ?", (str(record.id),))
            self.conn.execute("DELETE FROM trades_fts WHERE id = ?", (str(record.id),))
            self.conn.execute("DELETE FROM setups_fts WHERE id = ?", (str(record.id),))
            return
        record.soft_delete()
        self.save(record)

    # ── reads ────────────────────────────────────────────────────────────────

    def _parse(self, row: sqlite3.Row) -> T:
        return self.model.model_validate_json(row["data"])

    def get(self, record_id: UUID | str) -> T | None:
        row = self.conn.execute(
            f"SELECT data FROM {self.table} WHERE id = ? AND deleted_at IS NULL",
            (str(record_id),),
        ).fetchone()
        return self._parse(row) if row else None

    def all(self, *, include_deleted: bool = False, limit: int | None = None) -> list[T]:
        sql = f"SELECT data FROM {self.table}"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._parse(r) for r in self.conn.execute(sql)]

    def where(
        self,
        clause: str,
        params: Sequence[Any] = (),
        *,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[T]:
        """Query on promoted columns.

        ``clause`` and ``order_by`` are raw SQL fragments and are **not** for
        untrusted input — they are called only with literals defined in this
        codebase. Values always go through ``params``.

        Ordering is a separate argument rather than something you append to
        ``clause``, because the clause is wrapped in parentheses and an
        ``ORDER BY`` smuggled inside becomes a syntax error.
        """
        sql = f"SELECT data FROM {self.table} WHERE deleted_at IS NULL AND ({clause})"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._parse(r) for r in self.conn.execute(sql, tuple(params))]

    def count(self, clause: str = "1=1", params: Sequence[Any] = ()) -> int:
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {self.table} WHERE deleted_at IS NULL AND ({clause})",
            tuple(params),
        ).fetchone()
        return int(row["n"])

    def changed_since(self, since: datetime) -> list[T]:
        """Delta query for sync — includes tombstones, which is the point.

        A client syncing from ``since`` must learn about deletions as well as
        creations, so this deliberately does not filter ``deleted_at``.
        """
        rows = self.conn.execute(
            f"SELECT data FROM {self.table} WHERE updated_at > ? ORDER BY updated_at",
            (since.isoformat(),),
        )
        return [self._parse(r) for r in rows]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class Database:
    """Owns the connection and exposes one repository per entity."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.conn = connect(self.path)
        self._migrate()

        self.orders: Repository[Order] = Repository(
            self.conn, "orders", Order,
            {
                "symbol": lambda o: o.symbol,
                "side": lambda o: o.side.value,
                "status": lambda o: o.status.value,
                "broker": lambda o: o.broker,
                "parent_order_id": lambda o: str(o.parent_order_id) if o.parent_order_id else None,
                "oco_group": lambda o: str(o.oco_group) if o.oco_group else None,
            },
        )
        self.fills: Repository[Fill] = Repository(
            self.conn, "fills", Fill,
            {
                "order_id": lambda f: str(f.order_id),
                "symbol": lambda f: f.symbol,
                "filled_at": lambda f: f.filled_at.isoformat(),
            },
        )
        self.positions: Repository[Position] = Repository(
            self.conn, "positions", Position,
            {"symbol": lambda p: p.symbol, "quantity": lambda p: p.quantity},
        )
        self.signals: Repository[Signal] = Repository(
            self.conn, "signals", Signal,
            {
                "source": lambda s: s.source.value,
                "symbol": lambda s: s.symbol,
                "received_at": lambda s: s.received_at.isoformat(),
            },
        )
        self.proposals: Repository[Proposal] = Repository(
            self.conn, "proposals", Proposal,
            {
                "signal_id": lambda p: str(p.signal_id),
                "symbol": lambda p: p.symbol,
                "decision": lambda p: p.decision.value,
            },
        )
        self.trades: Repository[Trade] = Repository(
            self.conn, "trades", Trade,
            {
                "symbol": lambda t: t.symbol,
                "contract": lambda t: t.contract,
                "side": lambda t: t.side.value,
                "entry_at": lambda t: t.entry_at.isoformat(),
                "exit_at": lambda t: _iso(t.exit_at),
                # Stored as TEXT to preserve Decimal exactness — SQLite REAL is
                # a float, and money must not round-trip through one.
                "net_pnl": lambda t: str(t.net_pnl),
                "r_multiple": lambda t: t.r_multiple,
                "session": lambda t: t.session.value if t.session else None,
                "time_of_day": lambda t: t.time_of_day.value if t.time_of_day else None,
                "setup_card_id": lambda t: str(t.setup_card_id) if t.setup_card_id else None,
                "followed_playbook": lambda t: int(t.followed_playbook),
                "broker": lambda t: t.broker,
            },
        )
        self.setups: Repository[SetupCard] = Repository(
            self.conn, "setup_cards", SetupCard,
            {
                "slug": lambda s: s.slug,
                "name": lambda s: s.name,
                "version": lambda s: s.version,
                "supersedes": lambda s: str(s.supersedes) if s.supersedes else None,
                "validated": lambda s: int(s.validated),
            },
        )
        self.audit: Repository[AuditEvent] = Repository(
            self.conn, "audit_events", AuditEvent,
            {
                "event_type": lambda e: e.event_type,
                "severity": lambda e: e.severity,
                "signal_id": lambda e: str(e.signal_id) if e.signal_id else None,
                "proposal_id": lambda e: str(e.proposal_id) if e.proposal_id else None,
                "order_id": lambda e: str(e.order_id) if e.order_id else None,
                "trade_id": lambda e: str(e.trade_id) if e.trade_id else None,
            },
        )

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(row["value"]) > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database at {self.path} was written by a newer Shani "
                f"(schema v{row['value']} > v{SCHEMA_VERSION}). Upgrade Shani "
                f"rather than downgrading the database."
            )

    # ── full-text search ─────────────────────────────────────────────────────

    def search_trades(self, query: str, limit: int = 20) -> list[Trade]:
        """Free-text search over trade notes, interview answers, and tags."""
        rows = self.conn.execute(
            "SELECT t.data FROM trades_fts f "
            "JOIN trades t ON t.id = f.id "
            "WHERE trades_fts MATCH ? AND t.deleted_at IS NULL "
            "ORDER BY rank LIMIT ?",
            (query, limit),
        )
        return [Trade.model_validate_json(r["data"]) for r in rows]

    def search_setups(self, query: str, limit: int = 10) -> list[SetupCard]:
        """Free-text search over setup cards — the retrieval half of the loop."""
        rows = self.conn.execute(
            "SELECT s.data FROM setups_fts f "
            "JOIN setup_cards s ON s.id = f.id "
            "WHERE setups_fts MATCH ? AND s.deleted_at IS NULL "
            "ORDER BY rank LIMIT ?",
            (query, limit),
        )
        return [SetupCard.model_validate_json(r["data"]) for r in rows]

    def changed_since(self, since: datetime) -> dict[str, list[dict[str, Any]]]:
        """Everything modified since ``since``, for a future sync client."""
        return {
            name: [json.loads(r.model_dump_json()) for r in repo.changed_since(since)]
            for name, repo in (
                ("orders", self.orders), ("fills", self.fills),
                ("positions", self.positions), ("signals", self.signals),
                ("proposals", self.proposals), ("trades", self.trades),
                ("setups", self.setups),
            )
        }

    def now(self) -> datetime:
        return datetime.now(UTC)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
