import assert from 'node:assert/strict';
import { test } from 'node:test';

import { detectTicker, normalizeSymbol, SUPPORTED_SITES, SYMBOL_RE } from '../lib/tickers.js';

const sym = (url) => (detectTicker(url) || {}).symbol ?? null;

test('symbol regex is the backend one', () => {
  assert.equal(SYMBOL_RE.source, '^[A-Z0-9^.\\-=]{1,20}$');
});

test('normalizeSymbol upper-cases, trims and validates', () => {
  assert.equal(normalizeSymbol(' aapl '), 'AAPL');
  assert.equal(normalizeSymbol('brk-b'), 'BRK-B');
  assert.equal(normalizeSymbol('^gspc'), '^GSPC');
  assert.equal(normalizeSymbol('eurusd=x'), 'EURUSD=X');
  assert.equal(normalizeSymbol('reliance.ns'), 'RELIANCE.NS');
  for (const bad of ['', '   ', 'AA PL', 'M&M.NS', 'A'.repeat(21), '<script>', '...', '.SPX', 'AAPL.', null, undefined]) {
    assert.equal(normalizeSymbol(bad), null, `should reject ${JSON.stringify(bad)}`);
  }
});

test('Yahoo Finance', () => {
  assert.equal(sym('https://finance.yahoo.com/quote/AAPL/'), 'AAPL');
  assert.equal(sym('https://finance.yahoo.com/quote/AAPL?p=AAPL&.tsrc=fin-srch'), 'AAPL');
  assert.equal(sym('https://finance.yahoo.com/quote/MSFT/history/'), 'MSFT');
  assert.equal(sym('https://finance.yahoo.com/quote/%5EGSPC/'), '^GSPC');
  assert.equal(sym('https://finance.yahoo.com/quote/BRK-B/'), 'BRK-B');
  assert.equal(sym('https://uk.finance.yahoo.com/quote/HSBA.L/'), 'HSBA.L');
  assert.equal(sym('https://in.finance.yahoo.com/quote/RELIANCE.NS'), 'RELIANCE.NS');
  assert.equal(sym('https://finance.yahoo.com/quote/EURUSD=X/'), 'EURUSD=X');
  assert.equal(sym('https://finance.yahoo.com/chart/TSLA'), 'TSLA');
  assert.equal(sym('https://finance.yahoo.com/'), null);
  assert.equal(sym('https://finance.yahoo.com/news/some-article.html'), null);
  assert.deepEqual(detectTicker('https://finance.yahoo.com/quote/NVDA/'), { symbol: 'NVDA', source: 'Yahoo Finance' });
});

test('Google Finance maps the exchange to a Yahoo suffix', () => {
  assert.equal(sym('https://www.google.com/finance/quote/AAPL:NASDAQ'), 'AAPL');
  assert.equal(sym('https://www.google.com/finance/quote/BRK.B:NYSE?hl=en'), 'BRK-B');
  assert.equal(sym('https://www.google.com/finance/quote/RELIANCE:NSE'), 'RELIANCE.NS');
  assert.equal(sym('https://www.google.com/finance/quote/500325:BOM'), '500325.BO');
  assert.equal(sym('https://www.google.com/finance/quote/HSBA:LON'), 'HSBA.L');
  assert.equal(sym('https://www.google.com/finance/quote/SHOP:TSE'), 'SHOP.TO');
  assert.equal(sym('https://www.google.com/finance/quote/.INX:INDEXSP'), null, 'unknown exchange is not guessed');
  assert.equal(sym('https://www.google.com/finance/quote/EUR-USD'), null, 'currencies are not stocks');
  assert.equal(sym('https://www.google.com/search?q=AAPL'), null);
});

test('TradingView symbol pages and charts', () => {
  assert.equal(sym('https://www.tradingview.com/symbols/NASDAQ-AAPL/'), 'AAPL');
  assert.equal(sym('https://www.tradingview.com/symbols/NYSE-BRK.B/'), 'BRK-B');
  assert.equal(sym('https://www.tradingview.com/symbols/NSE-RELIANCE/'), 'RELIANCE.NS');
  assert.equal(sym('https://www.tradingview.com/symbols/BSE-TCS/'), 'TCS.BO');
  assert.equal(sym('https://in.tradingview.com/symbols/NSE-INFY/technicals/'), 'INFY.NS');
  assert.equal(sym('https://www.tradingview.com/symbols/MSFT/'), 'MSFT');
  assert.equal(sym('https://www.tradingview.com/chart/?symbol=NASDAQ:AAPL'), 'AAPL');
  assert.equal(sym('https://www.tradingview.com/chart/AbCdEf12/?symbol=NSE%3AINFY'), 'INFY.NS');
  assert.equal(sym('https://www.tradingview.com/chart/?symbol=TSLA'), 'TSLA');
  assert.equal(sym('https://www.tradingview.com/symbols/BINANCE-BTCUSDT/'), null, 'crypto venues are not guessed');
  assert.equal(sym('https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT'), null);
  assert.equal(sym('https://www.tradingview.com/chart/'), null);
  assert.equal(sym('https://www.tradingview.com/markets/'), null);
});

test('MarketWatch', () => {
  assert.equal(sym('https://www.marketwatch.com/investing/stock/aapl'), 'AAPL');
  assert.equal(sym('https://www.marketwatch.com/investing/stock/aapl/financials?mod=mw_quote_tab'), 'AAPL');
  assert.equal(sym('https://www.marketwatch.com/investing/fund/spy'), 'SPY');
  assert.equal(sym('https://www.marketwatch.com/investing/stock/hsba?countrycode=uk'), 'HSBA.L');
  assert.equal(sym('https://www.marketwatch.com/investing/stock/reliance?countrycode=in'), null, 'NSE or BSE: ambiguous');
  assert.equal(sym('https://www.marketwatch.com/investing/stock/brk.b'), 'BRK-B');
  assert.equal(sym('https://www.marketwatch.com/latest-news'), null);
});

test('Seeking Alpha, Finviz, CNBC, Nasdaq', () => {
  assert.equal(sym('https://seekingalpha.com/symbol/AAPL'), 'AAPL');
  assert.equal(sym('https://seekingalpha.com/symbol/BRK.B/earnings'), 'BRK-B');
  assert.equal(sym('https://seekingalpha.com/article/123-foo'), null);
  assert.equal(sym('https://finviz.com/quote.ashx?t=AAPL&p=d'), 'AAPL');
  assert.equal(sym('https://elite.finviz.com/quote.ashx?t=brk-b'), 'BRK-B');
  assert.equal(sym('https://finviz.com/screener.ashx?v=111'), null);
  assert.equal(sym('https://www.cnbc.com/quotes/AAPL'), 'AAPL');
  assert.equal(sym('https://www.cnbc.com/quotes/BRK.B?tab=news'), 'BRK-B');
  assert.equal(sym('https://www.cnbc.com/quotes/VOD-GB'), 'VOD.L');
  assert.equal(sym('https://www.cnbc.com/quotes/RELIANCE-IN'), null, 'unmapped country is not guessed');
  assert.equal(sym('https://www.cnbc.com/quotes/.SPX'), null, 'CNBC index notation is not a Yahoo symbol');
  assert.equal(sym('https://www.nasdaq.com/market-activity/stocks/aapl'), 'AAPL');
  assert.equal(sym('https://www.nasdaq.com/market-activity/stocks/msft/earnings'), 'MSFT');
  assert.equal(sym('https://www.nasdaq.com/market-activity/etf/qqq'), 'QQQ');
  assert.equal(sym('https://www.nasdaq.com/market-activity/index/comp'), null);
});

test('NSE and BSE India', () => {
  assert.equal(sym('https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE'), 'RELIANCE.NS');
  assert.equal(sym('https://www.nseindia.com/get-quotes/equity?symbol=bajaj-auto'), 'BAJAJ-AUTO.NS');
  assert.equal(sym('https://www.nseindia.com/get-quotes/equity?symbol=M%26M'), null, '& is not a valid symbol character');
  assert.equal(sym('https://www.nseindia.com/get-quotes/derivatives?symbol=NIFTY'), null);
  assert.equal(sym('https://www.bseindia.com/stock-share-price/reliance-industries-ltd/reliance/500325/'), 'RELIANCE.BO');
  assert.equal(sym('https://www.bseindia.com/stock-share-price/tata-consultancy-services-ltd/tcs/532540/financials-results/'),
    'TCS.BO');
  assert.equal(sym('https://www.bseindia.com/stock-share-price/x/m&m/500520/'), '500520.BO', 'falls back to the scrip code');
  assert.equal(sym('https://www.bseindia.com/markets/equity/EQReports/StockPrcHistori.aspx?scripcode=500325'), null);
});

test('unsupported, malformed and non-web URLs give null', () => {
  for (const url of [
    '', null, undefined, 'not a url', 'chrome://extensions/', 'chrome-extension://abc/popup.html',
    'file:///home/user/AAPL.html', 'https://example.com/quote/AAPL', 'https://finance.yahoo.com.evil.example/quote/AAPL',
    'https://evilfinance.yahoo.com.example/quote/AAPL', 'https://finance.yahoo.com/quote/%E0%A4%A/', 'about:blank',
  ]) {
    assert.equal(detectTicker(url), null, `expected null for ${url}`);
  }
});

test('every detector is reachable', () => {
  assert.deepEqual(SUPPORTED_SITES, [
    'Yahoo Finance', 'Google Finance', 'TradingView', 'MarketWatch', 'Seeking Alpha', 'Finviz', 'CNBC', 'Nasdaq',
    'NSE India', 'BSE India',
  ]);
});
