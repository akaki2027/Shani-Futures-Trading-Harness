'use client';

import { useCallback, useEffect, useState } from 'react';
import { api, ApiError, type NewsPayload } from '@/lib/api';

const LEGEND: { lean: string; label: string }[] = [
  { lean: 'strong_bearish', label: 'strong down' },
  { lean: 'bearish', label: 'leans down' },
  { lean: 'neutral', label: 'no signal' },
  { lean: 'bullish', label: 'leans up' },
  { lean: 'strong_bullish', label: 'strong up' },
];

/**
 * The news desk.
 *
 * Built to answer one question in under a second: does this give the market a
 * reason to go up or down? Every other news UI shows headlines; a trader at
 * 09:15 has ninety seconds and a wall of undifferentiated text costs time
 * without changing a decision.
 *
 * Colour rides on a left rail rather than a background wash, so a dense list
 * stays readable and the leans form a column you can scan vertically. Intensity
 * follows the model's confidence — a weak read renders pale, so a guess never
 * carries the visual weight of a rate decision.
 *
 * Unrated items are grey, never yellow. "We have not read this" and "this is
 * balanced" are different claims and only one of them is honest.
 */
export function News({ onOpenConnectors }: { onOpenConnectors: () => void }) {
  const [data, setData] = useState<NewsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<'all' | 'directional'>('all');

  const load = useCallback(async (refresh = false) => {
    setBusy(true);
    setError(null);
    try {
      setData(await api.news(refresh));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load news.');
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // Five minutes matches the server cache; polling faster just burns calls.
    const timer = setInterval(() => void load(), 300_000);
    return () => clearInterval(timer);
  }, [load]);

  const items = (data?.items ?? []).filter(
    (i) => filter === 'all' || (i.lean !== 'neutral' && i.lean !== 'unrated'),
  );
  const down = data?.connectors.filter((c) => !c.ok && c.detail !== 'not configured') ?? [];

  return (
    <section className="region">
      <div className="region-head">
        <h2>News</h2>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: '0.3rem' }}>
          <button
            className={filter === 'directional' ? 'selected' : ''}
            aria-pressed={filter === 'directional'}
            style={{ padding: '0.15rem 0.45rem', fontSize: '0.7rem' }}
            onClick={() => setFilter((f) => (f === 'all' ? 'directional' : 'all'))}
          >
            Signal only
          </button>
          <button
            style={{ padding: '0.15rem 0.45rem', fontSize: '0.7rem' }}
            onClick={onOpenConnectors}
          >
            Sources
          </button>
          <button
            style={{ padding: '0.15rem 0.45rem', fontSize: '0.7rem' }}
            disabled={busy}
            onClick={() => void load(true)}
          >
            {busy ? '…' : 'Refresh'}
          </button>
        </div>
      </div>

      <div className="region-body">
        {error && <div className="error">{error}</div>}

        {data?.digest && (
          <div className="digest" data-lean={data.digest.lean}>
            <span className="digest-dot" data-lean={data.digest.lean} />
            <span>{data.digest.headline}</span>
          </div>
        )}

        {/* Per-market read. An oil supply shock is bullish crude and bearish
            equities in the same sentence, so one blended number would describe
            neither market. */}
        {data && data.markets.length > 0 && (
          <div className="market-strip">
            {data.markets.map((m) => (
              <div
                key={m.symbol}
                className="market-chip"
                data-lean={m.lean}
                title={m.headline}
              >
                <b>{m.symbol}</b>
                <span>
                  {m.lean === 'unrated' ? 'no news' : m.lean_label.toLowerCase()}
                </span>
              </div>
            ))}
          </div>
        )}

        {data && !data.classified && (
          <p className="muted" style={{ fontSize: '0.6875rem', marginTop: '0.5rem' }}>
            No model configured, so nothing is rated. Add a provider in settings
            and headlines get a directional read.
          </p>
        )}

        {down.length > 0 && (
          <div className="error" style={{ marginTop: '0.5rem' }}>
            {down.map((c) => `${c.name}: ${c.detail}`).join(' · ')}
          </div>
        )}

        <div className="legend">
          {LEGEND.map((l) => (
            <span key={l.lean}>
              <i className="news-rail" data-lean={l.lean} style={{ height: 3, width: 12 }} />
              {l.label}
            </span>
          ))}
        </div>

        {items.length === 0 && !busy && !error && (
          <div className="empty">
            {filter === 'directional'
              ? 'Nothing in the feed is arguing a direction right now. That is usually the truth rather than a fault.'
              : 'No headlines. Check your sources.'}
          </div>
        )}

        {items.map((item) => (
          <div className="news-item settle" key={item.id}>
            {/* Opacity carries confidence: a weak read renders pale. */}
            <span
              className="news-rail"
              data-lean={item.lean}
              style={{
                opacity:
                  item.lean === 'unrated' ? 1 : Math.max(0.28, Math.min(1, item.confidence + 0.25)),
              }}
              title={`${item.lean_label} · confidence ${item.confidence}`}
            />
            <div>
              <a
                className="news-title"
                data-lean={item.lean}
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {item.title}
              </a>
              <div className="news-meta">
                <span>{item.source}</span>
                <span>{formatAge(item.age_minutes)}</span>
                {item.symbols.length > 0 && (
                  <span style={{ color: 'var(--accent)' }}>{item.symbols.join(' · ')}</span>
                )}
                {item.lean !== 'unrated' && item.lean !== 'neutral' && (
                  <span>{item.lean_label}</span>
                )}
              </div>
              {item.rationale && <div className="news-why">{item.rationale}</div>}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

/**
 * Connector settings. Credentials are write-only, same as the model key.
 */
export function Connectors({ onClose }: { onClose: () => void }) {
  const [connectors, setConnectors] = useState<NewsPayload['connectors']>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const reload = useCallback(() => {
    void api.newsConnectors().then(setConnectors);
  }, []);

  useEffect(reload, [reload]);

  const save = async (id: string, keys: string[]) => {
    const payload: Record<string, string> = {};
    for (const key of keys) {
      const v = values[key];
      if (v?.trim()) payload[key] = v.trim();
    }
    if (Object.keys(payload).length === 0) {
      setFailure('Nothing to save.');
      return;
    }
    setFailure(null);
    try {
      await api.saveConnector(id, payload);
      setStatus('Saved.');
      setValues({});
      reload();
    } catch (e) {
      setFailure(e instanceof ApiError ? e.message : 'Could not save.');
    }
  };

  return (
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(6,8,12,0.72)',
        display: 'grid', placeItems: 'center', padding: '1.5rem', zIndex: 60,
      }}
      onClick={onClose}
    >
      <div
        className="settle"
        style={{
          background: 'var(--ground-050)', border: '1px solid var(--line-strong)',
          borderRadius: 4, boxShadow: 'var(--shadow-high)', maxWidth: 620,
          width: '100%', maxHeight: '86vh', overflowY: 'auto', padding: '1.5rem',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h1 style={{ marginBottom: '0.35rem' }}>News sources</h1>
        <p className="muted" style={{ fontSize: '0.8125rem', marginTop: 0, lineHeight: 1.55 }}>
          Newswires work with no account. Reddit and X need credentials you
          create yourself — stored in <code>.env</code>, never sent back to this
          page.
        </p>

        {connectors.map((c) => (
          <div key={c.id} style={{ padding: '0.85rem 0', borderBottom: '1px solid var(--line)' }}>
            <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'baseline' }}>
              <strong style={{ color: 'var(--fg-000)' }}>{c.name}</strong>
              <span
                className="muted"
                style={{ fontSize: '0.6875rem', color: c.available ? 'var(--up-bright)' : undefined }}
              >
                {c.available ? 'active' : c.requires_key ? 'needs a key' : 'unavailable'}
              </span>
            </div>
            <p className="muted" style={{ fontSize: '0.75rem', margin: '0.2rem 0 0.4rem' }}>
              {c.description}
            </p>

            {c.requires_key && (
              <>
                <div style={{ display: 'grid', gap: '0.35rem' }}>
                  {keysFor(c.id).map((key) => (
                    <input
                      key={key}
                      type="password"
                      placeholder={key}
                      value={values[key] ?? ''}
                      onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
                    />
                  ))}
                </div>
                <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.4rem' }}>
                  <button onClick={() => void save(c.id, keysFor(c.id))}>Save</button>
                  {c.signup_url && (
                    <a
                      href={c.signup_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ fontSize: '0.75rem', alignSelf: 'center' }}
                    >
                      get credentials
                    </a>
                  )}
                </div>
              </>
            )}
          </div>
        ))}

        <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1rem', alignItems: 'center' }}>
          <button onClick={onClose}>Close</button>
          {status && (
            <span style={{ fontSize: '0.75rem', color: 'var(--up-bright)' }}>{status}</span>
          )}
        </div>
        {failure && <div className="error" style={{ marginTop: '0.5rem' }}>{failure}</div>}
      </div>
    </div>
  );
}

/** Which env vars a connector needs. Reddit takes two; X takes one. */
function keysFor(id: string): string[] {
  if (id === 'reddit') return ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET'];
  if (id === 'x') return ['X_BEARER_TOKEN'];
  return [];
}

function formatAge(minutes: number): string {
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
