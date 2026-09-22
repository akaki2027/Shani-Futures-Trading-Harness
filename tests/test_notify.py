"""Notifications must never block the trading loop.

The module already swallows exceptions. A backend that simply never answers is
the same hazard and raises nothing, which is how a headless macOS CI runner
wedged the whole suite: the notification centre accepted the call on an order
refusal and never came back.
"""

from __future__ import annotations

import asyncio

import pytest

from shani.notify import Notifier


class _HangingBackend:
    """Accepts the send and never returns, like a headless notification centre."""

    async def send(self, **_: object) -> None:
        await asyncio.Event().wait()


class _WorkingBackend:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, *, title: str, **_: object) -> None:
        self.sent.append(title)


async def test_a_hanging_backend_cannot_block_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shani.notify.SEND_TIMEOUT", 0.05)
    notifier = Notifier()
    notifier._backend = _HangingBackend()

    sent = await asyncio.wait_for(notifier.send("t", "m"), timeout=5)

    assert sent is False, "a timed-out notification must report failure, not success"


async def test_a_hung_backend_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise every later order pays the timeout again."""
    monkeypatch.setattr("shani.notify.SEND_TIMEOUT", 0.05)
    notifier = Notifier()
    notifier._backend = _HangingBackend()

    await notifier.send("first", "m")
    assert notifier._unavailable

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await notifier.send("second", "m") is False
    assert loop.time() - started < 0.05, "second send should short-circuit, not wait again"


async def test_a_working_backend_still_sends() -> None:
    backend = _WorkingBackend()
    notifier = Notifier()
    notifier._backend = backend

    assert await notifier.send("Shani halted trading", "detail") is True
    assert backend.sent == ["Shani halted trading"]
