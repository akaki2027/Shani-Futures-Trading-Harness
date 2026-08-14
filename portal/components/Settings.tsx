'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError, type CatalogueModel, type ModelSettings } from '@/lib/api';

const PROVIDERS = [
  { id: 'openrouter', label: 'OpenRouter', note: 'One key, several hundred models' },
  { id: 'anthropic', label: 'Anthropic', note: 'Claude, direct' },
  { id: 'openai', label: 'OpenAI', note: 'GPT, direct' },
  { id: 'ollama', label: 'Ollama', note: 'Local — nothing leaves this machine' },
  { id: 'none', label: 'Disabled', note: 'Journal and paper broker still work' },
] as const;

/**
 * Model settings.
 *
 * Two tiers rather than one model, because the call volumes differ by orders of
 * magnitude: triage runs on every incoming signal, extraction runs once per
 * closed trade. Prices are shown next to every option at the moment of choosing,
 * since putting an expensive model in the triage slot is the easy mistake and
 * the one that quietly costs money.
 *
 * The API key is write-only. It is stored in .env, never returned by any
 * endpoint, and the field only ever displays a four-character hint of what is
 * already saved.
 */
export function Settings({ onClose }: { onClose: () => void }) {
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [models, setModels] = useState<CatalogueModel[]>([]);
  const [catalogueError, setCatalogueError] = useState<string | null>(null);

  const [provider, setProvider] = useState('openrouter');
  const [triage, setTriage] = useState('');
  const [reasoning, setReasoning] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [filter, setFilter] = useState('');
  const [freeOnly, setFreeOnly] = useState(false);

  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    void api.modelSettings().then((s) => {
      setSettings(s);
      setProvider(s.provider);
      setTriage(s.triage_model);
      setReasoning(s.reasoning_model);
    });
  }, []);

  const loadCatalogue = useCallback((refresh = false) => {
    setCatalogueError(null);
    void api
      .modelCatalogue('openrouter', refresh)
      .then((r) => {
        setModels(r.models);
        setCatalogueError(r.error);
      })
      .catch((e: unknown) =>
        setCatalogueError(e instanceof Error ? e.message : 'Could not load models.'),
      );
  }, []);

  useEffect(() => {
    if (provider === 'openrouter') loadCatalogue();
  }, [provider, loadCatalogue]);

  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return models
      .filter((m) => !freeOnly || m.is_free)
      .filter((m) => !needle || m.id.toLowerCase().includes(needle) || m.name.toLowerCase().includes(needle))
      .slice(0, 60);
  }, [models, filter, freeOnly]);

  const save = async () => {
    setBusy(true);
    setFailure(null);
    setStatus(null);
    try {
      const saved = await api.saveModelSettings({
        provider,
        triage_model: triage,
        reasoning_model: reasoning,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      });
      setSettings(saved);
      setApiKey('');
      setStatus('Saved.');
    } catch (e) {
      setFailure(e instanceof ApiError ? e.message : 'Could not save.');
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    setBusy(true);
    setFailure(null);
    setStatus('Testing…');
    try {
      const r = await api.testModel();
      setStatus(r.ok ? `Working — ${r.detail}` : null);
      if (!r.ok) setFailure(r.detail);
    } catch (e) {
      setFailure(e instanceof ApiError ? e.message : 'Test failed.');
      setStatus(null);
    } finally {
      setBusy(false);
    }
  };

  const usesKey = provider !== 'ollama' && provider !== 'none';

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(6, 8, 12, 0.72)',
        display: 'grid',
        placeItems: 'center',
        padding: '1.5rem',
        zIndex: 60,
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
          maxWidth: 720,
          width: '100%',
          maxHeight: '88vh',
          overflowY: 'auto',
          padding: '1.5rem',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h1 style={{ marginBottom: '0.35rem' }}>Model</h1>
        <p className="muted" style={{ fontSize: '0.8125rem', marginTop: 0, lineHeight: 1.55 }}>
          Shani runs two tiers. Triage fires on every signal and wants to be
          cheap; extraction runs once per closed trade and wants to be good — a
          badly-written setup card poisons the playbook for months.
        </p>

        <div style={{ marginTop: '1.25rem' }}>
          <span className="label">Provider</span>
          <div style={{ display: 'grid', gap: '0.35rem', marginTop: '0.4rem' }}>
            {PROVIDERS.map((p) => (
              <button
                key={p.id}
                className={provider === p.id ? 'selected' : ''}
                aria-pressed={provider === p.id}
                onClick={() => setProvider(p.id)}
                style={{ textAlign: 'left', display: 'flex', gap: '0.6rem', alignItems: 'baseline' }}
              >
                <span style={{ color: 'var(--fg-000)' }}>{p.label}</span>
                <span className="muted" style={{ fontSize: '0.75rem' }}>{p.note}</span>
              </button>
            ))}
          </div>
        </div>

        {usesKey && (
          <div style={{ marginTop: '1.25rem' }}>
            <span className="label">
              API key{settings?.key_env_var ? ` — ${settings.key_env_var}` : ''}
            </span>
            <input
              type="password"
              value={apiKey}
              placeholder={
                settings?.has_key ? `Stored (${settings.key_hint}) — type to replace` : 'Paste key'
              }
              onChange={(e) => setApiKey(e.target.value)}
              style={{ marginTop: '0.3rem' }}
            />
            <p className="muted" style={{ fontSize: '0.6875rem', marginTop: '0.3rem' }}>
              Written to <code>.env</code>, which is gitignored. Never sent back to
              this page — the field above only shows the last four characters of
              whatever is already stored.
            </p>
          </div>
        )}

        {provider === 'openrouter' && (
          <div style={{ marginTop: '1.25rem' }}>
            <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
              <span className="label">Models</span>
              <span className="muted" style={{ fontSize: '0.6875rem' }}>
                {models.length} available · $ per million tokens
              </span>
              <button
                style={{ marginLeft: 'auto', padding: '0.15rem 0.4rem', fontSize: '0.7rem' }}
                onClick={() => loadCatalogue(true)}
              >
                Refresh
              </button>
            </div>

            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.4rem' }}>
              <input
                type="text"
                value={filter}
                placeholder="Filter — claude, gpt, llama…"
                onChange={(e) => setFilter(e.target.value)}
              />
              <button
                className={freeOnly ? 'selected' : ''}
                aria-pressed={freeOnly}
                onClick={() => setFreeOnly((v) => !v)}
                style={{ whiteSpace: 'nowrap' }}
              >
                Free only
              </button>
            </div>

            {catalogueError && (
              <div className="error" style={{ marginTop: '0.5rem' }}>{catalogueError}</div>
            )}

            <div style={{ marginTop: '0.6rem', maxHeight: 260, overflowY: 'auto' }}>
              <table>
                <thead>
                  <tr>
                    <th>Model</th>
                    <th className="right">In</th>
                    <th className="right">Out</th>
                    <th className="right">Use for</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((m) => (
                    <tr key={m.id}>
                      <td>
                        <span style={{ color: 'var(--fg-000)' }}>{m.name}</span>
                        <br />
                        <span className="muted" style={{ fontSize: '0.6875rem' }}>{m.id}</span>
                      </td>
                      <td className="right num">{m.prompt_per_m ?? '—'}</td>
                      <td className="right num">{m.completion_per_m ?? '—'}</td>
                      <td className="right" style={{ whiteSpace: 'nowrap' }}>
                        <button
                          className={triage === m.id ? 'selected' : ''}
                          style={{ padding: '0.15rem 0.4rem', fontSize: '0.7rem' }}
                          onClick={() => setTriage(m.id)}
                        >
                          triage
                        </button>{' '}
                        <button
                          className={reasoning === m.id ? 'selected' : ''}
                          style={{ padding: '0.15rem 0.4rem', fontSize: '0.7rem' }}
                          onClick={() => setReasoning(m.id)}
                        >
                          reasoning
                        </button>
                      </td>
                    </tr>
                  ))}
                  {filtered.length === 0 && !catalogueError && (
                    <tr>
                      <td colSpan={4} className="muted">
                        Nothing matches that filter.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div style={{ marginTop: '1.25rem', display: 'grid', gap: '0.5rem' }}>
          <label>
            <span className="label">Triage model — runs on every signal</span>
            <input type="text" value={triage} onChange={(e) => setTriage(e.target.value)} />
          </label>
          <label>
            <span className="label">Reasoning model — runs once per trade</span>
            <input type="text" value={reasoning} onChange={(e) => setReasoning(e.target.value)} />
          </label>
        </div>

        <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1.25rem', alignItems: 'center' }}>
          <button className="primary" disabled={busy} onClick={() => void save()}>
            {busy ? '…' : 'Save'}
          </button>
          <button disabled={busy} onClick={() => void test()}>
            Test connection
          </button>
          <button onClick={onClose}>Close</button>
          {status && (
            <span className="muted" style={{ fontSize: '0.75rem', color: 'var(--up-bright)' }}>
              {status}
            </span>
          )}
        </div>
        {failure && <div className="error" style={{ marginTop: '0.6rem' }}>{failure}</div>}
      </div>
    </div>
  );
}
