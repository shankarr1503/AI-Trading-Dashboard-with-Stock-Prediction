'use client';
import { useState, useEffect } from 'react';
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid } from 'recharts';
import { FiBriefcase, FiTrendingUp, FiTrendingDown } from 'react-icons/fi';
import { portfolioApi } from '@/lib/api';

const PORTFOLIO_ID = 1; // Demo portfolio ID

const COLORS = ['#4fa3ff', '#00d4aa', '#a855f7', '#ffd700', '#ff4757', '#06b6d4'];

const CustomTooltip = ({ active, payload }: any) => {
  if (!active || !payload?.length) return null;
  return (
    <div className="px-3 py-2 rounded-lg text-xs shadow-xl" style={{ background: '#1e2229', border: '1px solid #1e2535' }}>
      <p style={{ color: '#e8eaf0' }}>{payload[0].name}: <strong>{payload[0].value?.toFixed(1)}%</strong></p>
    </div>
  );
};

export default function PortfolioSummary() {
  const [portfolio, setPortfolio] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetch = async () => {
      try {
        const res = await portfolioApi.getSummary(PORTFOLIO_ID);
        setPortfolio(res.data);
      } catch (e) {
        // Use demo data if API not available
        setPortfolio({
          name: 'Tech Growth Portfolio',
          total_value: 142580.50,
          total_invested: 100000,
          total_pnl: 42580.50,
          total_pnl_pct: 42.58,
          sharpe_ratio: 1.24,
          sector_allocation: {
            Technology: 65.3,
            Financials: 18.2,
            'Communication Services': 10.5,
            Energy: 6.0,
          },
          holdings: [
            { symbol: 'AAPL',  market_value: 9800, pnl_pct: 12.5 },
            { symbol: 'MSFT',  market_value: 13200, pnl_pct: 16.1 },
            { symbol: 'GOOGL', market_value: 3200, pnl_pct: 14.3 },
            { symbol: 'NVDA',  market_value: 14500, pnl_pct: 28.9 },
            { symbol: 'JPM',   market_value: 8200, pnl_pct: 11.4 },
          ],
        });
      }
      setLoading(false);
    };
    fetch();
  }, []);

  if (loading) {
    return (
      <div className="card p-4">
        <div className="shimmer h-6 w-40 rounded mb-4" />
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => <div key={i} className="shimmer h-12 rounded-xl" />)}
        </div>
      </div>
    );
  }

  const pnlPositive = (portfolio?.total_pnl || 0) >= 0;
  const sectorData = Object.entries(portfolio?.sector_allocation || {}).map(([name, value]) => ({
    name, value: Number(value),
  }));

  return (
    <div className="card p-4 fade-in">
      <div className="flex items-center gap-2 mb-4">
        <div className="w-7 h-7 rounded-lg flex items-center justify-center"
          style={{ background: 'linear-gradient(135deg, #00d4aa, #4fa3ff)' }}>
          <FiBriefcase className="w-4 h-4 text-white" />
        </div>
        <h3 className="font-semibold text-sm" style={{ color: '#e8eaf0' }}>Portfolio</h3>
        <span className="text-xs ml-auto" style={{ color: '#5a6478' }}>{portfolio?.name}</span>
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-2 gap-3 mb-4">
        <div className="p-3 rounded-xl" style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
          <p className="text-xs mb-1" style={{ color: '#5a6478' }}>Total Value</p>
          <p className="font-mono font-bold text-lg" style={{ color: '#e8eaf0' }}>
            ${portfolio?.total_value?.toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </p>
        </div>
        <div className="p-3 rounded-xl" style={{ background: pnlPositive ? 'rgba(0,212,170,0.08)' : 'rgba(255,71,87,0.08)', border: `1px solid ${pnlPositive ? 'rgba(0,212,170,0.2)' : 'rgba(255,71,87,0.2)'}` }}>
          <p className="text-xs mb-1" style={{ color: '#5a6478' }}>Total P&L</p>
          <p className={`font-mono font-bold text-lg ${pnlPositive ? 'positive' : 'negative'}`}>
            {pnlPositive ? '+' : ''}${portfolio?.total_pnl?.toFixed(2)}
          </p>
          <p className={`font-mono text-xs ${pnlPositive ? 'positive' : 'negative'}`}>
            {pnlPositive ? '+' : ''}{portfolio?.total_pnl_pct?.toFixed(2)}%
          </p>
        </div>
      </div>

      {/* Sharpe Ratio */}
      <div className="flex items-center justify-between px-3 py-2 rounded-lg mb-4"
        style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
        <span className="text-xs" style={{ color: '#5a6478' }}>Sharpe Ratio</span>
        <span className="font-mono font-semibold text-sm" style={{ color: portfolio?.sharpe_ratio >= 1 ? '#00d4aa' : '#ffd700' }}>
          {portfolio?.sharpe_ratio?.toFixed(2)}
        </span>
      </div>

      {/* Sector Allocation Pie */}
      {sectorData.length > 0 && (
        <div>
          <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>SECTOR ALLOCATION</p>
          <div className="flex items-center gap-3">
            <ResponsiveContainer width={110} height={110}>
              <PieChart>
                <Pie data={sectorData} cx="50%" cy="50%" innerRadius={32} outerRadius={52}
                  dataKey="value" paddingAngle={2}>
                  {sectorData.map((_, i) => (
                    <Cell key={i} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip content={<CustomTooltip />} />
              </PieChart>
            </ResponsiveContainer>
            <div className="flex-1 space-y-1.5">
              {sectorData.map((sector, i) => (
                <div key={sector.name} className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5">
                    <div className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: COLORS[i % COLORS.length] }} />
                    <span className="text-xs truncate" style={{ color: '#9ba3b8', maxWidth: '90px' }}>{sector.name}</span>
                  </div>
                  <span className="font-mono text-xs font-semibold" style={{ color: '#e8eaf0' }}>{sector.value.toFixed(1)}%</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Top Holdings */}
      {portfolio?.holdings?.length > 0 && (
        <div className="mt-4">
          <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>TOP HOLDINGS</p>
          <div className="space-y-1.5">
            {(portfolio?.holdings || []).slice(0, 5).map((h: any) => (
              <div key={h.symbol} className="flex items-center justify-between px-3 py-2 rounded-lg"
                style={{ background: '#1a1d24' }}>
                <span className="font-mono text-xs font-semibold" style={{ color: '#4fa3ff' }}>{h.symbol}</span>
                <span className="font-mono text-xs" style={{ color: '#9ba3b8' }}>
                  ${h.market_value?.toFixed(0)}
                </span>
                <span className={`font-mono text-xs font-semibold ${h.pnl_pct >= 0 ? 'positive' : 'negative'}`}>
                  {h.pnl_pct >= 0 ? '+' : ''}{h.pnl_pct?.toFixed(1)}%
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
