'use client';
import { useState, useEffect } from 'react';
import { FiTrendingUp, FiTrendingDown, FiStar } from 'react-icons/fi';
import { marketApi, createPriceWebSocket } from '@/lib/api';

const DEFAULT_WATCHLIST = ['AAPL', 'MSFT', 'GOOGL', 'TSLA', 'NVDA', 'RELIANCE.NS', 'TCS.NS', 'INFY.NS'];

interface Stock {
  symbol: string;
  name: string;
  current_price: number;
  change: number;
  change_pct: number;
}

interface WatchlistProps {
  onSymbolSelect: (symbol: string) => void;
  activeSymbol: string;
}

export default function Watchlist({ onSymbolSelect, activeSymbol }: WatchlistProps) {
  const [stocks, setStocks] = useState<Record<string, Stock>>({});
  const [loading, setLoading] = useState(true);

  const fetchQuotes = async () => {
    try {
      const res = await marketApi.getBatch(DEFAULT_WATCHLIST);
      const data = res.data;
      setStocks(prev => ({ ...prev, ...data }));
    } catch (e) {
      console.error('Watchlist fetch failed:', e);
    }
    setLoading(false);
  };

  useEffect(() => {
    fetchQuotes();
    const interval = setInterval(fetchQuotes, 15000); // refresh every 15s
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="card flex flex-col" style={{ maxHeight: '500px' }}>
      {/* Header */}
      <div className="flex items-center justify-between p-4" style={{ borderBottom: '1px solid #1e2535' }}>
        <div className="flex items-center gap-2">
          <FiStar className="w-4 h-4" style={{ color: '#ffd700' }} />
          <span className="font-semibold text-sm" style={{ color: '#e8eaf0' }}>Watchlist</span>
        </div>
        <div className="flex gap-1 text-xs" style={{ color: '#5a6478' }}>
          <span>{DEFAULT_WATCHLIST.length} stocks</span>
        </div>
      </div>

      {/* Table Header */}
      <div className="grid grid-cols-4 px-4 py-2 text-xs" style={{ color: '#5a6478', borderBottom: '1px solid #1e2535' }}>
        <span>Symbol</span>
        <span className="text-right">Price</span>
        <span className="text-right">Change</span>
        <span className="text-right">%</span>
      </div>

      {/* Stock List */}
      <div className="overflow-y-auto flex-1">
        {loading ? (
          Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="px-4 py-3 flex items-center gap-3">
              <div className="shimmer h-4 w-16 rounded" />
              <div className="shimmer h-4 w-20 rounded ml-auto" />
            </div>
          ))
        ) : (
          DEFAULT_WATCHLIST.map((symbol) => {
            const stock = stocks[symbol];
            if (!stock || stock.error) return null;

            const isPositive = (stock.change_pct || 0) >= 0;
            const isActive = symbol === activeSymbol;

            return (
              <button
                key={symbol}
                onClick={() => onSymbolSelect(symbol)}
                className="w-full grid grid-cols-4 px-4 py-3 text-left transition-all text-sm"
                style={{
                  background: isActive ? 'rgba(79, 163, 255, 0.08)' : 'transparent',
                  borderLeft: isActive ? '2px solid #4fa3ff' : '2px solid transparent',
                }}>
                {/* Symbol */}
                <div>
                  <p className="font-mono font-semibold text-xs" style={{ color: isActive ? '#4fa3ff' : '#e8eaf0' }}>{symbol}</p>
                </div>

                {/* Price */}
                <div className="text-right">
                  <span className="font-mono text-xs font-medium" style={{ color: '#e8eaf0' }}>
                    {stock.current_price?.toFixed(2) ?? '--'}
                  </span>
                </div>

                {/* Change */}
                <div className="text-right">
                  <span className={`font-mono text-xs ${isPositive ? 'positive' : 'negative'}`}>
                    {isPositive ? '+' : ''}{stock.change?.toFixed(2) ?? '--'}
                  </span>
                </div>

                {/* % Change */}
                <div className="text-right">
                  <span className={`font-mono text-xs font-semibold ${isPositive ? 'positive' : 'negative'}`}>
                    {isPositive ? '+' : ''}{stock.change_pct?.toFixed(2) ?? '--'}%
                  </span>
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
