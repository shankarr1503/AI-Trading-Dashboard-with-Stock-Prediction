'use client';
import { useState, useCallback } from 'react';
import Link from 'next/link';
import { FiSearch, FiCpu, FiFilter, FiLogOut, FiUser } from 'react-icons/fi';
import { marketApi } from '@/lib/api';
import { useAuth } from '@/lib/useAuth';

interface SearchResult {
  symbol: string;
  name: string;
  exchange: string;
}

interface HeaderProps {
  onSymbolSelect: (symbol: string) => void;
  activeSymbol: string;
}

export default function Header({ onSymbolSelect, activeSymbol }: HeaderProps) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const { user, logout } = useAuth();

  const search = useCallback(async (q: string) => {
    setQuery(q);
    if (q.length < 1) { setResults([]); return; }
    setLoading(true);
    try {
      const res = await marketApi.search(q);
      setResults(res.data.slice(0, 8));
    } catch {}
    setLoading(false);
  }, []);

  return (
    <header className="sticky top-0 z-50 flex items-center justify-between px-6 py-3"
      style={{ background: '#0d1117', borderBottom: '1px solid #1e2535' }}>
      
      {/* Logo */}
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg flex items-center justify-center"
          style={{ background: 'linear-gradient(135deg, #4fa3ff, #00d4aa)' }}>
          <svg viewBox="0 0 24 24" fill="white" className="w-5 h-5">
            <path d="M3 17l6-6 4 4 8-10" stroke="white" strokeWidth="2.5" fill="none" strokeLinecap="round"/>
          </svg>
        </div>
        <span className="font-bold text-base tracking-tight" style={{ color: '#e8eaf0' }}>
          AI<span style={{ color: '#4fa3ff' }}>Trade</span>
        </span>
      </div>

      {/* Search */}
      <div className="relative flex-1 max-w-md mx-8">
        <div className="flex items-center gap-2 rounded-xl px-3 py-2"
          style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
          <FiSearch className="text-gray-500 w-4 h-4 flex-shrink-0" />
          <input
            type="text"
            placeholder="Search stocks: AAPL, RELIANCE.NS, MSFT..."
            className="bg-transparent text-sm outline-none w-full"
            style={{ color: '#e8eaf0', border: 'none', padding: 0 }}
            value={query}
            onChange={(e) => search(e.target.value)}
          />
          {loading && <div className="w-3 h-3 border-2 rounded-full animate-spin" style={{ borderColor: '#4fa3ff', borderTopColor: 'transparent' }} />}
        </div>
        
        {/* Dropdown */}
        {results.length > 0 && (
          <div className="absolute top-full left-0 right-0 mt-1 rounded-xl overflow-hidden z-50 shadow-xl"
            style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
            {results.map((r) => (
              <button key={r.symbol}
                className="w-full flex items-center justify-between px-4 py-3 text-left transition-colors hover:bg-white/5"
                onClick={() => {
                  onSymbolSelect(r.symbol);
                  setQuery(r.symbol);
                  setResults([]);
                }}>
                <div>
                  <span className="font-mono font-semibold text-sm" style={{ color: '#4fa3ff' }}>{r.symbol}</span>
                  <p className="text-xs mt-0.5" style={{ color: '#5a6478' }}>{r.name}</p>
                </div>
                <span className="text-xs px-2 py-0.5 rounded" style={{ background: '#1e2535', color: '#9ba3b8' }}>{r.exchange}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Right Controls */}
      <div className="flex items-center gap-3">
        <span className="font-mono text-sm font-semibold px-3 py-1.5 rounded-lg"
          style={{ background: '#1a1d24', color: '#4fa3ff', border: '1px solid #1e2535' }}>
          {activeSymbol}
        </span>

        <Link href="/research" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-white/5"
          style={{ background: '#1a1d24', border: '1px solid #1e2535', color: '#9ba3b8' }}>
          <FiFilter className="w-4 h-4" /> Research
        </Link>

        <Link href="/bot" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-white/5"
          style={{ background: '#1a1d24', border: '1px solid #1e2535', color: '#9ba3b8' }}>
          <FiCpu className="w-4 h-4" /> Trading Bot
        </Link>

        {user ? (
          <button onClick={logout} title="Sign out"
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs hover:bg-white/5"
            style={{ background: '#1a1d24', border: '1px solid #1e2535', color: '#9ba3b8' }}>
            <FiUser className="w-4 h-4" /> {user.username} <FiLogOut className="w-3.5 h-3.5" />
          </button>
        ) : (
          <Link href="/login" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs hover:bg-white/5"
            style={{ background: '#1a1d24', border: '1px solid #1e2535', color: '#9ba3b8' }}>
            <FiUser className="w-4 h-4" /> Sign in
          </Link>
        )}
      </div>
    </header>
  );
}
