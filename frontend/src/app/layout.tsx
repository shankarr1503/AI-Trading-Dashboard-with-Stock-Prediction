import type { Metadata } from 'next';
import { Inter } from 'next/font/google';
import '../styles/globals.css';

const inter = Inter({ subsets: ['latin'], variable: '--font-inter' });

export const metadata: Metadata = {
  title: 'AI Trading Platform — Real-Time Market Analytics & Predictions',
  description:
    'Professional AI-powered trading analytics platform with real-time market data, LSTM stock predictions, technical indicators, portfolio analytics, and market sentiment analysis.',
  keywords:
    'AI trading, stock prediction, technical analysis, LSTM, market analytics, NSE, BSE, portfolio',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={inter.variable}>
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
        <meta name="theme-color" content="#0a0b0d" />
      </head>
      <body className="bg-bg-primary text-text-primary antialiased">
        {children}
      </body>
    </html>
  );
}
