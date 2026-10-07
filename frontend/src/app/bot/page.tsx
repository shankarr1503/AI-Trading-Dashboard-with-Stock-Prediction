'use client';
import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { FiActivity, FiAlertTriangle, FiPause, FiPlay, FiRefreshCw, FiShield, FiZap } from 'react-icons/fi';
import { botApi, errorMessage } from '@/lib/api';
import { useAuth } from '@/lib/useAuth';

const card = 'card p-4';
const muted = { color: '#5a6478' };
const text = { color: '#e8eaf0' };
const box = { background: '#1a1d24', border: '1px solid #1e2535' };

const money = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : `$${Number(v).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '—' : Number(v).toFixed(d));
const when = (v: string | null | undefined) => (v ? new Date(v).toLocaleString() : '—');

function Stat({ label, value, color = '#e8eaf0' }: { label: string; value: string; color?: string }) {
  return (
    <div className="p-3 rounded-xl" style={box}>
      <p className="text-xs mb-1" style={muted}>{label}</p>
      <p className="font-mono font-bold text-base" style={{ color }}>{value}</p>
    </div>
  );
}

function Btn({ onClick, children, color = '#4fa3ff', disabled = false }: any) {
  return (
    <button onClick={onClick} disabled={disabled}
      className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold disabled:opacity-40"
      style={{ background: `${color}22`, color, border: `1px solid ${color}55` }}>
      {children}
    </button>
  );
}

export default function BotPage() {
  const { user, loading: authLoading } = useAuth();
  const [status, setStatus] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [trades, setTrades] = useState<any[]>([]);
  const [decisions, setDecisions] = useState<any[]>([]);
  const [equity, setEquity] = useState<any[]>([]);
  const [perf, setPerf] = useState<any>(null);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [bt, setBt] = useState<any>(null);
  const [btSymbols, setBtSymbols] = useState('AAPL,MSFT,NVDA,JPM,WMT');
  const [btWalk, setBtWalk] = useState(true);

  const [forbidden, setForbidden] = useState(false);

  const load = useCallback(async () => {
    const [s, p, t, d, e, pf] = await Promise.allSettled([
      botApi.status(), botApi.positions(), botApi.trades(), botApi.decisions(60), botApi.equity(), botApi.performance(),
    ]);
    if (s.status === 'rejected' && (s.reason as any)?.response?.status === 403) {
      setForbidden(true);
      return;
    }
    if (s.status === 'fulfilled') setStatus(s.value.data);
    if (p.status === 'fulfilled') setPositions(p.value.data);
    if (t.status === 'fulfilled') setTrades(t.value.data);
    if (d.status === 'fulfilled') setDecisions(d.value.data);
    if (e.status === 'fulfilled') setEquity(e.value.data);
    if (pf.status === 'fulfilled') setPerf(pf.value.data);
  }, []);

  useEffect(() => {
    if (!user) return;
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [user, load]);

  const act = async (fn: () => Promise<any>, ok: string, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    setMsg('');
    try {
      const r = await fn();
      const st = r?.data?.status;
      setMsg(st === 'queued' ? `${ok}: queued — a cycle is running and will flatten when it finishes` : st ? `${ok} (${st})` : ok);
      await load();
    } catch (e) {
      setMsg(errorMessage(e, 'Action failed'));
    }
    setBusy(false);
  };

  const runBacktest = async () => {
    setBusy(true);
    setMsg('Running backtest… this can take a minute.');
    try {
      const symbols = btSymbols.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
      const r = await botApi.backtest({ symbols, period: '5y', walk_forward: btWalk, folds: 4 });
      setBt(r.data);
      setMsg('');
    } catch (e) {
      setMsg(errorMessage(e, 'Backtest failed'));
    }
    setBusy(false);
  };

  if (authLoading) return <div className="p-8" style={muted}>Loading…</div>;
  if (!user) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: '#0a0b0d' }}>
        <p style={{ color: '#9ba3b8' }}><Link href="/login" style={{ color: '#4fa3ff' }}>Sign in</Link> to view the trading bot.</p>
      </div>
    );
  }

  if (forbidden || !user.is_superuser) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: '#0a0b0d' }}>
        <p style={{ color: '#9ba3b8' }}>The trading bot console is restricted to the administrator. <Link href="/dashboard" style={{ color: '#4fa3ff' }}>Back to dashboard</Link></p>
      </div>
    );
  }

  const admin = user.is_superuser;
  const metrics = bt?.out_of_sample ?? bt?.metrics;
  const ddColor = (status?.drawdown_pct ?? 0) > 5 ? '#ff4757' : '#e8eaf0';

  return (
    <div className="min-h-screen p-4 space-y-4" style={{ background: '#0a0b0d' }}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link href="/dashboard" className="text-xs" style={{ color: '#4fa3ff' }}>← Dashboard</Link>
          <h1 className="text-lg font-bold flex items-center gap-2" style={text}><FiZap style={{ color: '#4fa3ff' }} /> Trading Bot</h1>
          {status && (
            <span className="text-xs px-2 py-1 rounded font-mono" style={{
              background: status.live_trading ? '#ff475722' : '#00d4aa22',
              color: status.live_trading ? '#ff4757' : '#00d4aa',
            }}>
              {status.live_trading ? 'LIVE MONEY' : status.mode.toUpperCase().replace('_', ' ')}
            </span>
          )}
        </div>
        <button onClick={load} className="p-2 rounded-lg" style={box} aria-label="Refresh"><FiRefreshCw style={muted} /></button>
      </div>

      {status?.halted && (
        <div className="flex items-center gap-2 p-3 rounded-xl" style={{ background: '#ff475715', border: '1px solid #ff475755', color: '#ff4757' }}>
          <FiAlertTriangle /> <span className="text-sm">Halted: {status.halt_reason}{status.flatten_requested ? ' — flatten pending (positions close at the next open market)' : ''}</span>
        </div>
      )}
      {status && (status.stale || status.consecutive_failures > 0 || status.consecutive_data_faults > 0) && (
        <div className="flex items-start gap-2 p-3 rounded-xl" style={{ background: '#ffd70012', border: '1px solid #ffd70055', color: '#ffd700' }}>
          <FiAlertTriangle className="mt-0.5" />
          <div className="text-sm space-y-0.5">
            {status.stale && <p>No recent cycle{status.last_cycle_at ? ` since ${new Date(status.last_cycle_at).toLocaleString()}` : ''} — is the bot runner process running?</p>}
            {status.consecutive_failures > 0 && <p>{status.consecutive_failures} consecutive failed cycle(s): {status.last_error}</p>}
            {status.consecutive_data_faults > 0 && <p>Market data missing for held positions ({status.consecutive_data_faults} cycle(s)): entries paused, breakers frozen.</p>}
          </div>
        </div>
      )}

      {/* Status + controls */}
      <div className={card}>
        <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-4">
          <Stat label="State" value={status?.halted ? 'HALTED' : status?.stale ? 'STALE' : status?.enabled ? 'RUNNING' : 'PAUSED'}
            color={status?.halted ? '#ff4757' : status?.stale ? '#ffd700' : status?.enabled ? '#00d4aa' : '#ffd700'} />
          <Stat label="Equity" value={money(status?.equity)} />
          <Stat label="Cash" value={money(status?.cash)} />
          <Stat label="Drawdown" value={`${num(status?.drawdown_pct)}%`} color={ddColor} />
          <Stat label="Open positions" value={String(status?.open_positions ?? '—')} />
          <Stat label="Last cycle" value={status?.last_cycle_at ? new Date(status.last_cycle_at).toLocaleString() : '—'}
            color={status?.stale ? '#ffd700' : '#e8eaf0'} />
        </div>
        {admin ? (
          <div className="flex flex-wrap gap-2">
            <Btn onClick={() => act(botApi.start, 'Bot started')} disabled={busy || status?.enabled || status?.halted} color="#00d4aa"><FiPlay /> Start</Btn>
            <Btn onClick={() => act(botApi.stop, 'Bot paused (stops still protected)')} disabled={busy || !status?.enabled} color="#ffd700"><FiPause /> Pause</Btn>
            <Btn onClick={() => act(botApi.runOnce, 'Cycle complete')} disabled={busy}><FiActivity /> Run one cycle</Btn>
            <Btn onClick={() => act(botApi.calibrate, 'Calibration stored')} disabled={busy}><FiShield /> Calibrate edge</Btn>
            {status?.halted && <Btn onClick={() => act(botApi.resetHalt, 'Halt cleared')} disabled={busy} color="#ffd700">Reset halt</Btn>}
            <Btn onClick={() => act(botApi.flatten, 'All positions closed', 'Close ALL bot positions at market and pause the bot?')}
              disabled={busy} color="#ff4757"><FiAlertTriangle /> Flatten all</Btn>
          </div>
        ) : (
          <p className="text-xs" style={muted}>Read-only view. Only the administrator can control the bot.</p>
        )}
        {msg && <p className="text-xs mt-3" style={{ color: '#9ba3b8' }}>{msg}</p>}
      </div>

      <div className="grid md:grid-cols-3 gap-4">
        {/* Equity curve */}
        <div className={`${card} md:col-span-2`}>
          <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>EQUITY</p>
          {equity.length > 1 ? (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={equity.map((e) => ({ t: new Date(e.timestamp).toLocaleDateString(), equity: e.equity }))}>
                <CartesianGrid stroke="#1e2535" strokeDasharray="3 3" />
                <XAxis dataKey="t" tick={{ fill: '#5a6478', fontSize: 10 }} minTickGap={40} />
                <YAxis tick={{ fill: '#5a6478', fontSize: 10 }} domain={['auto', 'auto']} width={70} />
                <Tooltip contentStyle={{ background: '#1e2229', border: '1px solid #1e2535' }} />
                <Line type="monotone" dataKey="equity" stroke="#4fa3ff" dot={false} strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          ) : <p className="text-xs" style={muted}>No equity history yet — run a cycle.</p>}
        </div>

        {/* Risk + performance */}
        <div className={card}>
          <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>RISK LIMITS</p>
          {status?.risk_config && (
            <div className="space-y-1 text-xs font-mono">
              {[
                ['Risk / trade', `${(status.risk_config.max_risk_per_trade_pct * 100).toFixed(2)}%`],
                ['Max position', `${(status.risk_config.max_position_pct * 100).toFixed(0)}%`],
                ['Daily loss stop', `${(status.risk_config.daily_loss_limit_pct * 100).toFixed(1)}%`],
                ['Kill switch DD', `${(status.risk_config.max_drawdown_pct * 100).toFixed(0)}%`],
                ['Edge ≥ costs ×', String(status.risk_config.cost_safety_multiple)],
                ['Max positions', String(status.risk_config.max_open_positions)],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between"><span style={muted}>{k}</span><span style={text}>{v}</span></div>
              ))}
            </div>
          )}
          <p className="text-xs font-semibold mt-4 mb-2" style={{ color: '#9ba3b8' }}>EDGE CALIBRATION</p>
          {status?.calibration ? (
            <div className="space-y-1 text-xs font-mono">
              {[
                ['Out-of-sample trades', String(status.calibration.trades)],
                ['OOS expectancy', `${num(status.calibration.expectancy_r, 3)} R`],
                ['OOS Sharpe / max DD', `${num(status.calibration.sharpe)} / ${num(status.calibration.max_drawdown_pct)}%`],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between"><span style={muted}>{k}</span><span style={text}>{v}</span></div>
              ))}
            </div>
          ) : <p className="text-xs" style={{ color: '#ffd700' }}>Not calibrated: trades are half size, and broker trading is blocked. Run “Calibrate edge”.</p>}
          <p className="text-xs font-semibold mt-4 mb-2" style={{ color: '#9ba3b8' }}>REALISED PERFORMANCE</p>
          {perf?.trades ? (
            <div className="space-y-1 text-xs font-mono">
              {[
                ['Trades', perf.trades], ['Net P&L', money(perf.net_pnl)], ['Costs paid', money(perf.total_costs)],
                ['Win rate', `${perf.win_rate_pct}%`], ['Expectancy', `${num(perf.expectancy_r)} R`],
              ].map(([k, v]) => (
                <div key={k as string} className="flex justify-between"><span style={muted}>{k}</span><span style={text}>{v}</span></div>
              ))}
            </div>
          ) : <p className="text-xs" style={muted}>No closed trades yet.</p>}
        </div>
      </div>

      {/* Positions */}
      <div className={card}>
        <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>OPEN POSITIONS</p>
        {positions.length ? (
          <table className="w-full text-xs font-mono">
            <thead><tr style={muted}>{['Symbol', 'Qty', 'Entry', 'Price', 'Stop', 'Target', 'P&L', 'Opened'].map((h) => <th key={h} className="text-left py-1">{h}</th>)}</tr></thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.symbol} style={text}>
                  <td style={{ color: '#4fa3ff' }}>{p.symbol}</td><td>{p.qty}</td><td>{num(p.entry_price)}</td><td>{num(p.current_price)}</td>
                  <td style={{ color: '#ff4757' }}>{num(p.stop_price)}</td><td style={{ color: '#00d4aa' }}>{num(p.target_price)}</td>
                  <td className={p.unrealized_pnl >= 0 ? 'positive' : 'negative'}>{money(p.unrealized_pnl)}</td><td>{when(p.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="text-xs" style={muted}>No open positions.</p>}
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        {/* Decisions */}
        <div className={card}>
          <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>DECISION JOURNAL (incl. trades not taken)</p>
          <div className="space-y-1.5 max-h-96 overflow-y-auto">
            {decisions.map((d) => (
              <details key={d.id} className="rounded-lg px-2 py-1.5" style={{ background: '#1a1d24' }}>
                <summary className="text-xs cursor-pointer flex gap-2">
                  <span className="font-mono w-14" style={{ color: d.action === 'BUY' ? '#00d4aa' : d.action === 'SELL' ? '#ff4757' : '#9ba3b8' }}>{d.action}</span>
                  <span className="font-mono w-16" style={{ color: '#4fa3ff' }}>{d.symbol ?? '—'}</span>
                  <span style={muted}>{d.score !== null ? `score ${num(d.score)}` : ''} {d.regime ?? ''}</span>
                  <span className="ml-auto" style={muted}>{new Date(d.created_at).toLocaleTimeString()}</span>
                </summary>
                <ul className="text-xs mt-1 space-y-0.5" style={{ color: '#9ba3b8' }}>
                  {(d.reasons || []).map((r: string, i: number) => <li key={i}>• {r}</li>)}
                  {d.llm_verdict && <li style={{ color: '#a855f7' }}>• Claude review: {d.llm_verdict.decision} — {d.llm_verdict.rationale}</li>}
                </ul>
              </details>
            ))}
            {!decisions.length && <p className="text-xs" style={muted}>No decisions yet.</p>}
          </div>
        </div>

        {/* Trades */}
        <div className={card}>
          <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>CLOSED TRADES</p>
          {trades.length ? (
            <table className="w-full text-xs font-mono">
              <thead><tr style={muted}>{['Symbol', 'Entry', 'Exit', 'P&L', 'R', 'Costs', 'Why'].map((h) => <th key={h} className="text-left py-1">{h}</th>)}</tr></thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} style={text}>
                    <td style={{ color: '#4fa3ff' }}>{t.symbol}</td><td>{num(t.entry_price)}</td><td>{num(t.exit_price)}</td>
                    <td className={t.pnl >= 0 ? 'positive' : 'negative'}>{money(t.pnl)}</td><td>{num(t.r_multiple)}</td>
                    <td>{money(t.costs)}</td><td style={muted}>{t.exit_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="text-xs" style={muted}>No closed trades yet.</p>}
        </div>
      </div>

      {/* Backtest */}
      <div className={card}>
        <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>BACKTEST (same strategy, risk and cost model as the live bot)</p>
        <div className="flex flex-wrap gap-2 items-center mb-3">
          <input className="rounded-lg px-2 py-1.5 text-xs font-mono flex-1 min-w-[240px]" style={{ ...box, color: '#e8eaf0' }}
            value={btSymbols} onChange={(e) => setBtSymbols(e.target.value)} />
          <label className="text-xs flex items-center gap-1" style={{ color: '#9ba3b8' }}>
            <input type="checkbox" checked={btWalk} onChange={(e) => setBtWalk(e.target.checked)} /> Walk-forward (out-of-sample)
          </label>
          <Btn onClick={runBacktest} disabled={busy}><FiActivity /> Run 5-year backtest</Btn>
        </div>
        {metrics && (
          <>
            <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-3">
              <Stat label="Total return" value={`${num(metrics.total_return_pct)}%`} />
              <Stat label="CAGR" value={`${num(metrics.cagr_pct)}%`} />
              <Stat label="Sharpe" value={num(metrics.sharpe)} />
              <Stat label="Max drawdown" value={`${num(metrics.max_drawdown_pct)}%`} color="#ff4757" />
              <Stat label="Trades / win rate" value={`${metrics.trades} / ${num(metrics.win_rate_pct, 0)}%`} />
              <Stat label="Costs paid" value={money(metrics.total_costs)} />
            </div>
            {bt.benchmark && (
              <p className="text-xs mb-2" style={muted}>
                Buy & hold (equal weight): return {num(bt.benchmark.total_return_pct)}%, Sharpe {num(bt.benchmark.sharpe)}, max drawdown {num(bt.benchmark.max_drawdown_pct)}%
              </p>
            )}
            {bt.equity_curve?.length > 1 && (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={bt.equity_curve}>
                  <CartesianGrid stroke="#1e2535" strokeDasharray="3 3" />
                  <XAxis dataKey="date" tick={{ fill: '#5a6478', fontSize: 10 }} minTickGap={50} />
                  <YAxis tick={{ fill: '#5a6478', fontSize: 10 }} domain={['auto', 'auto']} width={70} />
                  <Tooltip contentStyle={{ background: '#1e2229', border: '1px solid #1e2535' }} />
                  <Line type="monotone" dataKey="equity" stroke="#00d4aa" dot={false} strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            )}
            <p className="text-xs mt-2" style={muted}>{bt.disclaimer}</p>
          </>
        )}
      </div>
    </div>
  );
}
