'use client';
import { useEffect, useState } from 'react';
import { marketApi } from '@/lib/api';

const SYMBOLS = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'RELIANCE.NS', 'TCS.NS'];

/** Scrolling ticker of live quotes (previously hard-coded fake numbers). */
export default function TickerBar() {
  const [quotes, setQuotes] = useState<{ symbol: string; change_pct: number }[]>([]);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await marketApi.getBatch(SYMBOLS);
        setQuotes(
          Object.entries(res.data as Record<string, any>)
            .filter(([, q]) => q && !q.error)
            .map(([symbol, q]) => ({ symbol, change_pct: Number(q.change_pct) || 0 })),
        );
      } catch {
        /* keep the last good values */
      }
    };
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  if (!quotes.length) return <div className="py-2 px-4 text-xs" style={{ background: '#0d1117', color: '#3a4255' }}>Loading quotes…</div>;

  const items = [...quotes, ...quotes];
  return (
    <div className="ticker-bar py-2 px-4 text-xs font-mono" style={{ background: '#0d1117' }}>
      <div className="ticker-content">
        {items.map((q, i) => (
          <span key={i} style={{ color: q.change_pct >= 0 ? '#00d4aa' : '#ff4757', marginRight: 32 }}>
            {q.symbol} {q.change_pct >= 0 ? '+' : ''}{q.change_pct.toFixed(2)}%
          </span>
        ))}
      </div>
    </div>
  );
}
