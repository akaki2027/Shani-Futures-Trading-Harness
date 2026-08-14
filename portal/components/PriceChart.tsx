'use client';

import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  LineStyle,
} from 'lightweight-charts';
import { useEffect, useRef, useState } from 'react';
import { api, type Bar } from '@/lib/api';

const INTERVALS = ['5m', '15m', '1h', '4h', '1d'] as const;

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

  const [interval, setInterval] = useState<string>('15m');
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
    };
  }, []);

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
