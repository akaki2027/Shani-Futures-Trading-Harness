"""News connectors.

Two tiers by design.

**Works immediately, no account:** RSS from major financial desks, and Yahoo
Finance's per-symbol news. A news section that shows nothing until you have
registered for three APIs is a news section nobody switches on, so the default
install has real headlines flowing on first run.

**Needs a key:** Reddit and X. Both are genuinely useful — retail positioning
and breaking chatter often move futures before the wires catch up — and both
require credentials the user must obtain. They are listed in the settings panel
whether or not a key exists, with a link to get one, so the capability is
discoverable rather than hidden until configured.

Every provider is deliberately small and independent. A broken or rate-limited
source degrades to "that connector is down" rather than emptying the feed.
"""

from __future__ import annotations

import html
import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar
from xml.etree import ElementTree

import httpx

from shani.news.base import NewsItem, ProviderError, ProviderInfo

__all__ = ["ALL_PROVIDERS", "RedditProvider", "RssProvider", "XProvider", "YahooNewsProvider"]

_UA = {"User-Agent": "shani/0.1 (personal trading journal)"}


def _clean(text: str) -> str:
    """Strip tags and unescape entities. Feeds are full of both."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class RssProvider:
    """Financial newswires over RSS. No account, no key, no rate limit worth speaking of.

    Parsed with the standard library rather than a feed dependency: RSS is a
    handful of tags, and the failure modes of a lenient XML parse are easier to
    reason about than those of a library that silently returns an empty list.
    """

    #: Chosen for *futures* relevance, not general finance interest.
    #:
    #: The obvious feeds — Yahoo Finance's index, MarketWatch top stories — are
    #: consumer personal-finance desks. They produce a stream of retirement
    #: advice, insider-sale filler and celebrity items that the classifier
    #: correctly marks neutral, which means paying a model to read noise and
    #: spending screen space displaying it.
    #:
    #: What actually moves index and commodity futures is rates, inflation,
    #: central bank language, and supply. These feeds are weighted accordingly,
    #: with the Fed's own press releases first — it is the single highest-signal
    #: source available for free, and it is primary rather than reported.
    FEEDS: ClassVar[dict[str, str]] = {
        "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
        "Investing · Economy": "https://www.investing.com/rss/news_14.rss",
        "Investing · Commodities": "https://www.investing.com/rss/news_11.rss",
        "Investing · Markets": "https://www.investing.com/rss/news_25.rss",
        "CNBC Markets": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    }

    info = ProviderInfo(
        id="rss",
        name="Financial newswires",
        description="CNBC, Yahoo Finance, MarketWatch and Investing.com headlines over RSS.",
        requires_key=False,
        enabled_by_default=True,
    )

    def available(self) -> bool:
        return True

    def fetch(self, symbols: list[str], limit: int) -> list[NewsItem]:
        items: list[NewsItem] = []
        errors: list[str] = []

        for name, url in self.FEEDS.items():
            try:
                response = httpx.get(url, headers=_UA, timeout=12.0, follow_redirects=True)
                response.raise_for_status()
                root = ElementTree.fromstring(response.content)
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}")
                continue

            for entry in root.iter("item"):
                title = _clean(entry.findtext("title") or "")
                link = (entry.findtext("link") or "").strip()
                if not title or not link:
                    continue
                items.append(
                    NewsItem(
                        id=link,
                        title=title[:300],
                        source=name,
                        url=link,
                        published_at=_parse_date(entry.findtext("pubDate")),
                        summary=_clean(entry.findtext("description") or "")[:600],
                        symbols=_guess_symbols(f"{title} {entry.findtext('description') or ''}"),
                    )
                )

        # Only a total failure is an error. One dead feed out of five is a bad
        # day for that publisher, not a broken connector.
        if not items and errors:
            raise ProviderError(f"every RSS feed failed ({'; '.join(errors[:3])})")

        items.sort(key=lambda i: i.published_at, reverse=True)
        return items[:limit]


class YahooNewsProvider:
    """Per-symbol news from Yahoo Finance. No key.

    Complements the wires: RSS gives the macro picture, this gives items
    actually tagged to the instruments on the watchlist.
    """

    SYMBOL_MAP: ClassVar[dict[str, str]] = {
        "ES": "ES=F", "MES": "ES=F", "NQ": "NQ=F", "MNQ": "NQ=F",
        "RTY": "RTY=F", "YM": "YM=F", "CL": "CL=F", "MCL": "CL=F",
        "NG": "NG=F", "GC": "GC=F", "MGC": "GC=F", "SI": "SI=F",
    }

    info = ProviderInfo(
        id="yahoo",
        name="Yahoo Finance (per symbol)",
        description="Headlines tagged to the instruments on your watchlist.",
        requires_key=False,
        enabled_by_default=True,
    )

    def available(self) -> bool:
        return True

    def fetch(self, symbols: list[str], limit: int) -> list[NewsItem]:
        tickers = [self.SYMBOL_MAP.get(s) for s in symbols]
        query = ",".join(t for t in tickers if t) or "ES=F"
        try:
            response = httpx.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": query, "newsCount": limit, "quotesCount": 0},
                headers=_UA,
                timeout=12.0,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise ProviderError(f"Yahoo news request failed: {exc}") from exc

        items: list[NewsItem] = []
        for entry in payload.get("news", [])[:limit]:
            link = entry.get("link", "")
            title = _clean(entry.get("title", ""))
            if not link or not title:
                continue
            stamp = entry.get("providerPublishTime")
            published = (
                datetime.fromtimestamp(stamp, tz=UTC)
                if isinstance(stamp, (int, float))
                else datetime.now(UTC)
            )
            items.append(
                NewsItem(
                    id=link,
                    title=title[:300],
                    source=entry.get("publisher") or "Yahoo Finance",
                    url=link,
                    published_at=published,
                    symbols=_guess_symbols(title) or symbols[:1],
                )
            )
        return items


class RedditProvider:
    """Trading subreddits. Needs a Reddit app (free).

    Worth having despite the noise: retail positioning and sentiment shifts show
    up here before they reach the wires, and for index futures that occasionally
    matters. Treated as chatter rather than reporting, and the classifier is told
    as much.
    """

    SUBREDDITS = ("wallstreetbets", "stocks", "investing", "futurestrading", "economy")

    info = ProviderInfo(
        id="reddit",
        name="Reddit",
        description="Trading subreddits. Chatter and positioning, not reporting.",
        key_env_var="REDDIT_CLIENT_ID",
        signup_url="https://www.reddit.com/prefs/apps",
        requires_key=True,
    )

    def available(self) -> bool:
        return bool(os.environ.get("REDDIT_CLIENT_ID") or _from_dotenv("REDDIT_CLIENT_ID"))

    def fetch(self, symbols: list[str], limit: int) -> list[NewsItem]:
        client_id = os.environ.get("REDDIT_CLIENT_ID") or _from_dotenv("REDDIT_CLIENT_ID")
        secret = os.environ.get("REDDIT_CLIENT_SECRET") or _from_dotenv("REDDIT_CLIENT_SECRET")
        if not client_id or not secret:
            raise ProviderError(
                "Reddit needs REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET. "
                "Create an app at https://www.reddit.com/prefs/apps (type: script)."
            )
        try:
            auth = httpx.post(
                "https://www.reddit.com/api/v1/access_token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, secret),
                headers=_UA,
                timeout=15.0,
            )
            auth.raise_for_status()
            token = auth.json()["access_token"]
        except Exception as exc:
            raise ProviderError(f"Reddit authentication failed: {exc}") from exc

        items: list[NewsItem] = []
        per_sub = max(2, limit // len(self.SUBREDDITS))
        for sub in self.SUBREDDITS:
            try:
                response = httpx.get(
                    f"https://oauth.reddit.com/r/{sub}/hot",
                    params={"limit": per_sub},
                    headers={**_UA, "Authorization": f"Bearer {token}"},
                    timeout=15.0,
                )
                response.raise_for_status()
                children = response.json().get("data", {}).get("children", [])
            except Exception:
                continue

            for child in children:
                post = child.get("data", {})
                title = _clean(post.get("title", ""))
                if not title or post.get("stickied"):
                    continue
                items.append(
                    NewsItem(
                        id=f"reddit:{post.get('id')}",
                        title=title[:300],
                        source=f"r/{sub}",
                        url=f"https://reddit.com{post.get('permalink', '')}",
                        published_at=datetime.fromtimestamp(
                            post.get("created_utc", 0) or 0, tz=UTC
                        ),
                        summary=_clean(post.get("selftext", ""))[:400],
                        symbols=_guess_symbols(title),
                    )
                )
        items.sort(key=lambda i: i.published_at, reverse=True)
        return items[:limit]


class XProvider:
    """X / Twitter. Needs a paid API tier.

    Listed even though most people will not enable it, because discovering that
    a capability exists is worth more than hiding it. X removed free read access,
    so this requires at minimum their Basic tier — stated plainly in the UI
    rather than letting someone find out after signing up.
    """

    info = ProviderInfo(
        id="x",
        name="X (Twitter)",
        description="Breaking chatter. Requires a paid X API tier — no free read access.",
        key_env_var="X_BEARER_TOKEN",
        signup_url="https://developer.x.com/en/portal/dashboard",
        requires_key=True,
    )

    def available(self) -> bool:
        return bool(os.environ.get("X_BEARER_TOKEN") or _from_dotenv("X_BEARER_TOKEN"))

    def fetch(self, symbols: list[str], limit: int) -> list[NewsItem]:
        token = os.environ.get("X_BEARER_TOKEN") or _from_dotenv("X_BEARER_TOKEN")
        if not token:
            raise ProviderError("X needs X_BEARER_TOKEN (paid API tier).")

        cashtags = " OR ".join(f"${s}" for s in (symbols or ["ES"])[:4])
        try:
            response = httpx.get(
                "https://api.x.com/2/tweets/search/recent",
                params={
                    "query": f"({cashtags}) -is:retweet lang:en",
                    "max_results": max(10, min(limit, 100)),
                    "tweet.fields": "created_at,public_metrics",
                },
                headers={**_UA, "Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            if response.status_code == 401:
                raise ProviderError("X rejected the token — check it is a Bearer token.")
            if response.status_code == 429:
                raise ProviderError("X rate limit reached. Back off and try later.")
            response.raise_for_status()
            payload = response.json()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"X request failed: {exc}") from exc

        items: list[NewsItem] = []
        for tweet in payload.get("data", []):
            text = _clean(tweet.get("text", ""))
            if not text:
                continue
            items.append(
                NewsItem(
                    id=f"x:{tweet.get('id')}",
                    title=text[:280],
                    source="X",
                    url=f"https://x.com/i/status/{tweet.get('id')}",
                    published_at=_parse_date(tweet.get("created_at")),
                    symbols=_guess_symbols(text),
                )
            )
        return items[:limit]


#: Registry. Adding a connector means appending here and nothing else.
ALL_PROVIDERS: list[Any] = [
    RssProvider(),
    YahooNewsProvider(),
    RedditProvider(),
    XProvider(),
]


#: Words that reliably indicate a contract, mapped to its root. Deliberately
#: conservative — a wrong tag is worse than no tag, because it puts a headline
#: in front of a trader as though it bears on their position when it does not.
_SYMBOL_HINTS: dict[str, tuple[str, ...]] = {
    "ES": ("s&p", "s&p 500", "spx", "sp500", "es futures"),
    "NQ": ("nasdaq", "ndx", "qqq", "tech stocks"),
    "RTY": ("russell", "small cap", "small-cap"),
    "YM": ("dow jones", "dow ", "djia"),
    "CL": ("crude", "oil price", "wti", "opec", "petroleum"),
    "NG": ("natural gas", "nat gas", "henry hub"),
    "GC": ("gold", "bullion"),
    "SI": ("silver",),
}


def _guess_symbols(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [root for root, hints in _SYMBOL_HINTS.items() if any(h in lowered for h in hints)]


def _from_dotenv(key: str) -> str | None:
    """Read a credential from .env.

    Same reason as the model key: nothing loads .env into the process
    environment, so a connector configured through the portal would work until
    the next restart and then silently stop.
    """
    from shani.settings_store import read_env_value

    return read_env_value(key)
