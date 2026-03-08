'use client';
import { useState, useEffect } from 'react';
import { FiCpu, FiAlertCircle, FiRefreshCw } from 'react-icons/fi';
import { predictionsApi, signalsApi } from '@/lib/api';

interface PredictionPanelProps {
  symbol: string;
}

function ConfidenceBar({ value, color }: { value: number; color: string }) {
  return (
    <div className="confidence-bar mt-1.5">
      <div className="confidence-fill" style={{ width: `${value * 100}%`, background: color }} />
    </div>
  );
}

export default function PredictionPanel({ symbol }: PredictionPanelProps) {
  const [prediction, setPrediction] = useState<any>(null);
  const [signal, setSignal] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const fetchData = async () => {
    setLoading(true);
    setError('');
    try {
      const [predRes, signalRes] = await Promise.all([
        predictionsApi.predict(symbol),
        signalsApi.getSignal(symbol),
      ]);
      setPrediction(predRes.data);
      setSignal(signalRes.data);
    } catch (e: any) {
      setError('Failed to load AI predictions. Please try again.');
    }
    setLoading(false);
  };

  useEffect(() => { fetchData(); }, [symbol]);

  const signalColor = signal?.signal === 'BUY' ? '#00d4aa' : signal?.signal === 'SELL' ? '#ff4757' : '#ffd700';
  const signalClass = signal?.signal === 'BUY' ? 'signal-buy' : signal?.signal === 'SELL' ? 'signal-sell' : 'signal-hold';
  const nextDay = prediction?.predictions?.next_day;
  const nextWeek = prediction?.predictions?.next_week;
  const isPositive = nextDay ? nextDay.change_pct >= 0 : true;

  return (
    <div className="card p-4 fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center"
            style={{ background: 'linear-gradient(135deg, #4fa3ff, #a855f7)' }}>
            <FiCpu className="w-4 h-4 text-white" />
          </div>
          <div>
            <h3 className="font-semibold text-sm" style={{ color: '#e8eaf0' }}>AI Prediction</h3>
            <p className="text-xs" style={{ color: '#5a6478' }}>{symbol} • ML Ensemble</p>
          </div>
        </div>
        <button onClick={fetchData} className="w-7 h-7 rounded-lg flex items-center justify-center transition-colors hover:bg-white/5"
          style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
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
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="shimmer h-12 rounded-xl" />
          ))}
        </div>
      ) : prediction && signal && (
        <div className="space-y-3">
          {/* AI Signal Banner */}
          <div className={`flex items-center justify-between p-4 rounded-xl ${signalClass}`}
            style={{ transition: 'all 0.3s' }}>
            <div>
              <p className="text-xs font-medium mb-1" style={{ opacity: 0.8 }}>AI Signal</p>
              <p className="text-3xl font-black tracking-wider">{signal.signal}</p>
            </div>
            <div className="text-right">
              <p className="text-xs mb-1" style={{ opacity: 0.8 }}>Confidence</p>
              <p className="text-2xl font-bold">{signal.confidence_pct}%</p>
            </div>
          </div>

          {/* Price Targets */}
          <div className="grid grid-cols-3 gap-2">
            {[
              { label: 'Entry', value: signal.entry_price, color: '#9ba3b8' },
              { label: 'Target', value: signal.target_price, color: '#00d4aa' },
              { label: 'Stop Loss', value: signal.stop_loss, color: '#ff4757' },
            ].map(({ label, value, color }) => (
              <div key={label} className="p-3 rounded-xl text-center"
                style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
                <p className="text-xs mb-1" style={{ color: '#5a6478' }}>{label}</p>
                <p className="font-mono font-bold text-sm" style={{ color }}>{value?.toFixed(2)}</p>
              </div>
            ))}
          </div>

          {/* Risk/Reward */}
          {signal.risk_reward_ratio > 0 && (
            <div className="flex items-center justify-between px-3 py-2 rounded-lg"
              style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
              <span className="text-xs" style={{ color: '#5a6478' }}>Risk / Reward</span>
              <span className="font-mono font-semibold text-sm" style={{ color: signal.risk_reward_ratio >= 2 ? '#00d4aa' : '#ffd700' }}>
                1 : {signal.risk_reward_ratio}
              </span>
            </div>
          )}

          {/* Next Day Prediction */}
          <div className="p-3 rounded-xl" style={{ background: '#1a1d24', border: '1px solid #1e2535' }}>
            <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>NEXT-DAY PRICE FORECAST</p>
            <div className="flex items-center justify-between">
              <div>
                <p className="font-mono font-bold text-lg" style={{ color: '#e8eaf0' }}>
                  ${nextDay?.price?.toFixed(2)}
                </p>
                <p className={`font-mono text-xs font-semibold ${isPositive ? 'positive' : 'negative'}`}>
                  {isPositive ? '+' : ''}{nextDay?.change_pct?.toFixed(2)}%
                </p>
              </div>
              <div className="text-right">
                <div className="flex items-center gap-2 justify-end text-xs mb-1">
                  <span style={{ color: '#00d4aa' }}>Bullish {(nextDay?.bullish_probability * 100).toFixed(0)}%</span>
                </div>
                <ConfidenceBar value={nextDay?.bullish_probability} color="#00d4aa" />
                <div className="flex items-center gap-2 justify-end text-xs mt-1.5">
                  <span style={{ color: '#ff4757' }}>Bearish {(nextDay?.bearish_probability * 100).toFixed(0)}%</span>
                </div>
                <ConfidenceBar value={nextDay?.bearish_probability} color="#ff4757" />
              </div>
            </div>
          </div>

          {/* Models used */}
          <div className="flex items-center gap-2">
            <span className="text-xs" style={{ color: '#5a6478' }}>Models:</span>
            {(prediction.models_used || ['Statistical']).map((m: string) => (
              <span key={m} className="text-xs px-2 py-0.5 rounded font-mono"
                style={{ background: '#1e2535', color: '#9ba3b8' }}>{m}</span>
            ))}
          </div>

          {/* Disclaimer */}
          <p className="text-xs px-3 py-2 rounded-lg" style={{ background: 'rgba(255, 215, 0, 0.06)', color: '#5a6478', border: '1px solid rgba(255, 215, 0, 0.1)' }}>
            ⚠️ For educational analysis only. Not financial advice.
          </p>
        </div>
      )}
    </div>
  );
}
