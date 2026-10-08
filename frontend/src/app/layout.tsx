import type { Metadata } from 'next';
import { Inter, JetBrains_Mono } from 'next/font/google';
import '../styles/globals.css';

// next/font downloads the fonts at build time and serves them from /_next/static/media, so the
// app (including the offline desktop build) never needs Google's font servers at runtime.
// `--font-inter` / `--font-mono` let CSS and Tailwind (`font-sans` / `font-mono`) refer to them.
const inter = Inter({ subsets: ['latin'], variable: '--font-inter' });
const mono = JetBrains_Mono({ subsets: ['latin'], variable: '--font-mono' });

export const metadata: Metadata = {
  title: 'AI Trading Platform — Real-Time Market Analytics & Predictions',
  description:
    'Professional AI-powered trading analytics platform with real-time market data, LSTM stock predictions, technical indicators, portfolio analytics, and market sentiment analysis.',
  keywords:
    'AI trading, stock prediction, technical analysis, LSTM, market analytics, NSE, BSE, portfolio',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable}`}>
      <head>
        <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
        <meta name="theme-color" content="#0a0b0d" />
      </head>
      <body className={`${inter.className} bg-bg-primary text-text-primary antialiased`}>
        {children}
      </body>
    </html>
  );
}
