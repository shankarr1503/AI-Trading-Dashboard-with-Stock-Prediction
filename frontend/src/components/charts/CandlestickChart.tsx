'use client';
import { useState, useEffect, useRef } from 'react';
import { createChart, ColorType, CrosshairMode, LineStyle } from 'lightweight-charts';
import { marketApi, indicatorsApi } from '@/lib/api';

interface CandlestickChartProps {
  symbol: string;
}

const INTERVALS = [
  { label: '1D', period: '5d',  interval: '5m' },
  { label: '1W', period: '1mo', interval: '1h' },
  { label: '1M', period: '3mo', interval: '1d' },
  { label: '3M', period: '6mo', interval: '1d' },
  { label: '1Y', period: '1y',  interval: '1d' },
  { label: '5Y', period: '5y',  interval: '1wk' },
];

export default function CandlestickChart({ symbol }: CandlestickChartProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<any>(null);
  const candleSeriesRef = useRef<any>(null);
  const volumeSeriesRef = useRef<any>(null);
  const [activeInterval, setActiveInterval] = useState(INTERVALS[3]); // 3M default
  const [loading, setLoading] = useState(true);
  const [quote, setQuote] = useState<any>(null);
  const [showMA, setShowMA] = useState(true);
  const [showBB, setShowBB] = useState(false);

  // Initialize lightweight-charts
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#141720' },
        textColor: '#9ba3b8',
      },
      grid: {
        vertLines: { color: '#1a1d24', style: LineStyle.Dotted },
        horzLines: { color: '#1a1d24', style: LineStyle.Dotted },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: {
        borderColor: '#1e2535',
        textColor: '#9ba3b8',
      },
      timeScale: {
        borderColor: '#1e2535',
        timeVisible: true,
        secondsVisible: false,
      },
      width: chartContainerRef.current.clientWidth,
      height: 420,
    });

    // Candlestick series
    const candleSeries = chart.addCandlestickSeries({
      upColor: '#00d4aa',
      downColor: '#ff4757',
      borderUpColor: '#00d4aa',
      borderDownColor: '#ff4757',
      wickUpColor: '#00d4aa',
      wickDownColor: '#ff4757',
    });

    // Volume series (histogram at bottom)
    const volumeSeries = chart.addHistogramSeries({
      color: '#4fa3ff',
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;

    // Resize observer
    const ro = new ResizeObserver(entries => {
      if (entries[0]) {
        chart.applyOptions({ width: entries[0].contentRect.width });
      }
    });
    ro.observe(chartContainerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, []);

  // Load data when symbol or interval changes
  useEffect(() => {
    if (!candleSeriesRef.current) return;
    loadChartData();
  }, [symbol, activeInterval]);

  const loadChartData = async () => {
    setLoading(true);
    try {
      const [histRes, quoteRes] = await Promise.all([
        marketApi.getHistory(symbol, activeInterval.period, activeInterval.interval),
        marketApi.getQuote(symbol),
      ]);
      setQuote(quoteRes.data);

      const candles = histRes.data.map((d: any) => ({
        time: new Date(d.timestamp).getTime() / 1000,
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
      }));

      const volumes = histRes.data.map((d: any, i: number) => ({
        time: new Date(d.timestamp).getTime() / 1000,
        value: d.volume,
        color: i > 0 && d.close >= histRes.data[i - 1]?.close
          ? 'rgba(0, 212, 170, 0.35)'
          : 'rgba(255, 71, 87, 0.35)',
      }));

      candleSeriesRef.current.setData(candles);
      volumeSeriesRef.current.setData(volumes);
      chartRef.current.timeScale().fitContent();
    } catch (e) {
      console.error('Chart data load failed:', e);
    }
    setLoading(false);
  };

  const isPositive = quote ? quote.change_pct >= 0 : true;

  return (
    <div className="card p-4 fade-in">
      {/* Header row */}
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-bold font-mono" style={{ color: '#e8eaf0' }}>{symbol}</h2>
            {quote && (
              <span className={`text-xs px-2 py-0.5 rounded font-semibold ${isPositive ? 'signal-buy' : 'signal-sell'}`}>
                {quote.exchange}
              </span>
            )}
          </div>
          {quote && (
            <div className="flex items-baseline gap-3 mt-1">
              <span className="text-3xl font-bold font-mono" style={{ color: '#e8eaf0' }}>
                {quote.current_price?.toFixed(2)}
              </span>
              <span className={`text-sm font-semibold font-mono ${isPositive ? 'positive' : 'negative'}`}>
                {isPositive ? '+' : ''}{quote.change?.toFixed(2)} ({isPositive ? '+' : ''}{quote.change_pct?.toFixed(2)}%)
              </span>
            </div>
          )}
          {quote && (
            <p className="text-xs mt-1" style={{ color: '#5a6478' }}>{quote.name}</p>
          )}
        </div>
        
        {/* Volume & Market Cap */}
        {quote && (
          <div className="text-right">
            <div className="text-xs" style={{ color: '#5a6478' }}>
              Vol: <span style={{ color: '#9ba3b8' }}>{(quote.volume / 1e6).toFixed(1)}M</span>
            </div>
            {quote.market_cap > 0 && (
              <div className="text-xs mt-1" style={{ color: '#5a6478' }}>
                MCap: <span style={{ color: '#9ba3b8' }}>${(quote.market_cap / 1e9).toFixed(1)}B</span>
              </div>
            )}
            <div className="text-xs mt-1" style={{ color: '#5a6478' }}>
              52W: <span className="positive">{quote['52_week_high']?.toFixed(0)}</span>
              {' / '}
              <span className="negative">{quote['52_week_low']?.toFixed(0)}</span>
            </div>
          </div>
        )}
      </div>

      {/* Interval Selector + Overlay toggles */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex gap-1">
          {INTERVALS.map((iv) => (
            <button
              key={iv.label}
              onClick={() => setActiveInterval(iv)}
              className="px-3 py-1 rounded-lg text-xs font-semibold transition-all"
              style={{
                background: activeInterval.label === iv.label ? '#4fa3ff' : '#1a1d24',
                color: activeInterval.label === iv.label ? 'white' : '#5a6478',
              }}>
              {iv.label}
            </button>
          ))}
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setShowMA(!showMA)}
            className="px-2 py-1 rounded text-xs font-mono transition-all"
            style={{
              background: showMA ? 'rgba(79, 163, 255, 0.15)' : '#1a1d24',
              color: showMA ? '#4fa3ff' : '#5a6478',
              border: `1px solid ${showMA ? 'rgba(79, 163, 255, 0.3)' : '#1e2535'}`,
            }}>
            MA
          </button>
          <button
            onClick={() => setShowBB(!showBB)}
            className="px-2 py-1 rounded text-xs font-mono transition-all"
            style={{
              background: showBB ? 'rgba(168, 85, 247, 0.15)' : '#1a1d24',
              color: showBB ? '#a855f7' : '#5a6478',
              border: `1px solid ${showBB ? 'rgba(168, 85, 247, 0.3)' : '#1e2535'}`,
            }}>
            BB
          </button>
        </div>
      </div>

      {/* Chart Container */}
      <div className="relative rounded-xl overflow-hidden" style={{ background: '#141720' }}>
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center z-10 rounded-xl"
            style={{ background: '#141720' }}>
            <div className="text-center">
              <div className="w-8 h-8 border-2 rounded-full animate-spin mx-auto mb-2"
                style={{ borderColor: '#4fa3ff', borderTopColor: 'transparent' }} />
              <p className="text-xs" style={{ color: '#5a6478' }}>Loading chart data...</p>
            </div>
          </div>
        )}
        <div ref={chartContainerRef} className="w-full" />
      </div>

      {/* OHLC Row */}
      {quote && (
        <div className="grid grid-cols-4 gap-3 mt-3 pt-3" style={{ borderTop: '1px solid #1e2535' }}>
          {[
            { label: 'Open', value: quote.open },
            { label: 'High', value: quote.day_high, color: '#00d4aa' },
            { label: 'Low',  value: quote.day_low,  color: '#ff4757' },
            { label: 'Prev Close', value: quote.previous_close },
          ].map(({ label, value, color }) => (
            <div key={label} className="text-center">
              <p className="text-xs mb-1" style={{ color: '#5a6478' }}>{label}</p>
              <p className="font-mono font-semibold text-sm" style={{ color: color || '#9ba3b8' }}>
                {value?.toFixed(2) ?? '--'}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
