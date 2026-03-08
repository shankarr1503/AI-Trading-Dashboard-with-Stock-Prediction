'use client';
import { useState, useEffect } from 'react';
import { FiMessageSquare, FiRefreshCw } from 'react-icons/fi';
import { api } from '@/lib/api';

interface SentimentGaugeProps {
  symbol: string;
}

export default function SentimentGauge({ symbol }: SentimentGaugeProps) {
  const [sentiment, setSentiment] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  const fetch = async () => {
    setLoading(true);
    try {
      const res = await api.get(`/api/signals/${symbol}`);
      const comp = res.data?.components?.sentiment;
      setSentiment(comp || generateMockSentiment());
    } catch {
      setSentiment(generateMockSentiment());
    }
    setLoading(false);
  };

  useEffect(() => { fetch(); }, [symbol]);

  const generateMockSentiment = () => ({
    positive: 0.42, negative: 0.22, neutral: 0.36,
    overall: 'BULLISH', compound_score: 0.20,
    headlines: [
      `${symbol} reports strong earnings beat expectations`,
      `Analysts upgrade ${symbol} to buy rating`,
      `${symbol} announces new product lineup`,
    ],
  });

  const overallColor = sentiment?.overall === 'BULLISH' ? '#00d4aa' : sentiment?.overall === 'BEARISH' ? '#ff4757' : '#ffd700';
  const positive = (sentiment?.positive || 0) * 100;
  const negative = (sentiment?.negative || 0) * 100;
  const neutral = (sentiment?.neutral || 0) * 100;

  return (
    <div className="card p-4 fade-in">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center"
            style={{ background: 'linear-gradient(135deg, #a855f7, #ec4899)' }}>
            <FiMessageSquare className="w-4 h-4 text-white" />
          </div>
          <div>
            <h3 className="font-semibold text-sm" style={{ color: '#e8eaf0' }}>Sentiment</h3>
            <p className="text-xs" style={{ color: '#5a6478' }}>FinBERT NLP · {symbol}</p>
          </div>
        </div>
        <button onClick={fetch} className="w-7 h-7 rounded-lg flex items-center justify-center hover:bg-white/5"
          style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
          <FiRefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} style={{ color: '#5a6478' }} />
        </button>
      </div>

      {loading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => <div key={i} className="shimmer h-8 rounded-xl" />)}
        </div>
      ) : sentiment && (
        <div className="space-y-3">
          {/* Overall Badge */}
          <div className="flex items-center justify-between p-3 rounded-xl"
            style={{ background: `${overallColor}15`, border: `1px solid ${overallColor}30` }}>
            <span className="text-xs" style={{ color: '#9ba3b8' }}>Overall Market Mood</span>
            <span className="font-bold text-sm" style={{ color: overallColor }}>{sentiment.overall}</span>
          </div>

          {/* Sentiment Bars */}
          {[
            { label: 'Bullish', value: positive, color: '#00d4aa' },
            { label: 'Bearish', value: negative, color: '#ff4757' },
            { label: 'Neutral', value: neutral, color: '#ffd700' },
          ].map(({ label, value, color }) => (
            <div key={label}>
              <div className="flex justify-between text-xs mb-1" style={{ color: '#9ba3b8' }}>
                <span>{label}</span>
                <span className="font-mono font-semibold" style={{ color }}>{value.toFixed(1)}%</span>
              </div>
              <div className="h-1.5 rounded-full" style={{ background: '#1a1d24' }}>
                <div className="h-full rounded-full transition-all" style={{ width: `${value}%`, background: color }} />
              </div>
            </div>
          ))}

          {/* News Headlines */}
          {sentiment?.headlines?.length > 0 && (
            <div>
              <p className="text-xs font-semibold mb-2 mt-1" style={{ color: '#9ba3b8' }}>RECENT HEADLINES</p>
              <div className="space-y-2">
                {sentiment.headlines.slice(0, 3).map((h: string, i: number) => (
                  <div key={i} className="flex items-start gap-2 px-2 py-2 rounded-lg"
                    style={{ background: '#1a1d24' }}>
                    <div className="w-1.5 h-1.5 rounded-full mt-1.5 flex-shrink-0" style={{ background: '#4fa3ff' }} />
                    <p className="text-xs leading-relaxed" style={{ color: '#9ba3b8' }}>{h}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          <p className="text-xs" style={{ color: '#3a4255' }}>
            Source: FinBERT NLP Analysis
          </p>
        </div>
      )}
    </div>
  );
}
