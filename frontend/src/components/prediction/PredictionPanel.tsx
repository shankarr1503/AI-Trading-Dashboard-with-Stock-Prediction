'use client';
import { useEffect, useState } from 'react';
import { FiAlertCircle, FiCpu, FiRefreshCw } from 'react-icons/fi';
import { errorMessage, predictionsApi, signalsApi } from '@/lib/api';

interface PredictionPanelProps {
  symbol: string;
}

function Bar({ value, color }: { value: number; color: string }) {
  return (
    <div className="confidence-bar mt-1.5">
      <div className="confidence-fill" style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%`, background: color }} />
    </div>
  );
}

const fmt = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? '—' : v.toFixed(digits));

export default function PredictionPanel({ symbol }: PredictionPanelProps) {
  const [prediction, setPrediction] = useState<any>(null);
  const [signal, setSignal] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const fetchData = async () => {
    setLoading(true);
    setError('');
    const [pred, sig] = await Promise.allSettled([predictionsApi.predict(symbol), signalsApi.getSignal(symbol)]);
    setPrediction(pred.status === 'fulfilled' ? pred.value.data : null);
    setSignal(sig.status === 'fulfilled' ? sig.value.data : null);
    if (pred.status === 'rejected' && sig.status === 'rejected') {
      setError(errorMessage(sig.reason, 'Failed to load AI analysis.'));
    }
    setLoading(false);
  };

  useEffect(() => {
    fetchData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol]);

  const signalClass = signal?.signal === 'BUY' ? 'signal-buy' : signal?.signal === 'SELL' ? 'signal-sell' : 'signal-hold';
  const nextDay = prediction?.predictions?.next_day;
  const isPositive = nextDay ? nextDay.change_pct >= 0 : true;

  return (
    <div className="card p-4 fade-in">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center"
            style={{ background: 'linear-gradient(135deg, #4fa3ff, #a855f7)' }}>
            <FiCpu className="w-4 h-4 text-white" />
          </div>
          <div>
            <h3 className="font-semibold text-sm" style={{ color: '#e8eaf0' }}>AI Analysis</h3>
            <p className="text-xs" style={{ color: '#5a6478' }}>{symbol} • {signal?.regime?.replace('_', ' ') ?? 'multi-factor'}</p>
          </div>
        </div>
        <button onClick={fetchData} className="w-7 h-7 rounded-lg flex items-center justify-center transition-colors hover:bg-white/5"
          style={{ background: '#1a1d24', border: '1px solid #1e2535' }} aria-label="Refresh">
          <FiRefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} style={{ color: '#5a6478' }} />
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 p-3 rounded-lg mb-3"
          style={{ background: 'rgba(255, 71, 87, 0.1)', border: '1px solid rgba(255, 71, 87, 0.2)' }}>
          <FiAlertCircle className="w-4 h-4" style={{ color: '#ff4757' }} />
          <span className="text-xs" style={{ color: '#ff4757' }}>{error}</span>
        </div>
      )}

      {loading ? (
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => <div key={i} className="shimmer h-12 rounded-xl" />)}
        </div>
      ) : (
        <div className="space-y-3">
          {signal && (
            <>
              <div className={`flex items-center justify-between p-4 rounded-xl ${signalClass}`}>
                <div>
                  <p className="text-xs font-medium mb-1" style={{ opacity: 0.8 }}>Signal</p>
                  <p className="text-3xl font-black tracking-wider">{signal.signal}</p>
                </div>
                <div className="text-right">
                  <p className="text-xs mb-1" style={{ opacity: 0.8 }}>Score</p>
                  <p className="text-2xl font-bold">{signal.score > 0 ? '+' : ''}{fmt(signal.score)}</p>
                </div>
              </div>

              {signal.signal === 'BUY' && (
                <div className="grid grid-cols-3 gap-2">
                  {[
                    { label: 'Entry', value: signal.entry_price, color: '#9ba3b8' },
                    { label: 'Target', value: signal.target_price, color: '#00d4aa' },
                    { label: 'Stop', value: signal.stop_loss, color: '#ff4757' },
                  ].map(({ label, value, color }) => (
                    <div key={label} className="p-3 rounded-xl text-center" style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
                      <p className="text-xs mb-1" style={{ color: '#5a6478' }}>{label}</p>
                      <p className="font-mono font-bold text-sm" style={{ color }}>{fmt(value)}</p>
                    </div>
                  ))}
                </div>
              )}

              <div className="p-3 rounded-xl space-y-1.5" style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
                <div className="flex justify-between text-xs">
                  <span style={{ color: '#5a6478' }}>Expected edge / trade</span>
                  <span className="font-mono" style={{ color: '#e8eaf0' }}>{fmt(signal.expected_edge_pct, 2)}%</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span style={{ color: '#5a6478' }}>Round-trip costs</span>
                  <span className="font-mono" style={{ color: '#e8eaf0' }}>{fmt(signal.round_trip_cost_pct, 2)}%</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span style={{ color: '#5a6478' }}>Est. win rate ({signal.edge?.source})</span>
                  <span className="font-mono" style={{ color: '#e8eaf0' }}>{fmt((signal.edge?.p_win ?? 0) * 100, 0)}%</span>
                </div>
                <p className="text-xs pt-1" style={{ color: signal.worth_the_costs ? '#00d4aa' : '#ffd700' }}>
                  {signal.worth_the_costs ? '✓ Edge clears 2× transaction costs' : 'Edge does not justify a trade right now'}
                </p>
              </div>
            </>
          )}

          {nextDay && (
            <div className="p-3 rounded-xl" style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
              <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>NEXT-DAY FORECAST</p>
              <div className="flex items-center justify-between">
                <div>
                  <p className="font-mono font-bold text-lg" style={{ color: '#e8eaf0' }}>{fmt(nextDay.price)}</p>
                  <p className={`font-mono text-xs font-semibold ${isPositive ? 'positive' : 'negative'}`}>
                    {isPositive ? '+' : ''}{fmt(nextDay.change_pct)}%
                  </p>
                </div>
                <div className="text-right w-32">
                  <span className="text-xs" style={{ color: '#00d4aa' }}>P(up) {fmt(nextDay.bullish_probability * 100, 0)}%</span>
                  <Bar value={nextDay.bullish_probability} color="#00d4aa" />
                  <span className="text-xs block mt-1.5" style={{ color: '#ff4757' }}>P(down) {fmt(nextDay.bearish_probability * 100, 0)}%</span>
                  <Bar value={nextDay.bearish_probability} color="#ff4757" />
                </div>
              </div>
              <p className="text-xs mt-2" style={{ color: '#5a6478' }}>
                Model-implied probabilities. Daily moves are mostly noise, so values near 50% are expected.
              </p>
            </div>
          )}

          {prediction && (
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs" style={{ color: '#5a6478' }}>Models:</span>
              {(prediction.models_used || Object.keys(prediction.model_breakdown || {})).map((m: string) => (
                <span key={m} className="text-xs px-2 py-0.5 rounded font-mono" style={{ background: '#1e2535', color: '#9ba3b8' }}>{m}</span>
              ))}
            </div>
          )}

          <p className="text-xs px-3 py-2 rounded-lg" style={{ background: 'rgba(255, 215, 0, 0.06)', color: '#5a6478', border: '1px solid rgba(255, 215, 0, 0.1)' }}>
            For educational analysis only. Not financial advice.
          </p>
        </div>
      )}
    </div>
  );
}
