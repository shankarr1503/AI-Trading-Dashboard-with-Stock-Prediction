'use client';
import { FormEvent, useEffect, useState } from 'react';
import Link from 'next/link';
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';
import { FiBriefcase, FiPlus } from 'react-icons/fi';
import { errorMessage, portfolioApi } from '@/lib/api';
import { useAuth } from '@/lib/useAuth';

const COLORS = ['#4fa3ff', '#00d4aa', '#a855f7', '#ffd700', '#ff4757', '#06b6d4'];

const PieTooltip = ({ active, payload }: any) => {
  if (!active || !payload?.length) return null;
  return (
    <div className="px-3 py-2 rounded-lg text-xs shadow-xl" style={{ background: '#1e2229', border: '1px solid #1e2535' }}>
      <p style={{ color: '#e8eaf0' }}>{payload[0].name}: <strong>{payload[0].value?.toFixed(1)}%</strong></p>
    </div>
  );
};

const box = { background: '#1a1d24', border: '1px solid #1e2535' };
const inputCls = 'rounded-lg px-2 py-1.5 text-xs outline-none w-full';
const inputStyle = { background: '#111318', border: '1px solid #1e2535', color: '#e8eaf0' };

export default function PortfolioSummary() {
  const { user, loading: authLoading } = useAuth();
  const [portfolios, setPortfolios] = useState<any[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [portfolio, setPortfolio] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [form, setForm] = useState({ symbol: '', quantity: '', price: '' });

  const loadList = async () => {
    try {
      const r = await portfolioApi.list();
      setPortfolios(r.data);
      if (r.data.length && activeId === null) setActiveId(r.data[0].id);
    } catch (e) {
      setError(errorMessage(e, 'Could not load portfolios'));
    }
  };

  const loadSummary = async (id: number) => {
    setLoading(true);
    try {
      setPortfolio((await portfolioApi.getSummary(id)).data);
      setError('');
    } catch (e) {
      setError(errorMessage(e, 'Could not load portfolio'));
    }
    setLoading(false);
  };

  useEffect(() => {
    if (user) loadList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  useEffect(() => {
    if (activeId !== null) loadSummary(activeId);
  }, [activeId]);

  const createPortfolio = async () => {
    try {
      const r = await portfolioApi.createPortfolio({ name: 'My Portfolio' });
      setActiveId(r.data.id);
      await loadList();
    } catch (e) {
      setError(errorMessage(e, 'Could not create portfolio'));
    }
  };

  const addHolding = async (e: FormEvent) => {
    e.preventDefault();
    if (activeId === null) return;
    try {
      await portfolioApi.addHolding(activeId, {
        symbol: form.symbol.trim().toUpperCase(),
        quantity: Number(form.quantity),
        avg_buy_price: Number(form.price),
      });
      setForm({ symbol: '', quantity: '', price: '' });
      await loadSummary(activeId);
    } catch (err) {
      setError(errorMessage(err, 'Could not add holding'));
    }
  };

  const header = (
    <div className="flex items-center gap-2 mb-4">
      <div className="w-7 h-7 rounded-lg flex items-center justify-center" style={{ background: 'linear-gradient(135deg, #00d4aa, #4fa3ff)' }}>
        <FiBriefcase className="w-4 h-4 text-white" />
      </div>
      <h3 className="font-semibold text-sm" style={{ color: '#e8eaf0' }}>Portfolio</h3>
      {portfolios.length > 1 ? (
        <select className="ml-auto text-xs rounded px-2 py-1" style={inputStyle} value={activeId ?? ''}
          onChange={(e) => setActiveId(Number(e.target.value))}>
          {portfolios.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      ) : (
        <span className="text-xs ml-auto" style={{ color: '#5a6478' }}>{portfolio?.name}</span>
      )}
    </div>
  );

  if (authLoading) return <div className="card p-4"><div className="shimmer h-24 rounded-xl" /></div>;

  if (!user) {
    return (
      <div className="card p-4">
        {header}
        <p className="text-sm" style={{ color: '#9ba3b8' }}>
          <Link href="/login" style={{ color: '#4fa3ff' }}>Sign in</Link> to track your portfolio.
        </p>
      </div>
    );
  }

  if (!portfolios.length) {
    return (
      <div className="card p-4">
        {header}
        {error && <p className="text-xs mb-2" style={{ color: '#ff4757' }}>{error}</p>}
        <button onClick={createPortfolio} className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold"
          style={{ background: '#4fa3ff', color: 'white' }}>
          <FiPlus /> Create a portfolio
        </button>
      </div>
    );
  }

  const pnlPositive = (portfolio?.total_pnl || 0) >= 0;
  const sectorData = Object.entries(portfolio?.sector_allocation || {}).map(([name, value]) => ({ name, value: Number(value) }));

  return (
    <div className="card p-4 fade-in">
      {header}
      {error && <p className="text-xs mb-2" style={{ color: '#ff4757' }}>{error}</p>}
      {loading && !portfolio ? (
        <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="shimmer h-12 rounded-xl" />)}</div>
      ) : portfolio && (
        <>
          <div className="grid grid-cols-2 gap-3 mb-4">
            <div className="p-3 rounded-xl" style={box}>
              <p className="text-xs mb-1" style={{ color: '#5a6478' }}>Total Value</p>
              <p className="font-mono font-bold text-lg" style={{ color: '#e8eaf0' }}>
                ${portfolio.total_value?.toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </p>
            </div>
            <div className="p-3 rounded-xl" style={{ background: pnlPositive ? 'rgba(0,212,170,0.08)' : 'rgba(255,71,87,0.08)', border: `1px solid ${pnlPositive ? 'rgba(0,212,170,0.2)' : 'rgba(255,71,87,0.2)'}` }}>
              <p className="text-xs mb-1" style={{ color: '#5a6478' }}>Total P&L</p>
              <p className={`font-mono font-bold text-lg ${pnlPositive ? 'positive' : 'negative'}`}>
                {pnlPositive ? '+' : ''}${portfolio.total_pnl?.toFixed(2)}
              </p>
              <p className={`font-mono text-xs ${pnlPositive ? 'positive' : 'negative'}`}>
                {pnlPositive ? '+' : ''}{portfolio.total_pnl_pct?.toFixed(2)}%
              </p>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 mb-4">
            {[
              { label: 'Sharpe (1y)', value: portfolio.sharpe_ratio, digits: 2, suffix: '' },
              { label: 'Max drawdown (1y)', value: portfolio.max_drawdown_pct, digits: 1, suffix: '%' },
            ].map(({ label, value, digits, suffix }) => (
              <div key={label} className="flex items-center justify-between px-3 py-2 rounded-lg" style={box}>
                <span className="text-xs" style={{ color: '#5a6478' }}>{label}</span>
                <span className="font-mono font-semibold text-sm" style={{ color: '#e8eaf0' }}>
                  {value === null || value === undefined ? '—' : `${Number(value).toFixed(digits)}${suffix}`}
                </span>
              </div>
            ))}
          </div>

          {sectorData.length > 0 && (
            <div>
              <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>SECTOR ALLOCATION</p>
              <div className="flex items-center gap-3">
                <ResponsiveContainer width={110} height={110}>
                  <PieChart>
                    <Pie data={sectorData} cx="50%" cy="50%" innerRadius={32} outerRadius={52} dataKey="value" paddingAngle={2}>
                      {sectorData.map((_, i) => <Cell key={i} fill={COLORS[i % COLORS.length]} />)}
                    </Pie>
                    <Tooltip content={<PieTooltip />} />
                  </PieChart>
                </ResponsiveContainer>
                <div className="flex-1 space-y-1.5">
                  {sectorData.map((sector, i) => (
                    <div key={sector.name} className="flex items-center justify-between">
                      <div className="flex items-center gap-1.5">
                        <div className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: COLORS[i % COLORS.length] }} />
                        <span className="text-xs truncate" style={{ color: '#9ba3b8', maxWidth: '120px' }}>{sector.name}</span>
                      </div>
                      <span className="font-mono text-xs font-semibold" style={{ color: '#e8eaf0' }}>{sector.value.toFixed(1)}%</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {portfolio.holdings?.length > 0 && (
            <div className="mt-4">
              <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>HOLDINGS</p>
              <div className="space-y-1.5">
                {portfolio.holdings.map((h: any, i: number) => (
                  <div key={`${h.symbol}-${i}`} className="grid grid-cols-4 items-center px-3 py-2 rounded-lg" style={{ background: '#1a1d24' }}>
                    <span className="font-mono text-xs font-semibold" style={{ color: '#4fa3ff' }}>{h.symbol}</span>
                    <span className="font-mono text-xs text-right" style={{ color: '#9ba3b8' }}>{h.quantity}</span>
                    <span className="font-mono text-xs text-right" style={{ color: '#9ba3b8' }}>${h.market_value?.toFixed(0)}</span>
                    <span className={`font-mono text-xs text-right font-semibold ${h.pnl_pct >= 0 ? 'positive' : 'negative'}`}>
                      {h.pnl_pct >= 0 ? '+' : ''}{h.pnl_pct?.toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      <form onSubmit={addHolding} className="grid grid-cols-4 gap-2 mt-4">
        <input className={inputCls} style={inputStyle} placeholder="Symbol" required value={form.symbol}
          onChange={(e) => setForm({ ...form, symbol: e.target.value })} />
        <input className={inputCls} style={inputStyle} placeholder="Qty" type="number" min="0" step="any" required value={form.quantity}
          onChange={(e) => setForm({ ...form, quantity: e.target.value })} />
        <input className={inputCls} style={inputStyle} placeholder="Avg price" type="number" min="0" step="any" required value={form.price}
          onChange={(e) => setForm({ ...form, price: e.target.value })} />
        <button type="submit" className="rounded-lg text-xs font-semibold" style={{ background: '#4fa3ff', color: 'white' }}>Add</button>
      </form>
    </div>
  );
}
