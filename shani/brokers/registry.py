"""Broker registry — where "live trading is disabled" is actually enforced.

The distinction this module exists to make: a guarded live adapter is one
inverted boolean away from sending a real order. An *unregistered* one cannot be
constructed at all.

So while ``allow_live`` is false, live venues are never added to the registry.
Asking for one does not return a disabled object that refuses politely — it
raises, because there is nothing there. There is no flag deep in the order path
that a refactor could invert by accident, and no code path from a signal to a
live venue that exists but happens not to run.

Enabling live trading deliberately requires two independent things: the
``allow_live`` flag *and* an exact confirmation phrase in the config. A boolean
alone is too easy to flip while skimming a config file.
"""

from __future__ import annotations

from shani.audit import AuditLog, EventType
from shani.brokers.base import Broker
from shani.brokers.paper import PaperBroker
from shani.config import Config, LiveTradingDisabledError
from shani.db import Database

__all__ = ["BrokerRegistry", "build_registry"]

#: Venues that can move real money. Named here so the gate is one obvious list
#: rather than a property scattered across adapter classes.
LIVE_VENUES = frozenset({"ninjatrader", "alpaca", "ccxt"})


class BrokerRegistry:
    """Holds the brokers this process is allowed to use."""

    def __init__(self, *, live_enabled: bool) -> None:
        self._brokers: dict[str, Broker] = {}
        self.live_enabled = live_enabled

    def register(self, broker: Broker) -> None:
        if broker.is_live and not self.live_enabled:
            raise LiveTradingDisabledError(
                f"Refusing to register live broker {broker.name!r}: live trading is "
                f"disabled. This is not a warning — the adapter will not be available."
            )
        self._brokers[broker.name] = broker

    def get(self, name: str) -> Broker:
        try:
            return self._brokers[name]
        except KeyError:
            if name in LIVE_VENUES and not self.live_enabled:
                raise LiveTradingDisabledError(
                    f"{name!r} is a live venue and live trading is disabled.\n\n"
                    f"To enable it you must set BOTH:\n"
                    f"  broker.allow_live: true\n"
                    f'  broker.live_confirmation: "I accept full responsibility for '
                    f'live orders"\n\n'
                    f"Read docs/safety.md first. Live execution is untested — it ships "
                    f"disabled precisely because nobody has verified it against a real "
                    f"account."
                ) from None
            available = ", ".join(sorted(self._brokers)) or "none"
            raise KeyError(
                f"No broker named {name!r}. Available: {available}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._brokers)

    def __contains__(self, name: str) -> bool:
        return name in self._brokers


def build_registry(config: Config, db: Database, audit: AuditLog | None = None) -> BrokerRegistry:
    """Construct the registry for this configuration.

    The paper broker is always present — it is what makes a fresh clone
    immediately useful, and what the learning loop runs against.
    """
    live_enabled = config.broker.live_enabled
    registry = BrokerRegistry(live_enabled=live_enabled)

    registry.register(
        PaperBroker(
            db,
            starting_balance=config.broker.starting_balance,
            slippage_ticks=config.broker.slippage_ticks,
            enforce_market_hours=config.broker.enforce_market_hours,
        )
    )

    if not live_enabled:
        if audit is not None and config.broker.allow_live:
            # Flag set but the phrase is missing or wrong. Worth logging loudly:
            # the trader believes live trading is on and it is not.
            audit.warn(
                EventType.LIVE_TRADING_BLOCKED,
                "broker.allow_live is true but live_confirmation does not match the "
                "required phrase — live venues remain unregistered.",
                payload={"registered": registry.names()},
            )
        return registry

    # Live venues would be registered here. Deliberately empty in this release:
    # no live adapter has been verified against a real account, and shipping an
    # unverified one that *looks* ready is worse than shipping none.
    if audit is not None:
        audit.warn(
            EventType.LIVE_TRADING_BLOCKED,
            "Live trading is enabled in config, but no live adapter is implemented "
            "in this release. Orders will continue to route to the paper broker.",
            payload={"registered": registry.names()},
        )
    return registry
