# AI Trading Bot: Companion (Chrome extension)

A Manifest V3 extension that keeps an eye on your AI Trading Bot from the browser toolbar:

- **Toolbar badge**: the bot's state at a glance, refreshed every 1 to 15 minutes.
- **Notifications** when something needs you: the bot halts, a panic flatten is requested or completes, the bot loop stops cycling, cycles keep failing, or prices for held positions go missing. You get one notification per change, not one per check.
- **Popup**: state, mode, equity, drawdown, daily P&L, last cycle and open positions, plus Start, Pause, Open dashboard and a **panic flatten** button that needs a second, explicit confirmation.
- **Ticker panel**: the bot's analysis (signal, scores, regime, entry/stop/target, reasons) and research fundamentals (composite score, fair value and upside, Piotroski, Altman zone, next earnings). The ticker is detected from the page you are on, or you type it.

It works with the **AI Trading Bot desktop app** (the server runs on this computer) and with a **self-hosted server** such as the Docker deployment, over https.

It is plain HTML, CSS and ES modules. There is no build step, no bundler, no remote code and no content scripts.

## Install (unpacked)

1. Open `chrome://extensions` and switch on **Developer mode** (top right).
2. Click **Load unpacked** and select this `extension/` folder (or the folder you extracted the release ZIP into).
3. The settings page opens. Pin the extension (puzzle-piece menu, then the pin) so the badge stays visible.

It needs Chrome 116 or later. Other Chromium browsers (Edge, Brave) work the same way.

## Connect

### Desktop app

The desktop app serves everything on `http://127.0.0.1:47821`. If that port is busy, it takes the next free one up to 47841 (and tells you so). The app's tray menu shows the exact URL.

**Pair the extension with the app first.** Any program on the computer, including one run by another user, can listen on those ports and pose as the app. So the extension sends no password and no session token to a server on this computer until that server has proved it knows the app's pairing code:

1. In the app's tray menu, choose **Copy Pairing Code for the Chrome Extension**.
2. Paste it into **Pairing code** in the extension settings and press **Save code**.

The extension then sends a random challenge to `GET /api/desktop/pair` and checks the answer (an HMAC-SHA256 of the challenge and the server's own address, keyed with the code) before every sign-in, and before authenticated requests at least once a minute. A server that fails the check gets nothing; the badge shows `!!` and the popup says the server is not your app. The check is always needed for the app's ports (47821 to 47841), and for every server on this computer once a code is entered (the app can be moved with `TRADEBOT_PORT`). Use `http://127.0.0.1:<port>`, not `localhost`: the app proves its own address.

**Find the desktop app** in the settings asks each of those ports for `/health` and, with a pairing code entered, only picks the server that passes the check. Then press **Save**.

Sign in with the account you created in the desktop app's window. Its first account is the administrator, and only the app window can create it.

### Self-hosted server

Enter the server's `https://` address and press **Save**. Chrome asks you to allow access to that one site. The extension only holds the permission for the server you configured. Changing servers signs you out of the old one and gives back its permission.

Plain `http://` is accepted only for `127.0.0.1` and `localhost`. For any other host, passwords and tokens would cross the network unencrypted, so the extension refuses.

### Accounts

Bot status, alerts and controls need an **administrator** account: the bot's book is private to administrators on the server. With any other account, the extension shows the connection state and the ticker panel works.

## The badge

| Badge  | Meaning |
|--------|---------|
| `RUN`  | Bot running: new entries allowed |
| `OFF`  | Bot paused: no new entries, open positions keep their stops |
| `HALT` | Halted by the circuit breaker or a manual flatten. Reset it in the dashboard |
| `FLAT` | Panic flatten requested: positions are being closed |
| `DATA` | Data fault: no trustworthy price for a held position |
| `ERR`  | Two or more trading cycles in a row failed |
| `LATE` | No recent cycle while the bot has work to do (enabled, positions open or flatten pending) |
| `DOWN` | Server not reachable (is the desktop app running?) |
| `?`    | Not signed in |
| `PAIR` | Desktop app not paired yet: enter the pairing code in the settings |
| `!!`   | The server on this computer could not prove it is your desktop app: nothing is sent to it |
| `!`    | Chrome has not granted access to the configured https server |
| none   | Signed in without administrator rights |

Hover over the icon for the details: halt reason, warnings, equity, drawdown, last cycle and server.

## Permissions, and why

| Permission | Used for |
|------------|----------|
| `storage` | Settings, sign-in tokens and the last state seen (to notify once per change) |
| `alarms` | Checking the bot every 1 to 15 minutes, even when no extension page is open |
| `notifications` | Bot alerts |
| `activeTab` | Reading the **address** of the current tab when you open the popup, to recognise a ticker |
| `http://127.0.0.1/*`, `http://localhost/*` | Talking to the desktop app on this computer |
| optional `https://*/*` | Requested at run time for **your** server's host only, when you save an https server |

## Privacy

- The extension never reads the content of web pages and has no content scripts.
- Only when you open the popup does it look at the address of the active tab, and only that tab. It does this to recognise a ticker on Yahoo Finance, Google Finance, TradingView, MarketWatch, Seeking Alpha, Finviz, CNBC, Nasdaq.com, NSE India or BSE India. The address is matched locally; only the ticker symbol is sent, and only to your server.
- It talks only to the server you configured. There are no analytics, no third-party requests and no remote code.
- Your password goes to your server's `/auth/login` once and is never stored. The extension keeps the refresh token in its local storage and the short-lived access token in session storage, which lives in memory and is cleared when the browser closes. Both are bound to the server that issued them. **Sign out** deletes both.

## Ticker detection

| Site | URL shape | Symbol |
|------|-----------|--------|
| Yahoo Finance | `/quote/AAPL`, `/quote/%5EGSPC`, `/chart/TSLA` | `AAPL`, `^GSPC`, `TSLA` |
| Google Finance | `/finance/quote/AAPL:NASDAQ`, `RELIANCE:NSE`, `500325:BOM` | `AAPL`, `RELIANCE.NS`, `500325.BO` |
| TradingView | `/symbols/NASDAQ-AAPL/`, `/chart/?symbol=NSE:INFY` | `AAPL`, `INFY.NS` |
| MarketWatch | `/investing/stock/aapl`, `?countrycode=uk` | `AAPL`, `HSBA.L` |
| Seeking Alpha | `/symbol/BRK.B` | `BRK-B` |
| Finviz | `quote.ashx?t=AAPL` | `AAPL` |
| CNBC | `/quotes/AAPL`, `/quotes/VOD-GB` | `AAPL`, `VOD.L` |
| Nasdaq.com | `/market-activity/stocks/aapl` | `AAPL` |
| NSE India | `/get-quotes/equity?symbol=RELIANCE` | `RELIANCE.NS` |
| BSE India | `/stock-share-price/<name>/<symbol>/<code>/` | `RELIANCE.BO` |

Symbols use Yahoo's notation, which the backend uses: US share classes are written `BRK-B`, and exchange suffixes look like `.NS` or `.L`. A symbol must match the backend's rule `^[A-Z0-9^.\-=]{1,20}$`. When the exchange is unknown or ambiguous (a crypto venue, MarketWatch's India pages, CNBC listings from unmapped countries), the extension makes no guess and you type the symbol yourself.

## Development

```
extension/
  manifest.json            MV3 manifest
  background.js            service worker: alarms, badge, notifications (logic in lib/poller.js)
  popup.html/.css/.js      toolbar popup
  options.html/.css/.js    settings page
  base.css                 shared dark theme (the dashboard's palette)
  lib/                     pure ES modules, unit-tested:
    api.js                   REST client, token storage, single-flight refresh on 401
    tickers.js               ticker detection from URLs
    status.js                state → badge text, colour and tooltip
    alerts.js                notification transitions
    poller.js                one monitoring pass (snapshot → badge → notifications)
    server.js                server URL rules, host permissions, desktop app discovery
    settings.js, format.js, dom.js
  icons/                   16/32/48/128 px PNG, rendered from frontend/public/favicon.svg
  tests/                   node --test unit tests (fake chrome.* and fetch)
  scripts/pack.mjs         builds the store ZIP (no dependencies)
  scripts/render_icons.py  re-renders the icons (Playwright)
```

Unit tests (Node 20 or later, no `npm install` needed):

```
cd extension && npm test
```

End-to-end tests (Python 3.11 with the backend's requirements, Playwright for Python and a Chromium build). They start the real desktop sidecar from source, load the packed extension into Chromium and drive the options page and the popup:

```
pip install playwright && python -m playwright install chromium
PYTHONPATH=. xvfb-run -a python -m pytest -q tests/test_extension_e2e.py      # or without xvfb-run: headless
```

Set `CHROMIUM_EXECUTABLE` to use a specific Chromium build, `EXTENSION_E2E_SCREENSHOTS=<dir>` to save screenshots, or `SKIP_EXTENSION_E2E=1` to skip the tests.

To change the icons, edit `frontend/public/favicon.svg` and run `python extension/scripts/render_icons.py` from the repository root.

## Package and publish

```
node extension/scripts/pack.mjs          # writes extension/dist/ai-trading-bot-extension-<version>.zip
```

The ZIP contains only the runtime files: the manifest, pages, scripts, styles and icons. Before writing it, the script checks that every file the manifest, the pages and the module imports reference is included. Builds are reproducible: the same sources give the same bytes.

To publish on the Chrome Web Store:

1. Bump `version` in `manifest.json` (and `package.json`), then run the tests and `pack.mjs`.
2. In the [Chrome Web Store developer dashboard](https://chrome.google.com/webstore/devconsole), create an item, or open the existing one, and upload the ZIP.
3. Store listing: a description, screenshots (1280×800 or 640×400) and the 128 px icon from `icons/`.
4. Privacy practices:
   - **Single purpose**: monitoring and controlling your own AI Trading Bot server.
   - **Permission justifications**: copy them from the table above.
   - **Remote code**: none.
   - **Data use**: the extension handles authentication information, the tokens for your own server, and reads the active tab's URL locally when the popup opens. Nothing is sold, transferred to third parties or used for anything else.
   - **Privacy policy**: link a policy page that summarises the Privacy section above.
5. Submit for review. The broad optional `https://*/*` permission is requested at run time, one host at a time. Mention this in the justification: reviewers check for it.
