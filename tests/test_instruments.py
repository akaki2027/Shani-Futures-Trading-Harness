"""Contract specification tests.

The table in ``EXCHANGE_SPECS`` is transcribed from published exchange contract
specifications, **not** derived from ``shani/instruments.py``. That independence
is the whole point: a test that computes its expectation from the code under
test verifies only that the code is self-consistent, which it always is.

If you add an instrument, add its row here by reading the exchange spec sheet —
not by running the code and pasting the output.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest

from shani.instruments import (
    INSTRUMENTS,
    UnknownInstrumentError,
    front_month_code,
    get_instrument,
    parse_contract,
    root_of,
)

# root: (tick_size, tick_value_usd, multiplier_usd_per_point)
EXCHANGE_SPECS: dict[str, tuple[str, str, str]] = {
    # CME equity index
    "ES":  ("0.25",  "12.50", "50"),
    "MES": ("0.25",   "1.25",  "5"),
    "NQ":  ("0.25",   "5.00", "20"),
    "MNQ": ("0.25",   "0.50",  "2"),
    "RTY": ("0.10",   "5.00", "50"),
    "M2K": ("0.10",   "0.50",  "5"),
    "YM":  ("1",      "5.00",  "5"),
    "MYM": ("1",      "0.50",  "0.50"),
    # NYMEX energy
    "CL":  ("0.01",  "10.00", "1000"),
    "MCL": ("0.01",   "1.00",  "100"),
    "NG":  ("0.001", "10.00", "10000"),
    # COMEX metals
    "GC":  ("0.10",  "10.00", "100"),
    "MGC": ("0.10",   "1.00",  "10"),
    "SI":  ("0.005", "25.00", "5000"),
}


class TestContractSpecs:
    def test_every_shipped_instrument_is_covered_by_the_table(self) -> None:
        assert set(INSTRUMENTS) == set(EXCHANGE_SPECS), (
            "An instrument was added to INSTRUMENTS without a corresponding row "
            "in EXCHANGE_SPECS. Transcribe it from the exchange spec sheet."
        )

    @pytest.mark.parametrize("root", sorted(EXCHANGE_SPECS))
    def test_tick_size_matches_exchange(self, root: str) -> None:
        expected, _, _ = EXCHANGE_SPECS[root]
        assert INSTRUMENTS[root].tick_size == Decimal(expected)

    @pytest.mark.parametrize("root", sorted(EXCHANGE_SPECS))
    def test_multiplier_matches_exchange(self, root: str) -> None:
        _, _, expected = EXCHANGE_SPECS[root]
        assert INSTRUMENTS[root].multiplier == Decimal(expected)

    @pytest.mark.parametrize("root", sorted(EXCHANGE_SPECS))
    def test_derived_tick_value_matches_exchange(self, root: str) -> None:
        """tick_size * multiplier must equal the published tick value.

        This is the assertion that actually protects the P&L. Both inputs can
        look individually plausible while their product is wrong.
        """
        _, expected, _ = EXCHANGE_SPECS[root]
        assert INSTRUMENTS[root].tick_value == Decimal(expected)

    @pytest.mark.parametrize("root", sorted(EXCHANGE_SPECS))
    def test_micro_is_one_tenth_of_parent(self, root: str) -> None:
        inst = INSTRUMENTS[root]
        if inst.micro_of is None:
            pytest.skip(f"{root} is not a micro contract")
        parent = INSTRUMENTS[inst.micro_of]
        assert inst.multiplier * 10 == parent.multiplier
        assert inst.tick_size == parent.tick_size


class TestPnL:
    """Worked examples computed by hand from the contract specs."""

    def test_es_long_four_point_winner(self) -> None:
        """ES +4.00 points on 2 contracts = 4 * $50 * 2 = $400."""
        es = INSTRUMENTS["ES"]
        pnl = es.pnl(Decimal("5000.00"), Decimal("5004.00"), quantity=2, is_long=True)
        assert pnl == Decimal("400.00")

    def test_es_short_winner_is_positive_when_price_falls(self) -> None:
        """The classic sign bug: a short that profits must not read negative."""
        es = INSTRUMENTS["ES"]
        pnl = es.pnl(Decimal("5000.00"), Decimal("4996.00"), quantity=2, is_long=False)
        assert pnl == Decimal("400.00")

    def test_es_short_loser_is_negative_when_price_rises(self) -> None:
        es = INSTRUMENTS["ES"]
        pnl = es.pnl(Decimal("5000.00"), Decimal("5004.00"), quantity=1, is_long=False)
        assert pnl == Decimal("-200.00")

    def test_one_tick_equals_tick_value(self) -> None:
        """A single tick on one contract is exactly the published tick value."""
        for root, (tick_size, tick_value, _) in EXCHANGE_SPECS.items():
            inst = INSTRUMENTS[root]
            entry = Decimal("100")
            pnl = inst.pnl(entry, entry + Decimal(tick_size), quantity=1, is_long=True)
            assert pnl == Decimal(tick_value), f"{root}: one tick != published tick value"

    def test_cl_penny_move_is_ten_dollars(self) -> None:
        """CL is 1,000 barrels: a $0.01 move is $10.00 per contract."""
        cl = INSTRUMENTS["CL"]
        assert cl.pnl(Decimal("75.00"), Decimal("75.01"), 1, True) == Decimal("10.00")

    def test_gc_dime_move_is_ten_dollars(self) -> None:
        """GC is 100 troy oz: a $0.10 move is $10.00 per contract."""
        gc = INSTRUMENTS["GC"]
        assert gc.pnl(Decimal("2400.0"), Decimal("2400.1"), 1, True) == Decimal("10.00")

    def test_pnl_is_exact_over_many_ticks(self) -> None:
        """Decimal must not accumulate drift the way float would.

        With floats, summing 0.25 a thousand times then multiplying by 50 does
        not give exactly 12500.0. This is why the module uses Decimal.
        """
        es = INSTRUMENTS["ES"]
        entry = Decimal("5000.00")
        total = sum(
            (es.pnl(entry + i * es.tick_size, entry + (i + 1) * es.tick_size, 1, True)
             for i in range(1000)),
            start=Decimal(0),
        )
        assert total == Decimal("12500.00")


class TestCommission:
    def test_round_turn_is_both_sides(self) -> None:
        es = INSTRUMENTS["ES"]
        assert es.commission(quantity=1) == es.commission_per_side * 2

    def test_scales_with_contracts(self) -> None:
        es = INSTRUMENTS["ES"]
        assert es.commission(quantity=3) == es.commission_per_side * 6

    def test_single_side(self) -> None:
        es = INSTRUMENTS["ES"]
        assert es.commission(quantity=2, sides=1) == es.commission_per_side * 2


class TestTickRounding:
    def test_snaps_off_tick_price_to_nearest_valid_tick(self) -> None:
        """An LLM will propose 5001.13. The exchange would reject it."""
        es = INSTRUMENTS["ES"]
        assert es.round_to_tick(Decimal("5001.13")) == Decimal("5001.25")
        assert es.round_to_tick(Decimal("5001.10")) == Decimal("5001.00")

    def test_valid_price_is_unchanged(self) -> None:
        es = INSTRUMENTS["ES"]
        assert es.round_to_tick(Decimal("5001.25")) == Decimal("5001.25")

    def test_is_on_tick_detects_invalid_prices(self) -> None:
        es = INSTRUMENTS["ES"]
        assert es.is_on_tick(Decimal("5001.25"))
        assert not es.is_on_tick(Decimal("5001.13"))

    def test_rounding_respects_instrument_granularity(self) -> None:
        ng = INSTRUMENTS["NG"]  # 0.001 ticks
        assert ng.round_to_tick(Decimal("3.14159")) == Decimal("3.142")


class TestTickConversions:
    def test_ticks_to_dollars_scales_with_quantity(self) -> None:
        es = INSTRUMENTS["ES"]
        assert es.ticks_to_dollars(Decimal(4), quantity=2) == Decimal("100.00")

    def test_dollars_to_ticks_is_the_inverse(self) -> None:
        es = INSTRUMENTS["ES"]
        assert es.dollars_to_ticks(Decimal("100.00"), quantity=2) == Decimal(4)

    def test_ticks_between_is_signed(self) -> None:
        es = INSTRUMENTS["ES"]
        assert es.ticks_between(Decimal("5000"), Decimal("5001")) == Decimal(4)
        assert es.ticks_between(Decimal("5001"), Decimal("5000")) == Decimal(-4)


class TestSymbolParsing:
    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("ES", "ES"),
            ("es", "ES"),
            ("ESZ5", "ES"),
            ("ESZ25", "ES"),
            ("ES1!", "ES"),
            ("CME:ES1!", "ES"),
            ("CME_MINI:ES1!", "ES"),
            ("NYMEX:CL1!", "CL"),
            ("COMEX:GC1!", "GC"),
            ("MNQ", "MNQ"),
            ("MNQH5", "MNQ"),
        ],
    )
    def test_root_extraction(self, symbol: str, expected: str) -> None:
        assert root_of(symbol) == expected

    def test_lookup_accepts_every_symbol_form(self) -> None:
        for form in ("ES", "ESZ5", "ES1!", "CME:ES1!"):
            assert get_instrument(form).root == "ES"

    def test_unknown_root_raises_rather_than_guessing(self) -> None:
        """Guessing a tick size would silently corrupt every downstream number."""
        with pytest.raises(UnknownInstrumentError) as exc:
            get_instrument("ZZZ")
        assert "will not guess" in str(exc.value)

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("ESZ25", ("ES", 12, 2025)),
            ("ESH26", ("ES", 3, 2026)),
            ("CLF26", ("CL", 1, 2026)),
            ("GCG26", ("GC", 2, 2026)),
        ],
    )
    def test_parse_dated_contract(self, symbol: str, expected: tuple[str, int, int]) -> None:
        assert parse_contract(symbol) == expected

    def test_continuous_symbols_have_no_expiry(self) -> None:
        assert parse_contract("ES1!") is None
        assert parse_contract("CME:ES1!") is None
        assert parse_contract("ES") is None

    def test_single_digit_year_resolves_to_current_decade(self) -> None:
        parsed = parse_contract("ESZ5")
        assert parsed is not None
        root, month, year = parsed
        assert (root, month) == ("ES", 12)
        assert 2020 <= year <= 2039


class TestSessionWindows:
    def test_rth_does_not_wrap_midnight(self) -> None:
        assert not INSTRUMENTS["ES"].rth.wraps_midnight

    def test_globex_wraps_midnight(self) -> None:
        """18:00 → 17:00 next day. Getting this wrong breaks every overnight trade."""
        assert INSTRUMENTS["ES"].globex.wraps_midnight

    def test_rth_containment(self) -> None:
        rth = INSTRUMENTS["ES"].rth
        assert rth.contains(time(9, 30))       # start inclusive
        assert rth.contains(time(12, 0))
        assert not rth.contains(time(16, 0))   # end exclusive
        assert not rth.contains(time(9, 29))
        assert not rth.contains(time(3, 0))

    def test_globex_containment_across_midnight(self) -> None:
        globex = INSTRUMENTS["ES"].globex
        assert globex.contains(time(19, 0))    # evening, after open
        assert globex.contains(time(3, 0))     # overnight
        assert globex.contains(time(10, 0))    # next day
        assert not globex.contains(time(17, 30))  # maintenance halt

    def test_metals_rth_starts_earlier_than_equities(self) -> None:
        """GC opens 08:20 ET; using the equity 09:30 window would misclassify."""
        assert INSTRUMENTS["GC"].rth.contains(time(8, 30))
        assert not INSTRUMENTS["ES"].rth.contains(time(8, 30))


class TestFrontMonth:
    def test_equity_index_uses_quarterly_cycle(self) -> None:
        assert front_month_code(INSTRUMENTS["ES"], date(2026, 1, 15)) == "H"
        assert front_month_code(INSTRUMENTS["ES"], date(2026, 4, 1)) == "M"
        assert front_month_code(INSTRUMENTS["ES"], date(2026, 10, 1)) == "Z"

    def test_wraps_to_next_year_past_the_last_listed_month(self) -> None:
        """After December, the front month is next year's March."""
        assert front_month_code(INSTRUMENTS["ES"], date(2026, 12, 20)) == "Z"

    def test_crude_lists_every_month(self) -> None:
        assert front_month_code(INSTRUMENTS["CL"], date(2026, 5, 1)) == "K"

    def test_gold_skips_unlisted_months(self) -> None:
        """GC has no March contract; a March date must roll to April."""
        assert front_month_code(INSTRUMENTS["GC"], date(2026, 3, 1)) == "J"
