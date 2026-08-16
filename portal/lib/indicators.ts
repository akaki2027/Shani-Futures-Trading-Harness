/**
 * Chart overlays, computed from the bars already on screen.
 *
 * Client-side on purpose. These are pure functions of data the browser has
 * anyway, so a round trip would add latency and a failure mode for arithmetic a
 * laptop does in under a millisecond.
 *
 * Every series is length-aligned with the input by emitting `null` for bars
 * before the window fills, rather than starting the line partway along a
 * shorter array. lightweight-charts drops null points, so the line simply
 * begins where it becomes real — and an off-by-one shift, which would silently
 * draw every average one bar early, becomes impossible.
 */

import type { Bar } from './api';

export interface LinePoint {
  time: number;
  value: number;
}

/** Simple moving average. */
export function sma(bars: Bar[], period: number): LinePoint[] {
  const out: LinePoint[] = [];
  let running = 0;
  for (let i = 0; i < bars.length; i++) {
    running += bars[i]!.close;
    if (i >= period) running -= bars[i - period]!.close;
    if (i >= period - 1) {
      out.push({ time: bars[i]!.time, value: running / period });
    }
  }
  return out;
}

/** Exponential moving average, seeded from the first `period` bars' SMA. */
export function ema(bars: Bar[], period: number): LinePoint[] {
  if (bars.length < period) return [];
  const k = 2 / (period + 1);
  const out: LinePoint[] = [];

  let seed = 0;
  for (let i = 0; i < period; i++) seed += bars[i]!.close;
  let value = seed / period;
  out.push({ time: bars[period - 1]!.time, value });

  for (let i = period; i < bars.length; i++) {
    value = bars[i]!.close * k + value * (1 - k);
    out.push({ time: bars[i]!.time, value });
  }
  return out;
}

/**
 * Session-anchored VWAP.
 *
 * Anchored, not rolling. VWAP's meaning comes from being the volume-weighted
 * average price *since the session began* — it is where the day's business has
 * actually been done, which is why traders lean on it as a fair-value
 * reference. A rolling window would produce a smooth line with no such meaning,
 * and it would look plausible, which is worse.
 *
 * The anchor resets on each new trading day. `sessionStart` decides when that
 * is, so the caller can align it to the 18:00 ET Globex open rather than
 * midnight — futures do not start their day at 00:00.
 *
 * Returns an empty series for daily bars: a VWAP across days is not a thing.
 */
export function vwap(bars: Bar[], sessionKey: (bar: Bar) => string): LinePoint[] {
  const out: LinePoint[] = [];
  let key: string | null = null;
  let cumulativePV = 0;
  let cumulativeVolume = 0;

  for (const bar of bars) {
    const thisKey = sessionKey(bar);
    if (thisKey !== key) {
      key = thisKey;
      cumulativePV = 0;
      cumulativeVolume = 0;
    }
    // Typical price, the standard VWAP input.
    const typical = (bar.high + bar.low + bar.close) / 3;
    const volume = bar.volume > 0 ? bar.volume : 1; // some feeds report 0
    cumulativePV += typical * volume;
    cumulativeVolume += volume;
    out.push({ time: bar.time, value: cumulativePV / cumulativeVolume });
  }
  return out;
}

/**
 * Trading-day key in US Eastern, with the day starting at 18:00.
 *
 * Shares the boundary the Python side uses for sessions, so a VWAP anchor and a
 * journal entry agree about which day a 20:00 bar belongs to. Two different
 * answers to "what day is this" across one product is a bug waiting to be
 * argued about.
 */
export function globexSessionKey(bar: Bar): string {
  const et = new Date(
    new Date(bar.time * 1000).toLocaleString('en-US', { timeZone: 'America/New_York' }),
  );
  const shifted = new Date(et);
  if (et.getHours() >= 18) shifted.setDate(shifted.getDate() + 1);
  return shifted.toISOString().slice(0, 10);
}
