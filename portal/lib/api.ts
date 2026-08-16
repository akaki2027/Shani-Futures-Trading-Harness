/**
 * Client for the Shani API.
 *
 * The portal holds no business logic — every number shown here was computed by
 * the Python core. That is what keeps a future mobile client a second client
 * rather than a reimplementation, and it is why P&L arithmetic must never creep
 * into a React component.
 *
 * Money arrives as strings and stays strings until the moment it is formatted.
 * JSON numbers are IEEE doubles, and parsing "400.10" into one is how a
 * dashboard ends up displaying 400.09999999999997.
 */

const BASE = '/api/shani';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = typeof window !== 'undefined' ? localStorage.getItem('shani_token') : null;
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    // Read the body exactly once. A Response body is a single-use stream, so
    // the obvious `try json() catch text()` pattern throws "body stream already
    // read" from inside the catch — which then replaces the real server error
    // with a misleading one and sends you debugging the wrong thing.
    const raw = await response.text();
    let detail: unknown = raw;
    try {
      const parsed = JSON.parse(raw);
      detail = parsed?.detail ?? parsed;
    } catch {
      // Not JSON — a proxy error page or an empty body. Keep the raw text.
    }
    throw new ApiError(describe(detail, response.status), response.status, detail);
  }
  return response.json() as Promise<T>;
}

/** Turn a rejection into something a trader can act on. */
function describe(detail: unknown, status: number): string {
  if (detail && typeof detail === 'object' && 'reasons' in detail) {
    const d = detail as { rule?: string; reasons?: string[] };
    return d.reasons?.join(' ') ?? d.rule ?? 'Rejected by the risk gate';
  }
  if (typeof detail === 'string' && detail) return detail;
  if (status === 401) return 'Not authorised. Set your API token.';
  return `Request failed (${status})`;
}

// ── types ────────────────────────────────────────────────────────────────────

export interface Health {
  status: string;
  broker: string;
  live_enabled: boolean;
  model: string;
}

export interface Quote {
  symbol: string;
  name: string;
  tv_symbol: string;
  last: string | null;
  change: string | null;
  change_percent: number | null;
  high: string | null;
  low: string | null;
  volume: number | null;
  as_of: number;
}

export interface Account {
  balance: string;
  equity: string;
  realized_pnl: string;
  unrealized_pnl: string;
  commission_paid: string;
  open_positions: number;
  broker: string;
  is_live: boolean;
  realized_today: string;
  remaining_daily_loss: string;
}

export interface Position {
  symbol: string;
  quantity: number;
  average_price: string;
  realized_pnl: string;
  mae: string;
  mfe: string;
}

export interface InterviewItem {
  question: string;
  answer: string;
  answered: boolean;
  latency_seconds: number | null;
}

export interface Trade {
  id: string;
  symbol: string;
  contract: string | null;
  side: string;
  quantity: number;
  entry_price: string;
  exit_price: string | null;
  entry_at: string;
  exit_at: string | null;
  net_pnl: string;
  gross_pnl: string;
  commission: string;
  r_multiple: number | null;
  outcome: string;
  is_open: boolean;
  session: string | null;
  time_of_day: string | null;
  followed_playbook: boolean;
  has_interview: boolean;
  setup_card_id: string | null;
  interview?: InterviewItem[];
  mae?: string;
  mfe?: string;
  planned_risk?: string | null;
  chart_timeframe?: string | null;
  chart_studies?: string[];
  notes?: string;
  tags?: string[];
}

export interface PerfSlice {
  label: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  net_pnl: string;
  avg_r: number | null;
}

export interface Stats {
  total_trades: number;
  wins: number;
  losses: number;
  breakeven: number;
  win_rate: number | null;
  net_pnl: string;
  gross_pnl: string;
  commission: string;
  largest_win: string;
  largest_loss: string;
  avg_r: number | null;
  expectancy: string | null;
  profit_factor: number | null;
  max_drawdown: string;
  by_time_of_day: PerfSlice[];
  by_session: PerfSlice[];
  by_symbol: PerfSlice[];
  worst_time_of_day: PerfSlice | null;
  best_time_of_day: PerfSlice | null;
}

export interface SetupStats {
  sample_size: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  net_pnl: string;
  avg_r: number | null;
  is_provisional: boolean;
  summary: string;
  by_time_of_day: { label: string; trades: number; net_pnl: string }[];
}

export interface SetupCard {
  id: string;
  name: string;
  slug: string;
  version: number;
  description: string;
  trigger: string;
  context: string;
  invalidation: string;
  management: string;
  instruments: string[];
  timeframes: string[];
  sample_size: number;
  is_meaningful: boolean;
  validated: boolean;
  stats: SetupStats;
}

export interface Evaluation {
  followed: PerfSlice;
  unfollowed: PerfSlice;
  has_enough_data: boolean;
  verdict: string;
  caveat: string;
}

export interface EquityPoint {
  at: string;
  equity: string;
  trade_id: string;
}

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface BarsResponse {
  symbol: string;
  name: string;
  interval: string;
  tick_size: string;
  bars: Bar[];
}

export interface ModelSettings {
  provider: string;
  triage_model: string;
  reasoning_model: string;
  base_url: string | null;
  temperature: number;
  key_env_var: string | null;
  has_key: boolean;
  /** Last four characters only. The key itself is never returned. */
  key_hint: string | null;
}

export interface CatalogueModel {
  id: string;
  name: string;
  context_length: number | null;
  prompt_per_m: string | null;
  completion_per_m: string | null;
  is_free: boolean;
  modalities: string[];
}

export interface NewsArticle {
  id: string;
  title: string;
  source: string;
  url: string;
  published_at: string;
  summary: string;
  symbols: string[];
  lean: string;
  lean_label: string;
  score: number;
  /** Per-market direction. Genuinely differs from `lean` — an oil shock is
   *  bullish CL and bearish ES in the same sentence. */
  market_leans: Record<string, string>;
  /** 0–1. Drives colour intensity, so a weak read renders pale. */
  confidence: number;
  rationale: string;
  age_minutes: number;
}

export interface MarketRead {
  symbol: string;
  lean: string;
  lean_label: string;
  score: number;
  bullish: number;
  bearish: number;
  neutral: number;
  unrated: number;
  headline: string;
}

export interface NewsConnector {
  id: string;
  name: string;
  description: string;
  requires_key: boolean;
  key_env_var: string | null;
  signup_url: string | null;
  available: boolean;
  ok?: boolean;
  count?: number;
  detail?: string | null;
}

export interface NewsPayload {
  items: NewsArticle[];
  digest: {
    lean: string;
    lean_label: string;
    score: number;
    bullish: number;
    bearish: number;
    neutral: number;
    unrated: number;
    headline: string;
  };
  markets: MarketRead[];
  connectors: NewsConnector[];
  classified: boolean;
}

export interface OrderResult {
  id: string;
  symbol: string;
  status: string;
  average_fill_price: string | null;
  reject_reason: string | null;
}

// ── endpoints ────────────────────────────────────────────────────────────────

export const api = {
  health: () => request<Health>('/health'),
  watchlist: () => request<{ quotes: Quote[]; error: string | null }>('/watchlist'),
  account: () => request<Account>('/account'),
  positions: () => request<Position[]>('/positions'),
  trades: (limit = 100) => request<Trade[]>(`/trades?limit=${limit}`),
  trade: (id: string) => request<Trade>(`/trades/${id}`),
  stats: () => request<Stats>('/stats'),
  equity: () => request<EquityPoint[]>('/equity'),
  playbook: () => request<SetupCard[]>('/playbook'),
  news: (refresh = false) =>
    request<NewsPayload>(`/news${refresh ? '?refresh=true' : ''}`),

  newsConnectors: () => request<NewsConnector[]>('/news/connectors'),

  saveConnector: (id: string, values: Record<string, string>) =>
    request<{ connectors: NewsConnector[] }>(`/news/connectors/${id}`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    }),

  modelSettings: () => request<ModelSettings>('/settings/model'),

  modelCatalogue: (provider = 'openrouter', refresh = false) =>
    request<{ provider: string; models: CatalogueModel[]; error: string | null }>(
      `/settings/models?provider=${provider}${refresh ? '&refresh=true' : ''}`,
    ),

  saveModelSettings: (body: {
    provider: string;
    triage_model?: string;
    reasoning_model?: string;
    base_url?: string | null;
    api_key?: string;
  }) => request<ModelSettings>('/settings/model', { method: 'POST', body: JSON.stringify(body) }),

  testModel: () => request<{ ok: boolean; detail: string }>('/settings/model/test', {
    method: 'POST',
  }),

  bars: (symbol: string, interval: string) =>
    request<BarsResponse>(`/bars/${symbol}?interval=${encodeURIComponent(interval)}`),
  evaluation: () => request<Evaluation>('/evaluation'),

  pushPrice: (symbol: string, price: string) =>
    request<{ fills: number }>('/price', {
      method: 'POST',
      body: JSON.stringify({ symbol, price }),
    }),

  submitOrder: (order: {
    symbol: string;
    side: string;
    quantity: number;
    stop_loss?: string;
    take_profit?: string;
  }) => request<OrderResult>('/orders', { method: 'POST', body: JSON.stringify(order) }),

  answer: (tradeId: string, index: number, answer: string) =>
    request<Trade>(`/trades/${tradeId}/interview`, {
      method: 'POST',
      body: JSON.stringify({ index, answer }),
    }),

  extract: (tradeId: string) =>
    request<{ card: SetupCard | null; reason: string | null }>(`/trades/${tradeId}/extract`, {
      method: 'POST',
    }),
};

// ── formatting ───────────────────────────────────────────────────────────────

export function money(value: string | null | undefined, sign = false): string {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  if (Number.isNaN(n)) return '—';
  const formatted = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const prefix = n < 0 ? '−' : sign ? '+' : '';
  return `${prefix}$${formatted}`;
}

export function price(value: string | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  return Number.isNaN(n) ? '—' : n.toLocaleString('en-US', { minimumFractionDigits: 2 });
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${(value * 100).toFixed(0)}%`;
}

export function rMultiple(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(2)}R`;
}

export function tone(value: string | number | null | undefined): string {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return '';
  return n > 0 ? 'up' : 'down';
}

export function shortTime(iso: string): string {
  return new Date(iso).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}
