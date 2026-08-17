"""Market drivers — hard data, not headlines.

The news desk reads what journalists wrote *about* an event. This reads the
event: a number, its prior, and the change between them. For futures that
distinction matters, because the highest-signal inputs arrive on a published
calendar with a consensus attached, and a model's read of someone's prose about
them is strictly worse than the figure itself.

Two sources here, both free and neither needing a key:

**CFTC Commitments of Traders.** Weekly positioning per contract, split into
commercials (hedgers, usually the informed money) and non-commercials (large
speculators, usually trend followers). This is a *futures-native* dataset — it
exists because these are futures markets — and nothing in the equity world has
an equivalent.

**US Treasury par yield curve.** Daily. Drives ES and NQ through the discount
rate and GC through real yields, in opposite directions, which is exactly the
per-market divergence a single blended sentiment score cannot express.

## Mapping is the part that has to be right

A driver attached to the wrong market is worse than no driver, because it is
confident and specific. Every reading here is tied to an explicit CFTC contract
code rather than matched on a name substring — "GOLD" alone matches four
different contracts, and "S&P 500" matches both the full-size and the E-mini.
The codes are recorded in :data:`COT_CONTRACTS` with the exchange spelled out.

## What a lean means here

Direction comes from **change**, not level. Specs adding longs is a momentum
signal; specs *being* long is a state that can persist for months. Where a level
matters — a crowded position — it is reported as a separate note rather than
folded into the direction, because "everyone is already long" argues both ways
depending on your horizon and pretending otherwise would be false precision.
"""

from __future__ import annotations

import os
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from xml.etree import ElementTree

import httpx

from shani.news.base import Lean

__all__ = ["COT_CONTRACTS", "Driver", "DriversService"]

_UA = {"User-Agent": "shani/0.1 (personal trading journal)"}

#: CFTC contract market codes, per market Shani trades.
#:
#: These are exact codes, deliberately not name matching. "GOLD" appears in
#: several CFTC series and "S&P 500" matches both the full-size contract and the
#: E-mini; attaching the wrong one to a market would produce a confident,
#: specific, wrong reading — the worst kind.
COT_CONTRACTS: dict[str, tuple[str, str]] = {
    # market: (cftc_contract_market_code, human name for display)
    "ES": ("13874A", "E-mini S&P 500 · CME"),
    "NQ": ("209742", "E-mini Nasdaq-100 · CME"),
    "CL": ("067651", "Light Sweet Crude Oil · NYMEX"),
    "GC": ("088691", "Gold · COMEX"),
}

COT_ENDPOINT = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
TREASURY_XML = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={year}"
)

# Where a human goes to check the number themselves. Deliberately the readable
# page rather than the endpoint the number was fetched from — a trader following
# a link wants the published report, not the JSON that fed the parser.
#
# Every driver carries one. A reading with no verifiable origin is an assertion,
# and the point of this layer is that it is not making things up.
COT_REPORT_URL = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm"
TREASURY_CURVE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "TextView?type=daily_treasury_yield_curve&field_tdr_date_value={year}"
)
FRED_SERIES_URL = "https://fred.stlouisfed.org/series/{series_id}"
CME_VOLUME_URL = (
    "https://www.cmegroup.com/markets/products.html#pageNumber=1&sortAsc=false"
)


@dataclass(slots=True)
class Driver:
    """One structured reading, tied to one market."""

    id: str
    market: str
    name: str
    value: str
    prior: str | None
    change: str | None
    lean: Lean
    confidence: float
    rationale: str
    as_of: str
    source: str
    #: Context that argues both ways and so must not be folded into ``lean``.
    note: str | None = None
    #: Where to read the published figure yourself. The portal turns the source
    #: label into a link, so a reading can always be checked at its origin
    #: rather than taken on trust.
    url: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "market": self.market, "name": self.name,
            "value": self.value, "prior": self.prior, "change": self.change,
            "lean": self.lean.value, "lean_label": self.lean.label,
            "score": self.lean.score, "confidence": round(self.confidence, 2),
            "rationale": self.rationale, "as_of": self.as_of,
            "source": self.source, "note": self.note, "url": self.url,
        }


@dataclass
class DriversService:
    """Fetches structured market drivers, cached."""

    #: Both sources update at most daily; COT is weekly. An hour is generous.
    cache_seconds: int = 3600
    _cache: tuple[float, list[Driver]] | None = None
    _errors: list[str] = field(default_factory=list)

    def invalidate(self) -> None:
        self._cache = None

    def fetch(self, symbols: list[str], *, refresh: bool = False) -> list[Driver]:
        if not refresh and self._cache and self._cache[0] > _time.monotonic():
            return [d for d in self._cache[1] if d.market in symbols]

        self._errors = []
        drivers: list[Driver] = []
        for loader in (self._cot, self._treasury, self._fred, self._cme):
            try:
                drivers.extend(loader())
            except Exception as exc:
                # One dead source must not empty the layer.
                self._errors.append(f"{loader.__name__.lstrip('_')}: {exc}"[:180])

        self._cache = (_time.monotonic() + self.cache_seconds, drivers)
        return [d for d in drivers if d.market in symbols]

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    # ── CFTC Commitments of Traders ──────────────────────────────────────────

    def _cot(self) -> list[Driver]:
        drivers: list[Driver] = []
        for market, (code, label) in COT_CONTRACTS.items():
            try:
                response = httpx.get(
                    COT_ENDPOINT,
                    params={
                        "cftc_contract_market_code": code,
                        "$order": "report_date_as_yyyy_mm_dd DESC",
                        "$limit": 2,  # latest and prior week, for the change
                        "$select": (
                            "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
                            "noncomm_positions_short_all,comm_positions_long_all,"
                            "comm_positions_short_all,open_interest_all"
                        ),
                    },
                    headers=_UA,
                    timeout=20.0,
                )
                response.raise_for_status()
                rows = response.json()
            except Exception:
                continue

            if len(rows) < 2:
                continue

            latest, prior = rows[0], rows[1]
            net_now = _int(latest, "noncomm_positions_long_all") - _int(
                latest, "noncomm_positions_short_all"
            )
            net_prior = _int(prior, "noncomm_positions_long_all") - _int(
                prior, "noncomm_positions_short_all"
            )
            change = net_now - net_prior
            open_interest = _int(latest, "open_interest_all") or 1

            # Direction from the CHANGE. Specs adding longs is a momentum
            # signal; specs *being* long is a state that persists for months and
            # says nothing about this week.
            share = abs(change) / open_interest
            if share < 0.005:
                lean, confidence = Lean.NEUTRAL, 0.15
            elif change > 0:
                lean = Lean.STRONG_BULLISH if share > 0.03 else Lean.BULLISH
                confidence = min(0.65, 0.25 + share * 8)
            else:
                lean = Lean.STRONG_BEARISH if share > 0.03 else Lean.BEARISH
                confidence = min(0.65, 0.25 + share * 8)

            # Crowding argues both ways depending on horizon, so it is a note
            # rather than a direction. Saying otherwise would be false precision.
            note = None
            crowd = abs(net_now) / open_interest
            if crowd > 0.25:
                side = "long" if net_now > 0 else "short"
                note = (
                    f"Speculators hold {crowd:.0%} of open interest net {side} — "
                    f"a crowded position, which cuts both ways."
                )

            drivers.append(
                Driver(
                    id=f"cot:{market}",
                    market=market,
                    name="Speculative positioning (CFTC COT)",
                    value=f"{net_now:+,} net",
                    prior=f"{net_prior:+,}",
                    change=f"{change:+,} w/w",
                    lean=lean,
                    confidence=confidence,
                    rationale=(
                        f"Large speculators {'added' if change > 0 else 'cut'} "
                        f"{abs(change):,} contracts, {share:.1%} of open interest."
                    ),
                    as_of=str(latest.get("report_date_as_yyyy_mm_dd", ""))[:10],
                    source=label,
                    note=note,
                    url=COT_REPORT_URL,
                )
            )
        return drivers

    # ── US Treasury par yield curve ──────────────────────────────────────────

    def _treasury(self) -> list[Driver]:
        year = datetime.now().year
        response = httpx.get(
            TREASURY_XML.format(year=year), headers=_UA, timeout=20.0, follow_redirects=True
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)

        ns = {
            "a": "http://www.w3.org/2005/Atom",
            "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
            "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
        }
        rows: list[tuple[str, float, float]] = []
        for entry in root.findall("a:entry", ns):
            props = entry.find("a:content/m:properties", ns)
            if props is None:
                continue
            stamp = _text(props, "d:NEW_DATE", ns) or _text(props, "d:Date", ns)
            two = _float(_text(props, "d:BC_2YEAR", ns))
            ten = _float(_text(props, "d:BC_10YEAR", ns))
            if stamp and two is not None and ten is not None:
                rows.append((stamp[:10], two, ten))

        if len(rows) < 2:
            raise RuntimeError("Treasury feed returned too few observations")

        rows.sort(key=lambda r: r[0])
        (_, _, ten_prior), (stamp, two_now, ten_now) = rows[-2], rows[-1]
        move_bp = (ten_now - ten_prior) * 100
        curve_bp = (ten_now - two_now) * 100

        # Rising yields compress equity valuations and raise the opportunity
        # cost of holding a non-yielding asset — so the same move is bearish for
        # ES, NQ and GC alike. This is the clearest example of why drivers must
        # be mapped per market rather than blended: the sign is shared here, but
        # the magnitude and the reason are not.
        if abs(move_bp) < 3:
            lean, confidence = Lean.NEUTRAL, 0.15
        elif move_bp > 0:
            lean = Lean.STRONG_BEARISH if move_bp > 10 else Lean.BEARISH
            confidence = min(0.7, 0.2 + abs(move_bp) / 25)
        else:
            lean = Lean.STRONG_BULLISH if move_bp < -10 else Lean.BULLISH
            confidence = min(0.7, 0.2 + abs(move_bp) / 25)

        direction = "rose" if move_bp > 0 else "fell" if move_bp < 0 else "held"
        drivers: list[Driver] = []
        for market in ("ES", "NQ", "GC"):
            why = {
                "ES": "Higher yields raise the discount rate on equity earnings.",
                "NQ": "Long-duration tech is the most rate-sensitive index.",
                "GC": "Higher yields raise the opportunity cost of holding gold.",
            }[market]
            drivers.append(
                Driver(
                    id=f"ust10:{market}",
                    market=market,
                    name="10-year Treasury yield",
                    value=f"{ten_now:.2f}%",
                    prior=f"{ten_prior:.2f}%",
                    change=f"{move_bp:+.0f}bp",
                    lean=lean,
                    confidence=confidence,
                    rationale=f"10y {direction} {abs(move_bp):.0f}bp. {why}",
                    as_of=stamp,
                    source="US Treasury",
                    note=(
                        f"2s10s at {curve_bp:+.0f}bp — curve inverted."
                        if curve_bp < 0
                        else None
                    ),
                    url=TREASURY_CURVE_URL.format(year=stamp[:4] or date.today().year),
                )
            )
        return drivers


    # ── FRED (St. Louis Fed) ─────────────────────────────────────────────────

    def _fred(self) -> list[Driver]:
        """Macro series that actually move index futures.

        Needs a free key. Silently absent without one rather than erroring —
        an unconfigured optional source is not a fault.

        Series are chosen because each has a direct, explicable transmission to
        a market Shani trades. VIX is the exception to the change-not-level rule
        below: with volatility, the *level* genuinely is the signal, since
        elevated vol suppresses risk appetite regardless of yesterday's print.
        """
        key = os.environ.get("FRED_API_KEY") or _from_dotenv("FRED_API_KEY")
        if not key:
            return []

        # series: (label, markets affected, inverted?, why)
        series: dict[str, tuple[str, tuple[str, ...], bool, str]] = {
            "VIXCLS": ("VIX", ("ES", "NQ"), True,
                       "Elevated volatility suppresses risk appetite."),
            "T10YIE": ("10y breakeven inflation", ("GC",), False,
                       "Rising inflation expectations support gold."),
            "DTWEXBGS": ("Trade-weighted dollar", ("GC", "CL"), True,
                         "A stronger dollar prices commodities higher abroad."),
        }

        drivers: list[Driver] = []
        for series_id, (label, markets, inverted, why) in series.items():
            try:
                response = httpx.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id": series_id, "api_key": key, "file_type": "json",
                        "sort_order": "desc", "limit": 6,
                    },
                    headers=_UA, timeout=20.0,
                )
                response.raise_for_status()
                rows = [
                    r for r in response.json().get("observations", [])
                    if r.get("value") not in (".", "", None)
                ]
            except Exception:
                continue
            if len(rows) < 2:
                continue

            now = _float(rows[0]["value"])
            prior = _float(rows[1]["value"])
            if now is None or prior is None or prior == 0:
                continue

            pct = (now - prior) / abs(prior) * 100
            raw = pct if not inverted else -pct
            if abs(pct) < 0.5:
                lean, confidence = Lean.NEUTRAL, 0.15
            elif raw > 0:
                lean = Lean.STRONG_BULLISH if abs(pct) > 4 else Lean.BULLISH
                confidence = min(0.6, 0.2 + abs(pct) / 12)
            else:
                lean = Lean.STRONG_BEARISH if abs(pct) > 4 else Lean.BEARISH
                confidence = min(0.6, 0.2 + abs(pct) / 12)

            for market in markets:
                drivers.append(
                    Driver(
                        id=f"fred:{series_id}:{market}",
                        market=market,
                        name=label,
                        value=f"{now:g}",
                        prior=f"{prior:g}",
                        change=f"{pct:+.1f}%",
                        lean=lean,
                        confidence=confidence,
                        rationale=f"{label} {'up' if pct > 0 else 'down'} "
                                  f"{abs(pct):.1f}%. {why}",
                        as_of=str(rows[0].get("date", ""))[:10],
                        source="FRED · St. Louis Fed",
                        url=FRED_SERIES_URL.format(series_id=series_id),
                    )
                )
        return drivers

    # ── CME volume and open interest ─────────────────────────────────────────

    def _cme(self) -> list[Driver]:
        """Volume and open interest for the contracts Shani trades.

        Open interest is the futures-native tell that has no equity equivalent:
        it counts contracts outstanding, so a price move on *rising* OI means
        new money committing, while the same move on *falling* OI means an
        existing position unwinding. Those look identical on a chart and mean
        opposite things.

        Sourced from CME's public quote feed rather than the PDF bulletin, which
        is a scraping target rather than an interface.
        """
        products = {
            "ES": ("133", "E-mini S&P 500 · CME"),
            "NQ": ("146", "E-mini Nasdaq-100 · CME"),
            "CL": ("425", "Light Sweet Crude · NYMEX"),
            "GC": ("437", "Gold · COMEX"),
        }
        drivers: list[Driver] = []
        misses: list[str] = []
        for market, (product_id, label) in products.items():
            reading = self._cme_volume(product_id)
            if reading is None:
                misses.append(market)
                continue
            trade_date, volume, open_interest, prior_oi = reading

            # OI change has a real interpretation; volume alone does not, so
            # only the former earns a direction. Rising OI means positions being
            # opened, falling OI means an unwind — and a rating derived from
            # volume by itself would be the false precision this module avoids.
            lean, confidence, rationale = Lean.NEUTRAL, 0.0, (
                "Rising OI on a move means new money committing; falling OI "
                "means an unwind. Identical on a chart."
            )
            change = None
            if open_interest and prior_oi:
                delta = open_interest - prior_oi
                share = abs(delta) / max(prior_oi, 1)
                change = f"{delta:+,} OI"
                if share > 0.02:
                    lean = Lean.BULLISH if delta > 0 else Lean.BEARISH
                    confidence = min(0.4, 0.15 + share * 4)
                    rationale = (
                        f"Open interest {'rose' if delta > 0 else 'fell'} "
                        f"{abs(delta):,} ({share:.1%}) — "
                        f"{'new positions opening' if delta > 0 else 'positions unwinding'}."
                    )

            drivers.append(
                Driver(
                    id=f"cme:{market}",
                    market=market,
                    name="Volume / open interest",
                    value=f"{volume:,} vol" + (f" · {open_interest:,} OI" if open_interest else ""),
                    prior=f"{prior_oi:,} OI" if prior_oi else None,
                    change=change,
                    lean=lean,
                    confidence=confidence,
                    rationale=rationale,
                    as_of=trade_date,
                    source=label,
                    url=CME_VOLUME_URL,
                )
            )

        # Surfaced rather than swallowed. CME's volume service is undocumented
        # and returns HTTP 200 with a body full of zeros when it has nothing for
        # a date — a response that looks like data and is not. If it yields
        # nothing at all, the layer says so instead of quietly presenting three
        # sources as four.
        if not drivers and misses:
            raise RuntimeError(
                f"no volume/OI returned for {', '.join(misses)} — CME's endpoint "
                f"answered but had no figures. Undocumented service; see _cme_volume."
            )
        return drivers

    def _cme_volume(self, product_id: str) -> tuple[str, int, int, int] | None:
        """Most recent published volume and open interest for a product.

        CME publishes on a lag and skips weekends and holidays, so the date has
        to be walked backwards rather than assumed — asking for "today" returns
        a valid response full of zeros, which is the kind of answer that looks
        like data and is not.

        Returns ``(trade_date, volume, open_interest, prior_open_interest)``.
        """
        # A browser-ish agent is required; the default one gets a challenge page.
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.cmegroup.com/",
        }
        found: list[tuple[str, int, int]] = []
        for offset in range(1, 9):
            day = date.today() - timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            try:
                response = httpx.get(
                    f"https://www.cmegroup.com/CmeWS/mvc/Volume/Details/F/"
                    f"{product_id}/{day.isoformat()}/FUT",
                    headers=headers,
                    timeout=15.0,
                    follow_redirects=True,
                )
                response.raise_for_status()
                totals = response.json().get("totals") or {}
            except Exception:
                continue

            volume = _to_int(totals.get("totalVolume")) or 0
            open_interest = _to_int(totals.get("atClose")) or _to_int(
                totals.get("openInterest")
            ) or 0
            if volume > 0:
                found.append((day.isoformat(), volume, open_interest))
            if len(found) == 2:
                break

        if not found:
            return None
        trade_date, volume, open_interest = found[0]
        prior_oi = found[1][2] if len(found) > 1 else 0
        return trade_date, volume, open_interest, prior_oi


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _from_dotenv(key: str) -> str | None:
    from shani.settings_store import read_env_value

    return read_env_value(key)


def _int(row: dict[str, Any], key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _text(node: Any, path: str, ns: dict[str, str]) -> str | None:
    found = node.find(path, ns)
    return found.text if found is not None else None


def latest_report_date() -> date | None:
    """Not used internally; handy in a REPL when checking freshness."""
    return None
