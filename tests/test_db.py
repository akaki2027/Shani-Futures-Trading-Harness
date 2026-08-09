"""Persistence tests.

The FTS5 cases matter most: full-text retrieval over interview answers and setup
cards is the mechanism by which "have I traded this before?" gets answered. If
it silently returns nothing, the agent degrades into a generic chatbot with no
visible error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from shani.db import Database
from shani.models import (
    InterviewAnswer,
    Order,
    OrderType,
    SetupCard,
    Side,
    Trade,
)
from shani.sessions import Session, TimeOfDay


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def make_trade(**overrides: object) -> Trade:
    defaults: dict[str, object] = {
        "symbol": "ES",
        "contract": "ESZ25",
        "side": Side.BUY,
        "quantity": 2,
        "entry_price": Decimal("5000.00"),
        "exit_price": Decimal("5004.00"),
        "entry_at": datetime(2026, 3, 10, 9, 45, tzinfo=UTC),
        "exit_at": datetime(2026, 3, 10, 10, 15, tzinfo=UTC),
        "gross_pnl": Decimal("400.00"),
        "commission": Decimal("10.00"),
        "planned_risk": Decimal("200.00"),
        "session": Session.RTH,
        "time_of_day": TimeOfDay.OPENING_DRIVE,
    }
    return Trade(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestRoundTrip:
    def test_save_and_get_preserves_the_model(self, db: Database) -> None:
        trade = make_trade()
        db.trades.save(trade)
        loaded = db.trades.get(trade.id)
        assert loaded is not None
        assert loaded.id == trade.id
        assert loaded.symbol == "ES"

    def test_decimal_survives_the_round_trip_exactly(self, db: Database) -> None:
        """Money must not degrade to float in storage.

        SQLite REAL is a double. Storing $400.00 through one and back can yield
        399.99999999999994, which then propagates into every aggregate.
        """
        trade = make_trade(gross_pnl=Decimal("400.10"), commission=Decimal("4.30"))
        db.trades.save(trade)
        loaded = db.trades.get(trade.id)
        assert loaded is not None
        assert loaded.gross_pnl == Decimal("400.10")
        assert loaded.net_pnl == Decimal("395.80")
        assert isinstance(loaded.net_pnl, Decimal)

    def test_enums_survive_the_round_trip(self, db: Database) -> None:
        trade = make_trade()
        db.trades.save(trade)
        loaded = db.trades.get(trade.id)
        assert loaded is not None
        assert loaded.session is Session.RTH
        assert loaded.time_of_day is TimeOfDay.OPENING_DRIVE
        assert loaded.side is Side.BUY

    def test_computed_fields_recompute_after_load(self, db: Database) -> None:
        trade = make_trade()
        db.trades.save(trade)
        loaded = db.trades.get(trade.id)
        assert loaded is not None
        assert loaded.r_multiple == pytest.approx(1.95)  # (400 - 10) / 200

    def test_order_round_trip(self, db: Database) -> None:
        order = Order(
            symbol="NQ", side=Side.SELL, quantity=1,
            order_type=OrderType.LIMIT, limit_price=Decimal("18000.25"),
        )
        db.orders.save(order)
        loaded = db.orders.get(order.id)
        assert loaded is not None
        assert loaded.limit_price == Decimal("18000.25")
        assert loaded.order_type is OrderType.LIMIT


class TestSoftDelete:
    def test_deleted_records_are_hidden_from_reads(self, db: Database) -> None:
        trade = make_trade()
        db.trades.save(trade)
        db.trades.delete(trade)
        assert db.trades.get(trade.id) is None
        assert db.trades.all() == []

    def test_deleted_records_still_appear_in_sync_deltas(self, db: Database) -> None:
        """A tombstone must propagate, or other devices resurrect the row."""
        before = datetime.now(UTC) - timedelta(seconds=1)
        trade = make_trade()
        db.trades.save(trade)
        db.trades.delete(trade)
        changed = db.trades.changed_since(before)
        assert len(changed) == 1
        assert changed[0].is_deleted

    def test_hard_delete_removes_the_row_entirely(self, db: Database) -> None:
        trade = make_trade()
        db.trades.save(trade)
        db.trades.delete(trade, hard=True)
        assert db.trades.changed_since(datetime(2020, 1, 1, tzinfo=UTC)) == []


class TestQueries:
    def test_filter_on_a_promoted_column(self, db: Database) -> None:
        db.trades.save(make_trade(symbol="ES"))
        db.trades.save(make_trade(symbol="NQ"))
        assert len(db.trades.where("symbol = ?", ["ES"])) == 1

    def test_filter_by_time_of_day(self, db: Database) -> None:
        """The headline futures statistic depends on this grouping."""
        db.trades.save(make_trade(time_of_day=TimeOfDay.OPENING_DRIVE))
        db.trades.save(make_trade(time_of_day=TimeOfDay.LUNCH))
        db.trades.save(make_trade(time_of_day=TimeOfDay.LUNCH))
        assert len(db.trades.where("time_of_day = ?", ["lunch"])) == 2

    def test_open_trades_query(self, db: Database) -> None:
        db.trades.save(make_trade(exit_at=None, exit_price=None))
        db.trades.save(make_trade())
        assert len(db.trades.where("exit_at IS NULL")) == 1

    def test_count(self, db: Database) -> None:
        for _ in range(3):
            db.trades.save(make_trade())
        assert db.trades.count() == 3
        assert db.trades.count("symbol = ?", ["NQ"]) == 0


class TestFullTextSearch:
    def test_finds_a_trade_by_its_interview_answer(self, db: Database) -> None:
        """This is the retrieval half of the learning loop."""
        trade = make_trade(
            interview=[
                InterviewAnswer(
                    question="What made you take this?",
                    answer="Failed auction at the overnight high, absorption on the tape",
                    answered_at=datetime.now(UTC),
                )
            ]
        )
        db.trades.save(trade)
        assert [t.id for t in db.search_trades("absorption")] == [trade.id]
        assert [t.id for t in db.search_trades("auction")] == [trade.id]

    def test_stemming_matches_word_variants(self, db: Database) -> None:
        """The porter tokenizer is why 'reversal' finds 'reversed'."""
        trade = make_trade(notes="Price reversed hard off the level")
        db.trades.save(trade)
        assert len(db.search_trades("reverse")) == 1

    def test_finds_by_tag(self, db: Database) -> None:
        db.trades.save(make_trade(tags=["breakout", "trend-day"]))
        assert len(db.search_trades("breakout")) == 1

    def test_search_ignores_unrelated_trades(self, db: Database) -> None:
        db.trades.save(make_trade(notes="Failed auction at the highs"))
        db.trades.save(make_trade(notes="Momentum continuation off the open"))
        assert len(db.search_trades("auction")) == 1

    def test_setup_cards_are_searchable(self, db: Database) -> None:
        card = SetupCard(
            name="Opening drive failure",
            slug="opening-drive-failure",
            description="Fade the first push when it fails to hold VWAP",
            trigger="Price rejects the opening range high within 15 minutes",
        )
        db.setups.save(card)
        assert [c.id for c in db.search_setups("VWAP")] == [card.id]
        assert [c.id for c in db.search_setups("rejects")] == [card.id]

    def test_updating_a_record_refreshes_its_index(self, db: Database) -> None:
        """A stale FTS row would surface the old text forever."""
        trade = make_trade(notes="original wording")
        db.trades.save(trade)
        trade.notes = "completely different wording"
        db.trades.save(trade)
        assert db.search_trades("original") == []
        assert len(db.search_trades("completely")) == 1

    def test_deleting_a_record_removes_it_from_the_index(self, db: Database) -> None:
        trade = make_trade(notes="ephemeral note")
        db.trades.save(trade)
        db.trades.delete(trade)
        assert db.search_trades("ephemeral") == []


class TestSetupCardVersioning:
    def test_a_new_version_supersedes_the_old_one(self, db: Database) -> None:
        v1 = SetupCard(name="ORB", slug="orb", description="first attempt")
        db.setups.save(v1)
        v2 = SetupCard(name="ORB", slug="orb", description="refined", version=2, supersedes=v1.id)
        db.setups.save(v2)
        assert len(db.setups.where("slug = ?", ["orb"])) == 2
        latest = db.setups.where("slug = ?", ["orb"], order_by="version DESC", limit=1)[0]
        assert latest.version == 2
        assert latest.supersedes == v1.id

    def test_small_samples_are_flagged_as_not_meaningful(self, db: Database) -> None:
        """Ten trades at a 70% win rate is noise, and must be labelled as such."""
        from uuid import uuid4

        card = SetupCard(name="Thin", slug="thin", trade_ids=[uuid4() for _ in range(10)])
        assert card.sample_size == 10
        assert not card.is_statistically_meaningful

        card.trade_ids.extend(uuid4() for _ in range(20))
        assert card.is_statistically_meaningful


class TestSyncSupport:
    def test_changed_since_returns_only_newer_records(self, db: Database) -> None:
        old = make_trade()
        db.trades.save(old)
        checkpoint = datetime.now(UTC)
        new = make_trade(symbol="NQ")
        db.trades.save(new)
        changed = db.trades.changed_since(checkpoint)
        assert [t.id for t in changed] == [new.id]

    def test_database_wide_delta_covers_every_entity(self, db: Database) -> None:
        before = datetime.now(UTC) - timedelta(seconds=1)
        db.trades.save(make_trade())
        db.setups.save(SetupCard(name="X", slug="x"))
        delta = db.changed_since(before)
        assert len(delta["trades"]) == 1
        assert len(delta["setups"]) == 1
        assert delta["orders"] == []

    def test_saving_advances_updated_at(self, db: Database) -> None:
        trade = make_trade()
        db.trades.save(trade)
        first = trade.updated_at
        trade.notes = "edited"
        db.trades.save(trade)
        assert trade.updated_at > first


class TestSchema:
    def test_reopening_an_existing_database_is_safe(self, tmp_path: Path) -> None:
        path = tmp_path / "reopen.db"
        first = Database(path)
        trade = make_trade()
        first.trades.save(trade)
        first.close()

        second = Database(path)
        assert second.trades.get(trade.id) is not None
        second.close()

    def test_refuses_a_database_from_a_newer_shani(self, tmp_path: Path) -> None:
        """Silently reading a future schema risks corrupting the journal."""
        path = tmp_path / "future.db"
        db = Database(path)
        db.conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'version'")
        db.close()
        with pytest.raises(RuntimeError, match="newer Shani"):
            Database(path)
