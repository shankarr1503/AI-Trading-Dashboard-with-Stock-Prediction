'use client';
import { useState, useEffect } from 'react';
import { FiTrendingUp, FiTrendingDown, FiActivity } from 'react-icons/fi';
import { marketApi } from '@/lib/api';

interface MarketMoversProps {
  onSymbolSelect: (symbol: string) => void;
}

export default function MarketMovers({ onSymbolSelect }: MarketMoversProps) {
  const [movers, setMovers] = useState<any>(null);
  const [tab, setTab] = useState<'gainers' | 'losers' | 'most_active'>('gainers');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetch = async () => {
      try {
        const res = await marketApi.getMovers();
        setMovers(res.data);
      } catch {
        // Demo movers if API unavailable
        setMovers({
          gainers: [
            { symbol: 'NVDA', change_pct: 4.52, current_price: 875.35, volume: 45000000 },
            { symbol: 'TSLA', change_pct: 3.21, current_price: 252.10, volume: 98000000 },
            { symbol: 'META', change_pct: 2.87, current_price: 490.50, volume: 22000000 },
          ],
          losers: [
            { symbol: 'PFE',  change_pct: -2.31, current_price: 28.40, volume: 31000000 },
            { symbol: 'WMT',  change_pct: -1.55, current_price: 59.20, volume: 12000000 },
          ],
          most_active: [
            { symbol: 'AAPL', change_pct: 0.85, current_price: 189.30, volume: 120000000 },
            { symbol: 'AMZN', change_pct: 1.23, current_price: 185.10, volume: 88000000 },
          ],
        });
      }
      setLoading(false);
    };
    fetch();
    const interval = setInterval(fetch, 60000);
    return () => clearInterval(interval);
  }, []);

  const tabs = [
    { key: 'gainers', label: 'Gainers', icon: FiTrendingUp, color: '#00d4aa' },
    { key: 'losers', label: 'Losers', icon: FiTrendingDown, color: '#ff4757' },
    { key: 'most_active', label: 'Active', icon: FiActivity, color: '#4fa3ff' },
  ];

  const activeStocks = movers?.[tab] || [];

  return (
    <div className="card p-4 fade-in">
      <h3 className="font-semibold text-sm mb-3" style={{ color: '#e8eaf0' }}>Market Movers</h3>
      
      {/* Tabs */}
      <div className="flex gap-1 mb-3">
        {tabs.map(({ key, label, icon: Icon, color }) => (
          <button
            key={key}
            onClick={() => setTab(key as any)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all"
            style={{
              background: tab === key ? `${color}20` : '#1a1d24',
              color: tab === key ? color : '#5a6478',
              border: `1px solid ${tab === key ? `${color}40` : '#1e2535'}`,
            }}>
            <Icon className="w-3 h-3" />
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => <div key={i} className="shimmer h-10 rounded-lg" />)}
        </div>
      ) : (
        <div className="space-y-1.5">
          {activeStocks.slice(0, 5).map((stock: any) => {
            const isPos = stock.change_pct >= 0;
            return (
              <button
                key={stock.symbol}
                onClick={() => onSymbolSelect(stock.symbol)}
                className="w-full flex items-center justify-between px-3 py-2.5 rounded-lg transition-all hover:bg-white/5"
                style={{ background: '#1a1d24' }}>
                <span className="font-mono font-semibold text-xs" style={{ color: '#4fa3ff' }}>
                  {stock.symbol}
                </span>
                <span className="font-mono text-xs" style={{ color: '#9ba3b8' }}>
                  {stock.current_price?.toFixed(2)}
                </span>
                <span className={`font-mono text-xs font-bold px-2 py-0.5 rounded ${isPos ? 'positive' : 'negative'}`}
                  style={{ background: isPos ? 'rgba(0,212,170,0.1)' : 'rgba(255,71,87,0.1)' }}>
                  {isPos ? '+' : ''}{stock.change_pct?.toFixed(2)}%
                </span>
                <span className="text-xs" style={{ color: '#3a4255' }}>
                  {(stock.volume / 1e6).toFixed(0)}M
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
