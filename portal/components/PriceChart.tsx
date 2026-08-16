'use client';

import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  LineStyle,
} from 'lightweight-charts';
import { useEffect, useRef, useState } from 'react';
import { api, type Bar } from '@/lib/api';
import { ema, globexSessionKey, type LinePoint, sma, vwap } from '@/lib/indicators';

const INTERVALS = ['5m', '15m', '1h', '4h', '1d'] as const;

/** Overlays, in draw order. Colours are distinct from the P&L greens and reds
 *  so an indicator line is never mistaken for a directional signal. */
const OVERLAYS = [
  { id: 'sma20', label: 'SMA 20', colour: '#8fa8c8', width: 1 },
  { id: 'sma50', label: 'SMA 50', colour: '#a98fc8', width: 1 },
  { id: 'ema20', label: 'EMA 20', colour: '#c8a88f', width: 1 },
  { id: 'vwap', label: 'VWAP', colour: '#e0aa46', width: 2 },
] as const;

type OverlayId = (typeof OVERLAYS)[number]['id'];

function computeOverlay(id: OverlayId, bars: Bar[], interval: string): LinePoint[] {
  switch (id) {
    case 'sma20':
      return sma(bars, 20);
    case 'sma50':
      return sma(bars, 50);
    case 'ema20':
      return ema(bars, 20);
    case 'vwap':
      // A VWAP across days is meaningless, so it is simply absent on daily bars
      // rather than drawn as a line that looks authoritative and is not.
      return interval === '1d' ? [] : vwap(bars, globexSessionKey);
  }
}

/**
 * Price chart for the selected watchlist instrument.
 *
 * Themed from the design tokens rather than left on library defaults — a chart
 * that does not match the surface around it is the loudest possible signal that
 * a page was assembled rather than built. Candles use the same muted P&L greens
 * and reds as every other number in the portal, so a rising candle and a
 * winning trade read as the same colour.
 *
 * Volume sits in its own scale pinned to the bottom fifth, so it gives context
 * without stealing vertical space from price.
 */
export function PriceChart({ symbol }: { symbol: string }) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null);

  const overlays = useRef<Map<OverlayId, ISeriesApi<'Line'>>>(new Map());
  const barsRef = useRef<Bar[]>([]);

  const [interval, setInterval] = useState<string>('15m');
  const [active, setActive] = useState<Set<OverlayId>>(new Set(['sma20', 'vwap']));
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [meta, setMeta] = useState<{ name: string; last: number | null }>({
    name: '',
    last: null,
  });

  // Build the chart once. Data arrives separately so changing timeframe does
  // not tear down and rebuild the whole canvas.
  useEffect(() => {
    if (!container.current) return;

    const styles = getComputedStyle(document.documentElement);
    const token = (name: string, fallback: string) =>
      styles.getPropertyValue(name).trim() || fallback;

    const up = token('--up', '#6f9b6a');
    const down = token('--down', '#b56a5e');
    const line = token('--line', 'rgba(236,231,221,0.09)');

    const instance = createChart(container.current, {
      layout: {
        background: { color: 'transparent' },
        textColor: token('--fg-300', '#6e6a63'),
        fontFamily: token('--font-ui', 'sans-serif'),
        fontSize: 11,
      },
      grid: { vertLines: { color: line }, horzLines: { color: line } },
      rightPriceScale: { borderColor: line, scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { borderColor: line, timeVisible: true, secondsVisible: false },
      crosshair: {
        vertLine: { color: token('--accent-dim', '#8a6620'), style: LineStyle.Dotted },
        horzLine: { color: token('--accent-dim', '#8a6620'), style: LineStyle.Dotted },
      },
    });

    candles.current = instance.addCandlestickSeries({
      upColor: up,
      downColor: down,
      borderUpColor: up,
      borderDownColor: down,
      wickUpColor: up,
      wickDownColor: down,
    });

    // Its own price scale, so volume never compresses the price axis.
    volume.current = instance.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });
    instance.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    // One line series per overlay, created once and fed data as toggles change.
    // Adding and removing series on every toggle makes the chart flicker and
    // loses the user's zoom.
    for (const overlay of OVERLAYS) {
      overlays.current.set(
        overlay.id,
        instance.addLineSeries({
          color: overlay.colour,
          lineWidth: overlay.width as 1 | 2,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        }),
      );
    }

    chart.current = instance;

    const resize = () => {
      if (container.current) instance.applyOptions({ width: container.current.clientWidth });
    };
    resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      instance.remove();
      chart.current = null;
      candles.current = null;
      volume.current = null;
      overlays.current.clear();
    };
  }, []);

  // Redraw overlays whenever the toggles or the underlying bars change.
  useEffect(() => {
    const bars = barsRef.current;
    for (const overlay of OVERLAYS) {
      const series = overlays.current.get(overlay.id);
      if (!series) continue;
      if (!active.has(overlay.id) || bars.length === 0) {
        series.setData([]);
        continue;
      }
      series.setData(
        computeOverlay(overlay.id, bars, interval).map((p) => ({
          time: p.time as never,
          value: p.value,
        })),
      );
    }
  }, [active, interval, loading]);

  // Load bars whenever the symbol or timeframe changes.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    api
      .bars(symbol, interval)
      .then((payload) => {
        if (cancelled || !candles.current || !volume.current) return;
        const bars: Bar[] = payload.bars;
        barsRef.current = bars;
        candles.current.setData(
          bars.map((b) => ({
            time: b.time as never,
            open: b.open,
            high: b.high,
            low: b.low,
            close: b.close,
          })),
        );
        const styles = getComputedStyle(document.documentElement);
        const up = styles.getPropertyValue('--up-wash').trim() || 'rgba(111,155,106,0.14)';
        const down = styles.getPropertyValue('--down-wash').trim() || 'rgba(181,106,94,0.14)';
        volume.current.setData(
          bars.map((b) => ({
            time: b.time as never,
            value: b.volume,
            color: b.close >= b.open ? up : down,
          })),
        );
        chart.current?.timeScale().fitContent();
        setMeta({ name: payload.name, last: bars.at(-1)?.close ?? null });
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load bars.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [symbol, interval]);

  return (
    <section className="region">
      <div className="region-head">
        <h2>{symbol}</h2>
        <span className="muted" style={{ fontSize: '0.75rem' }}>
          {meta.name}
          {meta.last !== null && (
            <>
              {' · '}
              <span className="num" style={{ color: 'var(--fg-000)' }}>
                {meta.last.toLocaleString('en-US', { minimumFractionDigits: 2 })}
              </span>
            </>
          )}
        </span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: '0.25rem' }}>
          {INTERVALS.map((tf) => (
            <button
              key={tf}
              className={tf === interval ? 'selected' : ''}
              style={{ padding: '0.2rem 0.45rem', fontSize: '0.75rem' }}
              onClick={() => setInterval(tf)}
              aria-pressed={tf === interval}
            >
              {tf}
            </button>
          ))}
        </div>
      </div>

      <div
        style={{
          display: 'flex',
          gap: '0.35rem',
          padding: '0 1rem 0.5rem',
          flexWrap: 'wrap',
          alignItems: 'center',
        }}
      >
        {OVERLAYS.map((overlay) => {
          const on = active.has(overlay.id);
          const unavailable = overlay.id === 'vwap' && interval === '1d';
          return (
            <button
              key={overlay.id}
              disabled={unavailable}
              aria-pressed={on && !unavailable}
              title={unavailable ? 'VWAP is meaningless across days' : undefined}
              style={{
                padding: '0.15rem 0.45rem',
                fontSize: '0.7rem',
                display: 'inline-flex',
                alignItems: 'center',
                gap: '0.35rem',
                borderColor: on && !unavailable ? overlay.colour : undefined,
                color: on && !unavailable ? 'var(--fg-000)' : undefined,
              }}
              onClick={() =>
                setActive((current) => {
                  const next = new Set(current);
                  if (next.has(overlay.id)) next.delete(overlay.id);
                  else next.add(overlay.id);
                  return next;
                })
              }
            >
              <i
                style={{
                  width: 10,
                  height: 2,
                  background: overlay.colour,
                  opacity: on && !unavailable ? 1 : 0.35,
                  display: 'inline-block',
                }}
              />
              {overlay.label}
            </button>
          );
        })}
      </div>
      <div className="region-body" style={{ position: 'relative' }}>
        <div ref={container} style={{ height: 320, width: '100%' }} />
        {loading && (
          <div className="loading" style={{ position: 'absolute', top: '45%', left: '45%' }}>
            loading…
          </div>
        )}
        {error && <div className="error">{error}</div>}
        <p className="muted" style={{ fontSize: '0.6875rem', marginTop: '0.5rem' }}>
          Continuous contract, delayed. For reading the market — every P&amp;L
          figure in Shani comes from your fills, never from this series.
        </p>
      </div>
    </section>
  );
}
