'use client';

import { createChart, type IChartApi, type ISeriesApi, LineStyle } from 'lightweight-charts';
import { useEffect, useRef } from 'react';
import type { EquityPoint } from '@/lib/api';

/**
 * The equity curve, drawn with TradingView's own open-source charting library.
 *
 * Themed from the design tokens rather than left on library defaults, because
 * a chart that does not match the surface around it is the single loudest
 * signal that a page was assembled rather than built. The starting balance is
 * marked with a reference line so drawdown is legible as distance below a line
 * rather than as a number the reader has to hold in their head.
 */
export function EquityChart({
  points,
  baseline,
}: {
  points: EquityPoint[];
  baseline: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<'Area'> | null>(null);

  useEffect(() => {
    if (!container.current) return;

    const styles = getComputedStyle(document.documentElement);
    const token = (name: string, fallback: string) =>
      styles.getPropertyValue(name).trim() || fallback;

    const instance = createChart(container.current, {
      layout: {
        background: { color: 'transparent' },
        textColor: token('--fg-300', '#6e6a63'),
        fontFamily: token('--font-ui', 'sans-serif'),
        fontSize: 11,
      },
      grid: {
        vertLines: { visible: false },
        horzLines: { color: token('--line', 'rgba(255,255,255,0.08)') },
      },
      rightPriceScale: { borderColor: token('--line', 'rgba(255,255,255,0.08)') },
      timeScale: {
        borderColor: token('--line', 'rgba(255,255,255,0.08)'),
        timeVisible: false,
      },
      crosshair: {
        vertLine: { color: token('--accent-dim', '#8a6620'), width: 1, style: LineStyle.Dotted },
        horzLine: { color: token('--accent-dim', '#8a6620'), width: 1, style: LineStyle.Dotted },
      },
      handleScale: false,
      handleScroll: false,
    });

    const area = instance.addAreaSeries({
      lineColor: token('--accent-bright', '#e0aa46'),
      topColor: 'rgba(201, 146, 46, 0.22)',
      bottomColor: 'rgba(201, 146, 46, 0.01)',
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
    });

    area.createPriceLine({
      price: baseline,
      color: token('--fg-300', '#6e6a63'),
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: false,
      title: 'start',
    });

    chart.current = instance;
    series.current = area;

    const resize = () => {
      if (container.current) {
        instance.applyOptions({ width: container.current.clientWidth });
      }
    };
    resize();
    window.addEventListener('resize', resize);

    return () => {
      window.removeEventListener('resize', resize);
      instance.remove();
      chart.current = null;
      series.current = null;
    };
  }, [baseline]);

  useEffect(() => {
    if (!series.current) return;
    // lightweight-charts requires strictly ascending, unique timestamps. Several
    // trades can close in the same second, so duplicates are nudged forward
    // rather than dropped — losing a trade from the curve would misstate the
    // final equity.
    let previous = 0;
    const data = points.map((point) => {
      let time = Math.floor(new Date(point.at).getTime() / 1000);
      if (time <= previous) time = previous + 1;
      previous = time;
      return { time: time as never, value: Number(point.equity) };
    });
    series.current.setData(data);
    chart.current?.timeScale().fitContent();
  }, [points]);

  if (points.length === 0) {
    return (
      <div className="empty">
        No closed trades yet. The curve appears once you have taken and exited a
        position — run <code>shani demo</code> to seed synthetic history if you
        want to see the shape of it first.
      </div>
    );
  }

  return <div ref={container} style={{ height: 220, width: '100%' }} />;
}
