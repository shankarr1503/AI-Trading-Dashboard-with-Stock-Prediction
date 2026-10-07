'use client';
import { useEffect, useState } from 'react';
import { FiMessageSquare, FiRefreshCw } from 'react-icons/fi';
import { errorMessage, predictionsApi } from '@/lib/api';

interface SentimentGaugeProps {
  symbol: string;
}

const SOURCE_LABEL: Record<string, string> = {
  finbert: 'FinBERT NLP',
  lexicon: 'Financial keyword lexicon',
  keyword_fallback: 'Financial keyword lexicon',
  unavailable: 'No headlines available',
};

export default function SentimentGauge({ symbol }: SentimentGaugeProps) {
  const [sentiment, setSentiment] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const res = await predictionsApi.sentiment(symbol);
      setSentiment(res.data);
    } catch (e) {
      setSentiment(null);
      setError(errorMessage(e, 'Sentiment is unavailable right now.'));
    }
    setLoading(false);
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol]);

  const overallColor = sentiment?.overall === 'BULLISH' ? '#00d4aa' : sentiment?.overall === 'BEARISH' ? '#ff4757' : '#ffd700';
  const analyzed = sentiment?.headlines_analyzed ?? 0;

  return (
    <div className="card p-4 fade-in">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center"
            style={{ background: 'linear-gradient(135deg, #a855f7, #ec4899)' }}>
            <FiMessageSquare className="w-4 h-4 text-white" />
          </div>
          <div>
            <h3 className="font-semibold text-sm" style={{ color: '#e8eaf0' }}>News Sentiment</h3>
            <p className="text-xs" style={{ color: '#5a6478' }}>{symbol} · {analyzed} headlines</p>
          </div>
        </div>
        <button onClick={load} className="w-7 h-7 rounded-lg flex items-center justify-center hover:bg-white/5"
          style={{ background: '#1a1d24', border: '1px solid #1e2535' }} aria-label="Refresh">
          <FiRefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} style={{ color: '#5a6478' }} />
        </button>
      </div>

      {loading ? (
        <div className="space-y-3">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="shimmer h-8 rounded-xl" />)}</div>
      ) : error ? (
        <p className="text-xs" style={{ color: '#ff4757' }}>{error}</p>
      ) : sentiment && analyzed === 0 ? (
        <p className="text-xs" style={{ color: '#9ba3b8' }}>
          No recent headlines were found for {symbol}, so no sentiment is reported (rather than guessing).
        </p>
      ) : sentiment && (
        <div className="space-y-3">
          <div className="flex items-center justify-between p-3 rounded-xl"
            style={{ background: `${overallColor}15`, border: `1px solid ${overallColor}30` }}>
            <span className="text-xs" style={{ color: '#9ba3b8' }}>Overall tone</span>
            <span className="font-bold text-sm" style={{ color: overallColor }}>{sentiment.overall}</span>
          </div>

          {[
            { label: 'Positive', value: (sentiment.positive || 0) * 100, color: '#00d4aa' },
            { label: 'Negative', value: (sentiment.negative || 0) * 100, color: '#ff4757' },
            { label: 'Neutral', value: (sentiment.neutral || 0) * 100, color: '#ffd700' },
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

          {sentiment.headlines?.length > 0 && (
            <div>
              <p className="text-xs font-semibold mb-2 mt-1" style={{ color: '#9ba3b8' }}>RECENT HEADLINES</p>
              <div className="space-y-2">
                {sentiment.headlines.slice(0, 5).map((h: string, i: number) => (
                  <div key={i} className="flex items-start gap-2 px-2 py-2 rounded-lg" style={{ background: '#1a1d24' }}>
                    <div className="w-1.5 h-1.5 rounded-full mt-1.5 flex-shrink-0" style={{ background: '#4fa3ff' }} />
                    <p className="text-xs leading-relaxed" style={{ color: '#9ba3b8' }}>{h}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          <p className="text-xs" style={{ color: '#3a4255' }}>Method: {SOURCE_LABEL[sentiment.source] ?? sentiment.source}</p>
        </div>
      )}
    </div>
  );
}
