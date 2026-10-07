'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { FiBookOpen, FiCpu, FiRefreshCw } from 'react-icons/fi';
import { errorMessage, researchApi } from '@/lib/api';
import { useAuth } from '@/lib/useAuth';

const box = { background: '#1a1d24', border: '1px solid #1e2535' };
const muted = { color: '#5a6478' };
const text = { color: '#e8eaf0' };

const RATING_COLOR: Record<string, string> = {
  STRONG_BUY: '#00d4aa', BUY: '#4fd1a5', HOLD: '#ffd700', SELL: '#ff8a5c', STRONG_SELL: '#ff4757',
};

const pct = (v: number | null | undefined, d = 1) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(d)}%`);
const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '—' : Number(v).toFixed(d));
const big = (v: number | null | undefined) => {
  if (v === null || v === undefined) return '—';
  const a = Math.abs(v);
  return a >= 1e12 ? `${(v / 1e12).toFixed(2)}T` : a >= 1e9 ? `${(v / 1e9).toFixed(2)}B` : a >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v.toFixed(0);
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card p-4">
      <p className="text-xs font-semibold mb-3" style={{ color: '#9ba3b8' }}>{title}</p>
      {children}
    </div>
  );
}

function ScoreBar({ label, value }: { label: string; value: number | null }) {
  const v = value ?? 0;
  const color = v >= 65 ? '#00d4aa' : v >= 40 ? '#ffd700' : '#ff4757';
  return (
    <div>
      <div className="flex justify-between text-xs mb-1"><span style={{ color: '#9ba3b8' }}>{label}</span>
        <span className="font-mono" style={text}>{value === null ? 'n/a' : value.toFixed(0)}</span></div>
      <div className="h-1.5 rounded-full" style={{ background: '#111318' }}>
        <div className="h-full rounded-full" style={{ width: `${v}%`, background: color }} />
      </div>
    </div>
  );
}

function Rows({ rows }: { rows: [string, string][] }) {
  return (
    <div className="space-y-1 text-xs font-mono">
      {rows.map(([k, v]) => (
        <div key={k} className="flex justify-between"><span style={muted}>{k}</span><span style={text}>{v}</span></div>
      ))}
    </div>
  );
}

export default function ResearchReport({ symbol }: { symbol: string }) {
  const { user } = useAuth();
  const [dossier, setDossier] = useState<any>(null);
  const [report, setReport] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');

  // Page load / refresh only reads: it never starts a paid AI analyst run.
  const load = async () => {
    setLoading(true);
    setError('');
    const [d, r] = await Promise.allSettled([researchApi.fundamentals(symbol), researchApi.report(symbol)]);
    if (d.status === 'fulfilled') setDossier(d.value.data); else setDossier(null);
    if (r.status === 'fulfilled') setReport(r.value.data); else setReport(null);
    if (d.status === 'rejected' && r.status === 'rejected') setError(errorMessage(r.reason, 'Research unavailable for this symbol.'));
    setLoading(false);
  };

  // Explicit, admin-only action: runs the Claude analyst (costs API credits).
  const generate = async () => {
    if (!window.confirm(`Generate a new AI analyst report for ${symbol}? This uses Claude API credits and can take a few minutes.`)) return;
    setGenerating(true);
    setError('');
    try {
      const r = await researchApi.generateReport(symbol);
      setReport(r.data);
    } catch (e) {
      setError(errorMessage(e, 'Report generation failed.'));
    }
    setGenerating(false);
  };

  useEffect(() => {
    if (user) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, user]);

  if (!user) {
    return <div className="card p-4 text-sm" style={{ color: '#9ba3b8' }}>
      <Link href="/login" style={{ color: '#4fa3ff' }}>Sign in</Link> to view equity research.</div>;
  }

  const rep = report?.report;
  const canGenerate = Boolean(user.is_superuser && report?.ai_generation_available);
  const usage = report?.usage;
  const f = dossier?.fundamentals;
  const m = f?.metrics || {};
  const v = dossier?.valuation;
  const card = dossier?.scorecard;
  const profile = dossier?.snapshot?.profile;

  return (
    <div className="space-y-4 fade-in">
      <div className="card p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg flex items-center justify-center" style={{ background: 'linear-gradient(135deg,#4fa3ff,#00d4aa)' }}>
              <FiBookOpen className="w-5 h-5 text-white" />
            </div>
            <div>
              <h2 className="font-bold" style={text}>{profile?.name ?? symbol} <span className="font-mono text-sm" style={{ color: '#4fa3ff' }}>{symbol}</span></h2>
              <p className="text-xs" style={muted}>{[profile?.sector, profile?.industry, profile?.country].filter(Boolean).join(' · ')}</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {rep && (
              <span className="px-3 py-1.5 rounded-lg text-sm font-black" style={{ background: `${RATING_COLOR[rep.rating]}22`, color: RATING_COLOR[rep.rating] }}>
                {rep.rating.replace('_', ' ')} · {rep.conviction}/5
              </span>
            )}
            {canGenerate && (
              <button onClick={generate} disabled={generating || loading} title="Run the AI analyst (uses Claude API credits)"
                className="px-3 py-2 rounded-lg text-xs font-semibold flex items-center gap-1.5"
                style={{ background: '#4fa3ff', color: 'white', opacity: generating || loading ? 0.6 : 1 }}>
                <FiCpu className={generating ? 'animate-pulse' : ''} />
                {generating ? 'Generating…' : 'Generate AI report'}
              </button>
            )}
            <button onClick={() => load()} disabled={loading || generating} title="Reload research data"
              className="p-2 rounded-lg" style={box}>
              <FiRefreshCw className={loading ? 'animate-spin' : ''} style={muted} />
            </button>
          </div>
        </div>
        {report && (
          <p className="text-xs mt-2" style={muted}>
            {report.source === 'claude' ? `AI analyst (${report.model})` : 'Rules-based quant report'} · {new Date(report.created_at).toLocaleString()}
            {usage && usage.api_calls ? ` · ${usage.api_calls} API calls, ${big(usage.input_tokens)} in / ${big(usage.output_tokens)} out tokens` : ''}
            {usage && usage.web_search_requests ? `, ${usage.web_search_requests} web searches` : ''}
          </p>
        )}
        {rep?.llm_failed && (
          <p className="text-xs mt-1" style={{ color: '#ff8a5c' }}>
            The last AI analyst run failed ({rep.llm_error}); showing the rules-based report. It will not be retried automatically for a few hours.
          </p>
        )}
        {error && <p className="text-xs mt-2" style={{ color: '#ff4757' }}>{error}</p>}
        {loading && !rep && <div className="shimmer h-24 rounded-xl mt-3" />}
        {rep && <p className="text-sm mt-3 leading-relaxed" style={{ color: '#c9cfdd' }}>{rep.summary}</p>}
      </div>

      {rep && (
        <div className="grid md:grid-cols-3 gap-4">
          {(['bear_case', 'base_case', 'bull_case'] as const).map((k) => (
            <div key={k} className="card p-4">
              <p className="text-xs font-semibold" style={{ color: k === 'bull_case' ? '#00d4aa' : k === 'bear_case' ? '#ff4757' : '#ffd700' }}>
                {k.replace('_case', '').toUpperCase()} · {(rep[k].probability * 100).toFixed(0)}%
              </p>
              <p className="font-mono font-bold text-xl mt-1" style={text}>{num(rep[k].price_target)}</p>
              <p className="text-xs mt-1" style={{ color: '#9ba3b8' }}>{rep[k].narrative}</p>
            </div>
          ))}
        </div>
      )}
      {rep && (
        <p className="text-xs" style={muted}>
          Probability-weighted target {num(rep.expected_price)} ({rep.expected_return_pct !== null ? `${rep.expected_return_pct > 0 ? '+' : ''}${rep.expected_return_pct}%` : '—'} vs {num(report.price)})
          {rep.consistency_warnings?.length ? ` · ⚠ ${rep.consistency_warnings.join('; ')}` : ''}
        </p>
      )}

      <div className="grid md:grid-cols-2 gap-4">
        {rep && (
          <Section title="INVESTMENT THESIS">
            <ul className="space-y-1.5 text-sm" style={{ color: '#c9cfdd' }}>
              {rep.thesis.map((t: string, i: number) => <li key={i}>• {t}</li>)}
            </ul>
            {rep.what_would_change_our_mind?.length > 0 && (
              <>
                <p className="text-xs font-semibold mt-3 mb-1" style={muted}>WHAT WOULD CHANGE OUR MIND</p>
                <ul className="space-y-1 text-xs" style={{ color: '#9ba3b8' }}>
                  {rep.what_would_change_our_mind.map((t: string, i: number) => <li key={i}>• {t}</li>)}
                </ul>
              </>
            )}
          </Section>
        )}
        {rep && (
          <Section title="RISKS & CATALYSTS">
            <ul className="space-y-1.5 text-xs">
              {rep.risks.map((r: any, i: number) => (
                <li key={i} style={{ color: '#c9cfdd' }}>
                  <span className="font-mono mr-1" style={{ color: r.severity === 'high' ? '#ff4757' : r.severity === 'medium' ? '#ffd700' : '#9ba3b8' }}>[{r.severity}]</span>
                  {r.risk}{r.mitigant ? <span style={muted}> — {r.mitigant}</span> : null}
                </li>
              ))}
              {!rep.risks.length && <li style={muted}>No specific risks flagged.</li>}
            </ul>
            <ul className="space-y-1 text-xs mt-3">
              {rep.catalysts.map((c: any, i: number) => (
                <li key={i} style={{ color: '#c9cfdd' }}><span className="font-mono" style={muted}>{c.date}</span> {c.event} ({c.impact})</li>
              ))}
            </ul>
          </Section>
        )}

        {card && (
          <Section title={`FACTOR SCORECARD · composite ${card.composite ?? 'n/a'}/100 · coverage ${(card.coverage * 100).toFixed(0)}%`}>
            <div className="space-y-2">
              {Object.entries(card.factors).map(([k, fct]: any) => <ScoreBar key={k} label={k.replace('_', ' ')} value={fct.score} />)}
            </div>
            {card.notes?.length > 0 && (
              <ul className="text-xs mt-3 space-y-0.5" style={muted}>{card.notes.map((x: string) => <li key={x}>• {x}</li>)}</ul>
            )}
          </Section>
        )}

        {f?.available && (
          <Section title={`FINANCIALS · FY ${f.latest_period}`}>
            <Rows rows={[
              ['Revenue / CAGR', `${big(m.revenue)} / ${pct(m.revenue_cagr)}`],
              ['Gross / operating / net margin', `${pct(m.gross_margin)} / ${pct(m.operating_margin)} / ${pct(m.net_margin)}`],
              ['FCF / FCF margin', `${big(m.fcf)} / ${pct(m.fcf_margin)}`],
              ['ROIC / ROE', `${pct(m.roic)} / ${pct(m.roe)}`],
              ['Net debt / EBITDA', num(m.net_debt_to_ebitda, 2)],
              ['Interest coverage', m.interest_coverage ? `${num(m.interest_coverage, 1)}x` : '—'],
              ['Cash conversion (FCF/NI)', num(m.cash_conversion, 2)],
              ['Share count change', pct(m.share_count_change)],
              ['Piotroski F-score', f.piotroski?.score !== null ? `${f.piotroski.score}/9` : 'n/a'],
              [f.altman?.model === 'z_double_prime' ? "Altman Z'' (non-mfg)" : f.altman?.model === 'z_prime' ? "Altman Z' (book)" : 'Altman Z',
                f.altman?.z !== null && f.altman?.z !== undefined ? `${f.altman.z} (${f.altman.zone})` : String(f.altman?.zone ?? '—').replace('_', ' ')],
            ]} />
            {f.flags?.length > 0 && (
              <ul className="text-xs mt-3 space-y-0.5" style={{ color: '#ff8a5c' }}>{f.flags.map((x: string) => <li key={x}>⚠ {x}</li>)}</ul>
            )}
          </Section>
        )}

        {v && (
          <Section title={`VALUATION · ${v.method.toUpperCase()}`}>
            <Rows rows={[
              ['P/E · forward P/E', `${num(v.multiples.pe, 1)} · ${num(v.multiples.forward_pe, 1)}`],
              ['EV/EBITDA · EV/Sales', `${num(v.multiples.ev_ebitda, 1)} · ${num(v.multiples.ev_sales, 1)}`],
              ['FCF yield · earnings yield', `${pct(v.multiples.fcf_yield)} · ${pct(v.multiples.earnings_yield)}`],
              ['WACC', pct(v.cost_of_capital.wacc, 2)],
              ['Fair value (prob.-weighted)', `${num(v.fair_value)} (${v.upside_pct !== null ? `${v.upside_pct > 0 ? '+' : ''}${v.upside_pct}%` : '—'})`],
              ['Market-implied FCF growth', pct(v.market_implied_growth)],
              ['Street target (low–high)', `${num(v.street.target_mean)} (${num(v.street.target_low)}–${num(v.street.target_high)})`],
            ]} />
            <ul className="text-xs mt-3 space-y-0.5" style={muted}>
              {[...v.assumptions, ...v.warnings].map((a: string) => <li key={a}>• {a}</li>)}
            </ul>
          </Section>
        )}

        {rep && (
          <Section title="ANALYST NOTES">
            <Rows rows={[['Moat', rep.moat]]} />
            <p className="text-xs mt-2" style={{ color: '#9ba3b8' }}>{rep.moat_rationale}</p>
            {['financial_health', 'valuation_view', 'technical_view'].map((k) => (
              <p key={k} className="text-xs mt-2" style={{ color: '#9ba3b8' }}><span style={muted}>{k.replace('_', ' ')}:</span> {rep[k]}</p>
            ))}
            {rep.data_gaps?.length > 0 && <p className="text-xs mt-2" style={muted}>Data gaps: {rep.data_gaps.join('; ')}</p>}
          </Section>
        )}
      </div>
      <p className="text-xs" style={muted}>Research is for educational purposes only and is not investment advice. Models and AI can be wrong.</p>
    </div>
  );
}
