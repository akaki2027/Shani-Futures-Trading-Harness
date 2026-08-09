"""Desktop notifications.

Exists for exactly one reason: the post-trade interview is worthless if it
arrives late. An answer given four hours after the fill is a reconstruction —
tidy, flattering, and useless for learning. An answer given ninety seconds after
the fill is what actually happened, including the parts the trader would rather
not write down.

So when a trade closes, a native OS notification fires whether or not the portal
tab is open. No third-party service, no bot token, nothing leaving the machine.

Failures here are always swallowed. A notification backend that cannot start —
missing WinRT bindings, no D-Bus session, a headless server — must never take
down the trading loop. Losing a toast is a minor annoyance; crashing the process
that is tracking an open position is not.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from shani.models import Trade

__all__ = ["Notifier"]

log = logging.getLogger(__name__)


class Notifier:
    """Best-effort desktop notifications."""

    def __init__(self, *, enabled: bool = True, app_name: str = "Shani") -> None:
        self.enabled = enabled
        self.app_name = app_name
        self._backend: object | None = None
        self._unavailable = False

    def _get_backend(self) -> object | None:
        if self._unavailable or not self.enabled:
            return None
        if self._backend is None:
            try:
                from desktop_notifier import DesktopNotifier

                self._backend = DesktopNotifier(app_name=self.app_name)
            except Exception as exc:
                log.debug("Desktop notifications unavailable: %s", exc)
                self._unavailable = True
                return None
        return self._backend

    async def send(self, title: str, message: str, *, urgent: bool = False) -> bool:
        """Send a notification. Returns whether it went out."""
        backend = self._get_backend()
        if backend is None:
            return False
        try:
            from desktop_notifier import Urgency

            await backend.send(  # type: ignore[attr-defined]
                title=title,
                message=message,
                urgency=Urgency.Critical if urgent else Urgency.Normal,
            )
        except Exception as exc:
            log.debug("Notification failed: %s", exc)
            return False
        return True

    async def trade_closed(self, trade: Trade) -> bool:
        """Prompt the interview while the trade is still fresh."""
        result = "won" if trade.net_pnl > 0 else "lost" if trade.net_pnl < 0 else "scratched"
        r = f" ({trade.r_multiple:+.2f}R)" if trade.r_multiple is not None else ""
        return await self.send(
            f"{trade.symbol} {result} ${abs(trade.net_pnl):,.2f}{r}",
            "Why did you take it? Answer now, while you still remember.",
        )

    async def risk_limit(self, rule: str, detail: str) -> bool:
        return await self.send(f"Shani halted trading — {rule}", detail, urgent=True)

    async def proposal(self, symbol: str, side: str, grounded: bool) -> bool:
        suffix = "" if grounded else " (no matching history)"
        return await self.send(
            f"Proposal: {side} {symbol}{suffix}", "Open the portal to review."
        )

    async def daily_summary(self, net_pnl: Decimal, trades: int) -> bool:
        direction = "up" if net_pnl >= 0 else "down"
        return await self.send(
            f"Session finished {direction} ${abs(net_pnl):,.2f}",
            f"{trades} trades. Review them in the portal.",
        )
