'use client';
import { useState } from 'react';
import Header from '@/components/layout/Header';
import CandlestickChart from '@/components/charts/CandlestickChart';
import IndicatorsPanel from '@/components/charts/IndicatorsPanel';
import Watchlist from '@/components/market/Watchlist';
import MarketMovers from '@/components/market/MarketMovers';
import PredictionPanel from '@/components/prediction/PredictionPanel';
import PortfolioSummary from '@/components/portfolio/PortfolioSummary';
import SentimentGauge from '@/components/sentiment/SentimentGauge';
import TickerBar from '@/components/market/TickerBar';
import ResearchReport from '@/components/research/ResearchReport';

const TAB_ITEMS = ['Chart', 'Research', 'Indicators', 'Portfolio', 'Sentiment'] as const;
type Tab = typeof TAB_ITEMS[number];

export default function DashboardPage() {
  const [activeSymbol, setActiveSymbol] = useState('AAPL');
  const [activeTab, setActiveTab] = useState<Tab>('Chart');

  return (
    <div className="min-h-screen" style={{ background: '#0a0b0d' }}>
      {/* Header */}
      <Header onSymbolSelect={setActiveSymbol} activeSymbol={activeSymbol} />

      <TickerBar />

      {/* Main Layout */}
      <div className="flex h-[calc(100vh-90px)] overflow-hidden">

        {/* ── Left Sidebar ──────────────────────────────────────── */}
        <div className="w-56 flex-shrink-0 overflow-y-auto p-3 space-y-3" style={{ borderRight: '1px solid #1e2535' }}>
          <Watchlist onSymbolSelect={setActiveSymbol} activeSymbol={activeSymbol} />
          <MarketMovers onSymbolSelect={setActiveSymbol} />
        </div>

        {/* ── Center Content ─────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* Tab Nav */}
          <div className="flex gap-2">
            {TAB_ITEMS.map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className="px-4 py-2 rounded-xl text-sm font-semibold transition-all"
                style={{
                  background: activeTab === tab ? '#4fa3ff' : '#1a1d24',
                  color: activeTab === tab ? 'white' : '#5a6478',
                  border: activeTab === tab ? 'none' : '1px solid #1e2535',
                }}>
                {tab}
              </button>
            ))}
          </div>

          {/* Chart Tab */}
          {activeTab === 'Chart' && (
            <div className="fade-in space-y-4">
              <CandlestickChart symbol={activeSymbol} />
            </div>
          )}

          {/* Research Tab */}
          {activeTab === 'Research' && <ResearchReport symbol={activeSymbol} />}

          {/* Indicators Tab */}
          {activeTab === 'Indicators' && (
            <div className="fade-in">
              <IndicatorsPanel symbol={activeSymbol} />
            </div>
          )}

          {/* Portfolio Tab */}
          {activeTab === 'Portfolio' && (
            <div className="fade-in">
              <PortfolioSummary />
            </div>
          )}

          {/* Sentiment Tab */}
          {activeTab === 'Sentiment' && (
            <div className="fade-in">
              <SentimentGauge symbol={activeSymbol} />
            </div>
          )}
        </div>

        {/* ── Right Sidebar ──────────────────────────────────────── */}
        <div className="w-80 flex-shrink-0 overflow-y-auto p-3 space-y-3" style={{ borderLeft: '1px solid #1e2535' }}>
          <PredictionPanel symbol={activeSymbol} />
        </div>
      </div>
    </div>
  );
}
