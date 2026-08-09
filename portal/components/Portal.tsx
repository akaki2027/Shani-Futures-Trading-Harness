'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  api,
  ApiError,
  money,
  percent,
  price,
  rMultiple,
  shortTime,
  tone,
  type Account,
  type Evaluation,
  type EquityPoint,
  type Health,
  type Position,
  type Quote,
  type SetupCard,
  type Stats,
  type Trade,
} from '@/lib/api';
import { EquityChart } from './EquityChart';

/* Icons are drawn, not borrowed from the emoji table — one stroke weight, one
   grid, sized to the type they sit beside. */
const Ring = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <circle cx="12" cy="12" r="5.5" stroke="var(--accent-bright)" strokeWidth="1.5" />
    <ellipse
      cx="12"
      cy="12"
      rx="10.5"
      ry="3.4"
      stroke="var(--accent)"
      strokeWidth="1.5"
      transform="rotate(-20 12 12)"
    />
  </svg>
);

const Shield = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path
      d="M12 3l7 3v6c0 4.4-3 8.2-7 9-4-.8-7-4.6-7-9V6l7-3z"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinejoin="round"
    />
  </svg>
);

export function Portal() {
  const [health, setHealth] = useState<Health | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [playbook, setPlaybook] = useState<SetupCard[]>([]);
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [selected, setSelected] = useState<string>('ES');
  const [openTrade, setOpenTrade] = useState<Trade | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, a, w, p, t, s, e, pb, ev] = await Promise.all([
        api.health(),
        api.account(),
        api.watchlist(),
        api.positions(),
        api.trades(60),
        api.stats(),
        api.equity(),
        api.playbook(),
        api.evaluation(),
      ]);
      setHealth(h);
      setAccount(a);
      setQuotes(w.quotes);
      setQuoteError(w.error);
      setPositions(p);
      setTrades(t);
      setStats(s);
      setEquity(e);
      setPlaybook(pb);
      setEvaluation(ev);
      setNotice(null);
    } catch (error) {
      setNotice(error instanceof ApiError ? error.message : 'Cannot reach the Shani API.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 15_000);
    return () => clearInterval(timer);
  }, [refresh]);

  if (loading) {
    return <div className="loading">Connecting to Shani…</div>;
  }

  const live = health?.live_enabled ?? false;

  return (
    <div className="shell">
      <header className="masthead">
        <span className="wordmark">
          <Ring />
          Shani
        </span>
        {account && (
          <div className="masthead-stats">
            <div className="stat">
              <span className="label">Equity</span>
              <span className="stat-value num">{money(account.equity)}</span>
            </div>
            <div className="stat">
              <span className="label">Session P&amp;L</span>
              <span className={`stat-value num ${tone(account.realized_today)}`}>
                {money(account.realized_today, true)}
              </span>
            </div>
            <div className="stat">
              <span className="label">Loss budget left</span>
              <span className="stat-value num">{money(account.remaining_daily_loss)}</span>
            </div>
            <div className="stat">
              <span className="label">Open</span>
              <span className="stat-value num">{account.open_positions}</span>
            </div>
          </div>
        )}
      </header>

      {/* Persistent, not dismissible. Whether this screen can move real money is
          not something a trader should have to go and check. */}
      <div className={`mode-banner${live ? ' live' : ''}`}>
        <Shield />
        {live ? (
          <span>
            <strong>Live trading is enabled.</strong> Orders can reach a real
            account. Nothing here is financial advice.
          </span>
        ) : (
          <span>
            Paper trading — no real money. Simulated fills are optimistic: they
            model slippage and commission, not queue position or a fast market.
          </span>
        )}
      </div>

      {notice && <div className="error" style={{ margin: '0.75rem 1.5rem' }}>{notice}</div>}

      <div className="workspace">
        <div className="column">
          <Watchlist
            quotes={quotes}
            error={quoteError}
            selected={selected}
            onSelect={setSelected}
          />
          <Positions positions={positions} />
        </div>

        <div className="column">
          <section className="region">
            <div className="region-head">
              <h2>Equity</h2>
              {stats && (
                <span className="muted" style={{ marginLeft: 'auto', fontSize: '0.75rem' }}>
                  {stats.total_trades} trades · max drawdown{' '}
                  <span className="num down">{money(stats.max_drawdown)}</span>
                </span>
              )}
            </div>
            <div className="region-body">
              <EquityChart points={equity} baseline={100000} />
            </div>
          </section>

          {stats && <TimeOfDay stats={stats} />}
          <Journal trades={trades} onOpen={setOpenTrade} />
        </div>

        <div className="column">
          <Ticket symbol={selected} quotes={quotes} onDone={refresh} />
          {evaluation && <PlaybookCheck evaluation={evaluation} />}
          <Playbook cards={playbook} />
        </div>
      </div>

      {openTrade && (
        <Interview
          tradeId={openTrade.id}
          onClose={() => {
            setOpenTrade(null);
            void refresh();
          }}
        />
      )}
    </div>
  );
}

function Watchlist({
  quotes,
  error,
  selected,
  onSelect,
}: {
  quotes: Quote[];
  error: string | null;
  selected: string;
  onSelect: (symbol: string) => void;
}) {
  return (
    <section className="region">
      <div className="region-head">
        <h2>Watchlist</h2>
      </div>
      <div className="region-body settle">
        {error && (
          <div className="error" style={{ marginBottom: '0.5rem' }}>
            Market data unavailable. Showing what is known. {error.slice(0, 90)}
          </div>
        )}
        {quotes.length === 0 && !error && (
          <div className="empty">No instruments configured.</div>
        )}
        {quotes.map((quote) => (
          <button
            key={quote.symbol}
            className="quote"
            aria-pressed={quote.symbol === selected}
            onClick={() => onSelect(quote.symbol)}
          >
            <span>
              <span className="quote-symbol">{quote.symbol}</span>
              <br />
              <span className="quote-name">{quote.name}</span>
            </span>
            <span style={{ textAlign: 'right' }}>
              <span className="quote-price">{price(quote.last)}</span>
              <br />
              <span className={`quote-change ${tone(quote.change_percent)}`}>
                {quote.change_percent === null
                  ? '—'
                  : `${quote.change_percent >= 0 ? '+' : '−'}${Math.abs(
                      quote.change_percent,
                    ).toFixed(2)}%`}
              </span>
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

function Positions({ positions }: { positions: Position[] }) {
  return (
    <section className="region">
      <div className="region-head">
        <h2>Positions</h2>
      </div>
      <div className="region-body">
        {positions.length === 0 ? (
          <div className="empty">Flat.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="right">Qty</th>
                <th className="right">Avg</th>
                <th className="right">MAE</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((position) => (
                <tr key={position.symbol}>
                  <td>{position.symbol}</td>
                  <td className={`right num ${position.quantity > 0 ? 'up' : 'down'}`}>
                    {position.quantity > 0 ? '+' : ''}
                    {position.quantity}
                  </td>
                  <td className="right num">{price(position.average_price)}</td>
                  <td className="right num down">{money(position.mae)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

/**
 * Performance by time of day.
 *
 * For a futures trader this is usually the most useful view in the application:
 * most people have one part of the day quietly funding the rest, and it is
 * invisible in a P&L column. Bars diverge from a centre line so losing sessions
 * read as direction rather than as a number needing interpretation.
 */
function TimeOfDay({ stats }: { stats: Stats }) {
  const slices = stats.by_time_of_day;
  const scale = Math.max(...slices.map((s) => Math.abs(Number(s.net_pnl))), 1);

  return (
    <section className="region">
      <div className="region-head">
        <h2>When you make and lose money</h2>
      </div>
      <div className="region-body settle">
        {slices.length === 0 ? (
          <div className="empty">No closed trades yet.</div>
        ) : (
          <>
            {slices.map((slice) => {
              const value = Number(slice.net_pnl);
              const width = (Math.abs(value) / scale) * 50;
              return (
                <div className="heat-row" key={slice.label}>
                  <span className="muted">{slice.label}</span>
                  <span className="heat-track">
                    <span className="heat-zero" style={{ left: '50%' }} />
                    <span
                      className="heat-fill"
                      style={{
                        width: `${width}%`,
                        left: value >= 0 ? '50%' : `${50 - width}%`,
                        background: value >= 0 ? 'var(--up)' : 'var(--down)',
                      }}
                    />
                  </span>
                  <span className={`right num ${tone(slice.net_pnl)}`}>
                    {money(slice.net_pnl, true)}
                  </span>
                </div>
              );
            })}
            {stats.worst_time_of_day && (
              <p style={{ marginTop: '0.75rem', fontSize: '0.8125rem', lineHeight: 1.5 }}>
                <strong style={{ color: 'var(--fg-000)' }}>
                  {stats.worst_time_of_day.label}
                </strong>{' '}
                costs you{' '}
                <span className="num down">{money(stats.worst_time_of_day.net_pnl)}</span> across{' '}
                {stats.worst_time_of_day.trades} trades, winning{' '}
                {percent(stats.worst_time_of_day.win_rate)} of them.
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function Journal({ trades, onOpen }: { trades: Trade[]; onOpen: (trade: Trade) => void }) {
  const pending = trades.filter((t) => !t.is_open && !t.has_interview);

  return (
    <section className="region">
      <div className="region-head">
        <h2>Journal</h2>
        {pending.length > 0 && (
          <span className="provisional" style={{ marginLeft: 'auto' }}>
            {pending.length} awaiting interview
          </span>
        )}
      </div>
      <div className="region-body">
        {trades.length === 0 ? (
          <div className="empty">
            No trades yet. Place one from the ticket, or run{' '}
            <code>shani demo</code> to seed synthetic history.
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Symbol</th>
                <th>Session</th>
                <th className="right">P&amp;L</th>
                <th className="right">R</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {trades.slice(0, 25).map((trade) => (
                <tr key={trade.id}>
                  <td className="muted num">{shortTime(trade.entry_at)}</td>
                  <td>
                    {trade.side === 'buy' ? '↑' : '↓'} {trade.symbol}
                    {trade.followed_playbook && (
                      <span title="Followed the playbook" style={{ color: 'var(--accent)' }}>
                        {' '}
                        ◆
                      </span>
                    )}
                  </td>
                  <td className="muted">{trade.time_of_day ?? '—'}</td>
                  <td className={`right num ${tone(trade.net_pnl)}`}>
                    {trade.is_open ? 'open' : money(trade.net_pnl, true)}
                  </td>
                  <td className="right num">{rMultiple(trade.r_multiple)}</td>
                  <td className="right">
                    {!trade.is_open &&
                      (trade.has_interview ? (
                        <span className="answered-mark" title="Interviewed">
                          ✓
                        </span>
                      ) : (
                        <button onClick={() => onOpen(trade)}>Why?</button>
                      ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

/**
 * The trade ticket.
 *
 * A first-class surface, because trades are placed here — if this is not good
 * to use, the journal never gets any data and the whole system has nothing to
 * learn from. The stop is required rather than optional: the risk gate refuses
 * entries without one, and a form that lets you submit something guaranteed to
 * be rejected is a form that wastes your time at the worst moment.
 */
function Ticket({
  symbol,
  quotes,
  onDone,
}: {
  symbol: string;
  quotes: Quote[];
  onDone: () => void;
}) {
  const [quantity, setQuantity] = useState(1);
  const [stop, setStop] = useState('');
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const quote = quotes.find((q) => q.symbol === symbol);
  const last = quote?.last ?? null;

  const submit = async (side: 'buy' | 'sell') => {
    setBusy(true);
    setFailure(null);
    setResult(null);
    try {
      if (last) await api.pushPrice(symbol, last);
      const order = await api.submitOrder({
        symbol,
        side,
        quantity,
        ...(stop ? { stop_loss: stop } : {}),
        ...(target ? { take_profit: target } : {}),
      });
      setResult(
        order.status === 'filled'
          ? `Filled ${quantity} ${symbol} at ${price(order.average_fill_price)}`
          : `Order ${order.status}`,
      );
      onDone();
    } catch (error) {
      setFailure(error instanceof ApiError ? error.message : 'Order failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="region">
      <div className="region-head">
        <h2>Ticket</h2>
        <span className="muted" style={{ marginLeft: 'auto' }}>
          {symbol} {last ? price(last) : ''}
        </span>
      </div>
      <div className="region-body">
        <div style={{ display: 'grid', gap: '0.6rem' }}>
          <label>
            <span className="label">Contracts</span>
            <input
              type="number"
              min={1}
              value={quantity}
              onChange={(event) => setQuantity(Math.max(1, Number(event.target.value)))}
            />
          </label>
          <label>
            <span className="label">Stop — required</span>
            <input
              type="text"
              inputMode="decimal"
              value={stop}
              placeholder="e.g. 4990.00"
              onChange={(event) => setStop(event.target.value)}
            />
          </label>
          <label>
            <span className="label">Target</span>
            <input
              type="text"
              inputMode="decimal"
              value={target}
              placeholder="e.g. 5020.00"
              onChange={(event) => setTarget(event.target.value)}
            />
          </label>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.5rem' }}>
            <button className="buy" disabled={busy || !stop} onClick={() => void submit('buy')}>
              {busy ? '…' : 'Buy'}
            </button>
            <button className="sell" disabled={busy || !stop} onClick={() => void submit('sell')}>
              {busy ? '…' : 'Sell'}
            </button>
          </div>

          {!stop && (
            <p className="muted" style={{ fontSize: '0.75rem', margin: 0 }}>
              A stop is required. Shani refuses entries without one — decide where
              you are wrong before you are in.
            </p>
          )}
          {result && <div className="num" style={{ color: 'var(--up-bright)' }}>{result}</div>}
          {failure && <div className="error">{failure}</div>}
        </div>
      </div>
    </section>
  );
}

function PlaybookCheck({ evaluation }: { evaluation: Evaluation }) {
  return (
    <section className="region">
      <div className="region-head">
        <h2>Is this helping?</h2>
      </div>
      <div className="region-body">
        <p style={{ fontSize: '0.8125rem', lineHeight: 1.55, margin: 0 }}>
          {evaluation.verdict}
        </p>
      </div>
    </section>
  );
}

function Playbook({ cards }: { cards: SetupCard[] }) {
  return (
    <section className="region">
      <div className="region-head">
        <h2>Playbook</h2>
      </div>
      <div className="region-body">
        {cards.length === 0 ? (
          <div className="empty">
            Nothing learned yet. Setups appear here after you answer the interview
            on a few trades — Shani writes them from your own words, not from a
            textbook.
          </div>
        ) : (
          cards.map((card) => (
            <div key={card.id} style={{ padding: '0.7rem 0', borderBottom: '1px solid var(--line)' }}>
              <h3 style={{ marginBottom: '0.2rem' }}>
                {card.name}{' '}
                <span className="muted" style={{ fontSize: '0.6875rem', fontWeight: 400 }}>
                  v{card.version}
                </span>
              </h3>
              {card.trigger && (
                <p style={{ fontSize: '0.75rem', margin: '0.25rem 0', lineHeight: 1.5 }}>
                  {card.trigger}
                </p>
              )}
              <div style={{ fontSize: '0.75rem', marginTop: '0.35rem' }}>
                <span className={`num ${tone(card.stats.net_pnl)}`}>
                  {money(card.stats.net_pnl, true)}
                </span>{' '}
                <span className="muted">
                  · {card.stats.sample_size} trades · {percent(card.stats.win_rate)} won
                </span>{' '}
                {card.stats.is_provisional && (
                  <span className="provisional">provisional</span>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

/**
 * The post-trade interview.
 *
 * Deliberately plain and fast to complete. Every second between the fill and
 * the answer degrades what gets written down: an answer given hours later is a
 * reconstruction, and reconstructions are tidy, flattering, and useless for
 * learning. Answers save as you leave each field, so a half-finished interview
 * still captures something.
 */
function Interview({ tradeId, onClose }: { tradeId: string; onClose: () => void }) {
  const [trade, setTrade] = useState<Trade | null>(null);
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [extracted, setExtracted] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void api.trade(tradeId).then((loaded) => {
      setTrade(loaded);
      if (!loaded.interview?.length) {
        void api.answer(tradeId, 0, '').then(setTrade).catch(() => undefined);
      }
    });
  }, [tradeId]);

  const save = async (index: number) => {
    const answer = drafts[index];
    if (!answer?.trim()) return;
    setTrade(await api.answer(tradeId, index, answer));
  };

  const finish = async () => {
    setBusy(true);
    try {
      const outcome = await api.extract(tradeId);
      setExtracted(
        outcome.card ? `Learned: ${outcome.card.name}` : (outcome.reason ?? 'No card written.'),
      );
    } catch {
      setExtracted('Could not extract a setup — is a model provider configured?');
    } finally {
      setBusy(false);
    }
  };

  if (!trade) return null;

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(6, 8, 12, 0.72)',
        display: 'grid',
        placeItems: 'center',
        padding: '1.5rem',
        zIndex: 50,
      }}
      onClick={onClose}
    >
      <div
        className="settle"
        style={{
          background: 'var(--ground-050)',
          border: '1px solid var(--line-strong)',
          borderRadius: 4,
          boxShadow: 'var(--shadow-high)',
          maxWidth: 620,
          width: '100%',
          maxHeight: '86vh',
          overflowY: 'auto',
          padding: '1.5rem',
        }}
        onClick={(event) => event.stopPropagation()}
      >
        <h1 style={{ marginBottom: '0.35rem' }}>
          {trade.symbol} {trade.side} — {money(trade.net_pnl, true)}
        </h1>
        <p className="muted" style={{ fontSize: '0.8125rem', marginTop: 0 }}>
          {trade.time_of_day} · {rMultiple(trade.r_multiple)} · answer now, while you
          still remember what you actually saw.
        </p>

        {trade.interview?.map((item, index) => (
          <div className="interview-question" key={index}>
            <div className="question-text">
              {item.question}{' '}
              {item.answered && <span className="answered-mark">✓</span>}
            </div>
            <textarea
              defaultValue={item.answer}
              placeholder="In your own words."
              onChange={(event) =>
                setDrafts((current) => ({ ...current, [index]: event.target.value }))
              }
              onBlur={() => void save(index)}
            />
          </div>
        ))}

        <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1rem', alignItems: 'center' }}>
          <button className="primary" disabled={busy} onClick={() => void finish()}>
            {busy ? 'Thinking…' : 'Save and learn from this'}
          </button>
          <button onClick={onClose}>Close</button>
          {extracted && (
            <span className="muted" style={{ fontSize: '0.75rem' }}>
              {extracted}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
