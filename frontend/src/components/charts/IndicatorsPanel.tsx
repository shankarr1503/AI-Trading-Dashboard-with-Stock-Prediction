'use client';
import { useState, useEffect } from 'react';
import { indicatorsApi } from '@/lib/api';
import { FiBarChart2 } from 'react-icons/fi';

interface IndicatorsPanelProps {
  symbol: string;
}

const INDICATOR_GROUPS = [
  { label: 'Moving Averages', keys: ['SMA_20', 'EMA_20', 'SMA_50', 'EMA_50'] },
  { label: 'Oscillators', keys: ['RSI_14', 'STOCH_K', 'STOCH_D', 'WILLIAMS_R'] },
  { label: 'Trend', keys: ['MACD_line', 'MACD_signal', 'MACD_hist'] },
  { label: 'Volatility', keys: ['BB_upper', 'BB_lower', 'ATR_14', 'BB_bandwidth'] },
];

export default function IndicatorsPanel({ symbol }: IndicatorsPanelProps) {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetch = async () => {
      setLoading(true);
      try {
        const res = await indicatorsApi.get(symbol, '3mo', '1d');
        setData(res.data);
      } catch {}
      setLoading(false);
    };
    fetch();
  }, [symbol]);

  const signals = data?.signals || {};
  const current = data?.current || {};

  const signalColor = (sig: string) => {
    if (sig === 'BUY') return '#00d4aa';
    if (sig === 'SELL') return '#ff4757';
    return '#ffd700';
  };

  const signalBg = (sig: string) => {
    if (sig === 'BUY') return 'rgba(0,212,170,0.1)';
    if (sig === 'SELL') return 'rgba(255,71,87,0.1)';
    return 'rgba(255,215,0,0.08)';
  };

  return (
    <div className="card p-4 fade-in">
      <div className="flex items-center gap-2 mb-4">
        <div className="w-7 h-7 rounded-lg flex items-center justify-center"
          style={{ background: 'linear-gradient(135deg, #06b6d4, #4fa3ff)' }}>
          <FiBarChart2 className="w-4 h-4 text-white" />
        </div>
        <h3 className="font-semibold text-sm" style={{ color: '#e8eaf0' }}>Technical Indicators</h3>
        <span className="text-xs ml-auto font-mono" style={{ color: '#5a6478' }}>{symbol}</span>
      </div>

      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => <div key={i} className="shimmer h-8 rounded-lg" />)}
        </div>
      ) : (
        <div className="space-y-4">
          {/* Indicator Signals */}
          {Object.keys(signals).length > 0 && (
            <div>
              <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>SIGNALS SUMMARY</p>
              <div className="space-y-1.5">
                {Object.entries(signals).map(([name, sig]: any) => (
                  <div key={name} className="flex items-center justify-between px-3 py-2 rounded-lg"
                    style={{ background: '#1a1d24' }}>
                    <span className="text-xs font-semibold" style={{ color: '#9ba3b8' }}>{name.replace('_', ' ')}</span>
                    <div className="flex items-center gap-2">
                      <span className="text-xs" style={{ color: '#5a6478' }}>{sig.reason}</span>
                      <span className="text-xs px-2 py-0.5 rounded font-bold"
                        style={{ background: signalBg(sig.signal), color: signalColor(sig.signal) }}>
                        {sig.signal}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Current Values */}
          <div>
            <p className="text-xs font-semibold mb-2" style={{ color: '#9ba3b8' }}>CURRENT VALUES</p>
            <div className="grid grid-cols-2 gap-1.5">
              {[
                { label: 'RSI (14)', value: current.RSI_14?.toFixed(1), color: current.RSI_14 > 70 ? '#ff4757' : current.RSI_14 < 30 ? '#00d4aa' : '#9ba3b8' },
                { label: 'MACD', value: current.MACD_line?.toFixed(3) },
                { label: 'SMA 20', value: current.SMA_20?.toFixed(2) },
                { label: 'EMA 20', value: current.EMA_20?.toFixed(2) },
                { label: 'SMA 50', value: current.SMA_50?.toFixed(2) },
                { label: 'ATR 14', value: current.ATR_14?.toFixed(3) },
                { label: 'Stoch %K', value: current.STOCH_K?.toFixed(1) },
                { label: 'BB Width', value: current.BB_bandwidth?.toFixed(3) },
              ].map(({ label, value, color }) => value != null && (
                <div key={label} className="flex items-center justify-between px-2 py-1.5 rounded"
                  style={{ background: '#1a1d24' }}>
                  <span className="text-xs" style={{ color: '#5a6478' }}>{label}</span>
                  <span className="font-mono text-xs font-semibold" style={{ color: color || '#9ba3b8' }}>{value}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
