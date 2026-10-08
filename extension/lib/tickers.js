// Detect the stock ticker a finance page is about from its URL alone (no page
// content is read). Returns Yahoo-style symbols, which is what the backend uses:
// AAPL, BRK-B, RELIANCE.NS, 500325.BO, HSBA.L, ^GSPC ...

// Same rule as backend/market_data/service.py (_SYMBOL_RE).
export const SYMBOL_RE = /^[A-Z0-9^.\-=]{1,20}$/;

/** Upper-case, trim and validate a symbol; null when the backend would reject it. */
export function normalizeSymbol(input) {
  if (input === null || input === undefined) return null;
  const s = String(input).trim().toUpperCase();
  if (!SYMBOL_RE.test(s)) return null;
  // Not wrong per the regex, but never a real symbol: only punctuation, or a
  // leading/trailing separator (".SPX" is CNBC's index notation, not Yahoo's).
  if (!/[A-Z0-9]/.test(s) || /^[.\-=]|[.\-]$/.test(s)) return null;
  return s;
}

// Exchange code (as used by TradingView / Google Finance) → Yahoo suffix.
// '' = US listing (no suffix). Unknown exchanges are not guessed.
const EXCHANGE_SUFFIX = {
  NASDAQ: '', NYSE: '', NYSEARCA: '', NYSEAMERICAN: '', AMEX: '', ARCA: '', BATS: '', CBOE: '',
  OTC: '', OTCMKTS: '', NMS: '', NGM: '', NCM: '',
  NSE: '.NS', BSE: '.BO', BOM: '.BO',
  LSE: '.L', LON: '.L',
  TSX: '.TO', TSE: '.TO', TSXV: '.V', CVE: '.V',
  ASX: '.AX', HKEX: '.HK', HKG: '.HK',
  XETR: '.DE', XETRA: '.DE', ETR: '.DE', FWB: '.F', FRA: '.F',
  EURONEXT: null, EPA: '.PA', AMS: '.AS', EBR: '.BR', ELI: '.LS',
  SIX: '.SW', SWX: '.SW', TYO: '.T', TSEJ: '.T', KRX: '.KS', SGX: '.SI', SES: '.SI',
  MIL: '.MI', BIT: '.MI', BME: '.MC', STO: '.ST', OSL: '.OL', CPH: '.CO', HEL: '.HE',
};

const US_EXCHANGES = new Set(Object.keys(EXCHANGE_SUFFIX).filter((k) => EXCHANGE_SUFFIX[k] === ''));

// MarketWatch ?countrycode= → Yahoo suffix ("in" is ambiguous: NSE or BSE).
const MARKETWATCH_COUNTRY = { us: '', uk: '.L', ca: '.TO', au: '.AX', hk: '.HK', de: '.DE', jp: '.T', fr: '.PA', nl: '.AS', ch: '.SW', sg: '.SI', es: '.MC', it: '.MI' };

/** Share classes are written BRK.B on most sites but BRK-B on Yahoo (US listings only). */
function usClass(sym) {
  return /^[A-Z]{1,5}\.[A-Z]$/.test(sym) ? sym.replace('.', '-') : sym;
}

function withExchange(base, exchange) {
  const ex = String(exchange || '').toUpperCase();
  if (!ex) return normalizeSymbol(usClass(String(base).toUpperCase()));
  if (!(ex in EXCHANGE_SUFFIX) || EXCHANGE_SUFFIX[ex] === null) return null;   // unknown market: don't guess
  const b = String(base).toUpperCase();
  return normalizeSymbol(US_EXCHANGES.has(ex) ? usClass(b) : `${b}${EXCHANGE_SUFFIX[ex]}`);
}

function segment(pathname, index) {
  const parts = pathname.split('/').filter(Boolean);
  const raw = parts[index];
  if (raw === undefined) return null;
  try {
    return decodeURIComponent(raw);
  } catch {
    return null;
  }
}

function hostIs(hostname, domain) {
  return hostname === domain || hostname.endsWith(`.${domain}`);
}

// Each detector gets a URL object and returns a symbol or null.
const DETECTORS = [
  {
    name: 'Yahoo Finance',
    match: (u) => hostIs(u.hostname, 'finance.yahoo.com'),
    detect(u) {
      // /quote/AAPL/, /quote/AAPL/history, /quote/%5EGSPC, /chart/AAPL
      const m = u.pathname.match(/^\/(?:quote|chart)\/([^/]+)/i);
      if (m) return normalizeSymbol(decodeURIComponent(m[1]));
      const p = u.searchParams.get('p') || u.searchParams.get('s');
      return p ? normalizeSymbol(p) : null;
    },
  },
  {
    name: 'Google Finance',
    match: (u) => hostIs(u.hostname, 'google.com') && /^\/finance\//.test(u.pathname),
    detect(u) {
      // /finance/quote/AAPL:NASDAQ, /finance/quote/RELIANCE:NSE
      const m = u.pathname.match(/^\/finance\/quote\/([^/]+)/);
      if (!m) return null;
      // Stocks always carry the exchange; "EUR-USD" style pages (currencies) are not stocks.
      const [base, exchange] = decodeURIComponent(m[1]).split(':');
      return exchange ? withExchange(base, exchange) : null;
    },
  },
  {
    name: 'TradingView',
    match: (u) => hostIs(u.hostname, 'tradingview.com'),
    detect(u) {
      // /symbols/NASDAQ-AAPL/, /symbols/AAPL/ ; /chart/?symbol=NASDAQ:AAPL, /chart/xyz/?symbol=NSE%3AINFY
      const sym = u.searchParams.get('symbol') || u.searchParams.get('tvwidgetsymbol');
      if (/^\/chart\b/.test(u.pathname) || (sym && !/^\/symbols\//.test(u.pathname))) {
        if (!sym) return null;
        const [a, b] = sym.split(':');
        return b === undefined ? withExchange(a, '') : withExchange(b, a);
      }
      const m = u.pathname.match(/^\/symbols\/([^/]+)/);
      if (!m) return null;
      const s = decodeURIComponent(m[1]);
      // EXCHANGE-SYMBOL; TradingView writes share classes with a dot (BRK.B), so a dash
      // always separates the exchange. Unknown exchanges (crypto, FX ...) are not guessed.
      const dash = s.indexOf('-');
      if (dash > 0) return withExchange(s.slice(dash + 1), s.slice(0, dash));
      return withExchange(s, '');
    },
  },
  {
    name: 'MarketWatch',
    match: (u) => hostIs(u.hostname, 'marketwatch.com'),
    detect(u) {
      // /investing/stock/aapl, /investing/fund/spy, /investing/stock/hsba?countrycode=uk
      const m = u.pathname.match(/^\/investing\/(?:stock|fund)\/([^/]+)/i);
      if (!m) return null;
      const cc = (u.searchParams.get('countrycode') || u.searchParams.get('countryCode') || 'us').toLowerCase();
      if (!(cc in MARKETWATCH_COUNTRY)) return null;
      const base = decodeURIComponent(m[1]).toUpperCase();
      return normalizeSymbol(cc === 'us' ? usClass(base) : `${base}${MARKETWATCH_COUNTRY[cc]}`);
    },
  },
  {
    name: 'Seeking Alpha',
    match: (u) => hostIs(u.hostname, 'seekingalpha.com'),
    detect(u) {
      const m = u.pathname.match(/^\/symbol\/([^/]+)/i);
      return m ? normalizeSymbol(usClass(decodeURIComponent(m[1]).toUpperCase())) : null;
    },
  },
  {
    name: 'Finviz',
    match: (u) => hostIs(u.hostname, 'finviz.com'),
    detect(u) {
      if (!/^\/quote\.ashx$/i.test(u.pathname)) return null;
      const t = u.searchParams.get('t');
      return t ? normalizeSymbol(usClass(t.toUpperCase())) : null;
    },
  },
  {
    name: 'CNBC',
    match: (u) => hostIs(u.hostname, 'cnbc.com'),
    detect(u) {
      const m = u.pathname.match(/^\/quotes\/([^/]+)/i);
      if (!m) return null;
      const s = decodeURIComponent(m[1]).toUpperCase();
      // International listings are SYMBOL-CC (VOD-GB); only well-known countries are mapped.
      const intl = s.match(/^(.+)-([A-Z]{2})$/);
      if (intl) {
        const suffix = { GB: '.L', JP: '.T', HK: '.HK', CA: '.TO', AU: '.AX', DE: '.DE', FR: '.PA' }[intl[2]];
        return suffix ? normalizeSymbol(`${intl[1]}${suffix}`) : null;
      }
      return normalizeSymbol(usClass(s));
    },
  },
  {
    name: 'Nasdaq',
    match: (u) => hostIs(u.hostname, 'nasdaq.com'),
    detect(u) {
      const m = u.pathname.match(/^\/market-activity\/(?:stocks|etf|funds-and-etfs)\/([^/]+)/i);
      return m ? normalizeSymbol(usClass(decodeURIComponent(m[1]).toUpperCase())) : null;
    },
  },
  {
    name: 'NSE India',
    match: (u) => hostIs(u.hostname, 'nseindia.com'),
    detect(u) {
      if (!/^\/get-quotes\/equity\/?$/i.test(u.pathname)) return null;
      const s = u.searchParams.get('symbol');
      return s ? normalizeSymbol(`${s.toUpperCase()}.NS`) : null;
    },
  },
  {
    name: 'BSE India',
    match: (u) => hostIs(u.hostname, 'bseindia.com'),
    detect(u) {
      // /stock-share-price/<company-slug>/<symbol>/<6-digit scrip code>/ — only this exact
      // shape identifies one listing; anything else is ambiguous and not guessed.
      const m = u.pathname.match(/^\/stock-share-price\/[^/]+\/([^/]+)\/(\d{6})\/?/i);
      if (!m) return null;
      const sym = decodeURIComponent(m[1]).toUpperCase();
      return normalizeSymbol(`${sym}.BO`) || normalizeSymbol(`${m[2]}.BO`);
    },
  },
];

/**
 * Ticker for a page URL: { symbol, source } or null. Only http(s) URLs of the
 * supported sites are recognised; everything else returns null.
 */
export function detectTicker(pageUrl) {
  if (!pageUrl) return null;
  let u;
  try {
    u = new URL(pageUrl);
  } catch {
    return null;
  }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
  u.hostname = u.hostname.toLowerCase();
  for (const d of DETECTORS) {
    if (!d.match(u)) continue;
    let symbol = null;
    try {
      symbol = d.detect(u);
    } catch {
      symbol = null;
    }
    return symbol ? { symbol, source: d.name } : null;
  }
  return null;
}

export const SUPPORTED_SITES = DETECTORS.map((d) => d.name);
