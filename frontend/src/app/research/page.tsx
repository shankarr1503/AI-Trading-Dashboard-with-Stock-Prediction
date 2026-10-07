'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { FiFilter } from 'react-icons/fi';
import ResearchReport from '@/components/research/ResearchReport';
import { errorMessage, researchApi } from '@/lib/api';
import { useAuth } from '@/lib/useAuth';

const box = { background: '#1a1d24', border: '1px solid #1e2535' };
const muted = { color: '#5a6478' };

function cell(v: number | null | undefined, d = 0, suffix = '') {
  return v === null || v === undefined ? '—' : `${Number(v).toFixed(d)}${suffix}`;
}

function scoreColor(v: number | null | undefined) {
  if (v === null || v === undefined) return '#5a6478';
  return v >= 65 ? '#00d4aa' : v >= 40 ? '#ffd700' : '#ff4757';
}

export default function ResearchPage() {
  const { user, loading: authLoading } = useAuth();
  const [universes, setUniverses] = useState<Record<string, string[]>>({});
  const [universe, setUniverse] = useState('us_large_cap');
  const [custom, setCustom] = useState('');
  const [rows, setRows] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (user) researchApi.universes().then((r) => setUniverses(r.data.universes)).catch(() => {});
  }, [user]);

  const run = async () => {
    setBusy(true);
    setError('');
    try {
      const symbols = custom.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
      const r = await researchApi.screen(symbols.length ? { symbols } : { universe });
      setRows(r.data.results);
    } catch (e) {
      setError(errorMessage(e, 'Screen failed'));
    }
    setBusy(false);
  };

  if (authLoading) return <div className="p-8" style={muted}>Loading…</div>;
  if (!user) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: '#0a0b0d' }}>
        <p style={{ color: '#9ba3b8' }}><Link href="/login" style={{ color: '#4fa3ff' }}>Sign in</Link> to use the research screener.</p>
      </div>
    );
  }

  const factors = ['value', 'quality', 'growth', 'momentum', 'low_risk', 'street'];
  return (
    <div className="min-h-screen p-4 space-y-4" style={{ background: '#0a0b0d' }}>
      <div className="flex items-center gap-3">
        <Link href="/dashboard" className="text-xs" style={{ color: '#4fa3ff' }}>← Dashboard</Link>
        <h1 className="text-lg font-bold flex items-center gap-2" style={{ color: '#e8eaf0' }}><FiFilter style={{ color: '#4fa3ff' }} /> Equity Research Screener</h1>
      </div>

      <div className="card p-4 flex flex-wrap gap-2 items-center">
        <select value={universe} onChange={(e) => setUniverse(e.target.value)} className="rounded-lg px-2 py-1.5 text-xs" style={{ ...box, color: '#e8eaf0' }}>
          {Object.keys(universes).map((u) => <option key={u} value={u}>{u.replace(/_/g, ' ')} ({universes[u].length})</option>)}
        </select>
        <span className="text-xs" style={muted}>or</span>
        <input value={custom} onChange={(e) => setCustom(e.target.value)}
          placeholder={user.is_superuser ? 'custom symbols, e.g. AAPL,MSFT,TCS.NS' : 'subset of the universes, e.g. AAPL,MSFT,TCS.NS'}
          title={user.is_superuser ? undefined : 'Non-admin screens are limited to symbols in the predefined universes (max 40)'}
          className="rounded-lg px-2 py-1.5 text-xs font-mono flex-1 min-w-[240px]" style={{ ...box, color: '#e8eaf0' }} />
        <button onClick={run} disabled={busy} className="px-3 py-2 rounded-lg text-xs font-semibold" style={{ background: '#4fa3ff', color: 'white', opacity: busy ? 0.6 : 1 }}>
          {busy ? 'Scoring… (first run fetches fundamentals)' : 'Run screen'}
        </button>
        {error && <span className="text-xs" style={{ color: '#ff4757' }}>{error}</span>}
      </div>

      {rows.length > 0 && (
        <div className="card p-4 overflow-x-auto">
          <table className="w-full text-xs font-mono">
            <thead>
              <tr style={muted}>
                {['#', 'Symbol', 'Sector', 'Composite', ...factors, 'Fair value Δ', 'Street Δ', 'P/E', 'F-score', 'Altman', 'Tech'].map((h) => (
                  <th key={h} className="text-left py-1.5 pr-3 font-semibold">{h.replace('_', ' ')}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => r.error ? (
                <tr key={r.symbol} style={muted}><td className="py-1">—</td><td>{r.symbol}</td><td colSpan={15}>{r.error}</td></tr>
              ) : (
                <tr key={r.symbol} onClick={() => setSelected(r.symbol)} className="cursor-pointer hover:bg-white/5"
                  style={{ color: '#e8eaf0', background: selected === r.symbol ? 'rgba(79,163,255,0.08)' : undefined }}>
                  <td className="py-1.5 pr-3" style={muted}>{i + 1}</td>
                  <td className="pr-3" style={{ color: '#4fa3ff' }}>{r.symbol}</td>
                  <td className="pr-3 truncate max-w-[120px]" style={{ color: '#9ba3b8' }}>{r.sector}</td>
                  <td className="pr-3 font-bold" style={{ color: scoreColor(r.scores.composite) }}>{cell(r.scores.composite)}</td>
                  {factors.map((k) => <td key={k} className="pr-3" style={{ color: scoreColor(r.scores[k]) }}>{cell(r.scores[k])}</td>)}
                  <td className="pr-3">{cell(r.upside_pct, 0, '%')}</td>
                  <td className="pr-3">{cell(r.street_upside_pct, 0, '%')}</td>
                  <td className="pr-3">{cell(r.pe, 1)}</td>
                  <td className="pr-3">{r.piotroski ?? '—'}</td>
                  <td className="pr-3" style={{ color: r.altman_zone === 'distress' ? '#ff4757' : '#9ba3b8' }}>{r.altman_zone}</td>
                  <td className="pr-3" style={{ color: r.technical_signal === 'BUY' ? '#00d4aa' : r.technical_signal === 'SELL' ? '#ff4757' : '#9ba3b8' }}>{r.technical_signal ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-xs mt-3" style={muted}>Scores are 0–100 on fixed breakpoints. Click a row for the full analyst report.</p>
        </div>
      )}

      {selected && <ResearchReport symbol={selected} />}
    </div>
  );
}
