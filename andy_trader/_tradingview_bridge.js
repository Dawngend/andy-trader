'use strict';

// One-shot bridge around the workshop-authorized TradingView public API client.
// This file never reads or passes TradingView cookies, sessions, or signatures.

const path = require('path');

const [, , apiRoot, symbol, timeframe, rawBars, rawTimeoutMs] = process.argv;
const barsRequested = Number.parseInt(rawBars, 10);
const timeoutMs = Number.parseInt(rawTimeoutMs, 10);

if (!apiRoot || !symbol || !timeframe || !Number.isInteger(barsRequested)
  || !Number.isInteger(timeoutMs)) {
  process.stderr.write('Invalid bridge arguments\n');
  process.exit(2);
}

let TradingView;
try {
  TradingView = require(path.join(apiRoot, 'main.js'));
} catch (error) {
  process.stderr.write(`Could not load TradingView-API: ${error.message}\n`);
  process.exit(2);
}

const client = new TradingView.Client();
const chart = new client.Session.Chart();
let finished = false;

async function finish(code, value) {
  if (finished) return;
  finished = true;
  clearTimeout(deadline);
  try { chart.delete(); } catch (_) { /* best-effort cleanup */ }
  try { await client.end(); } catch (_) { /* best-effort cleanup */ }

  if (code === 0) process.stdout.write(`${JSON.stringify(value)}\n`);
  else process.stderr.write(`${String(value)}\n`);
  process.exitCode = code;
}

chart.onError((...errors) => {
  const message = errors.map((value) => (
    value instanceof Error ? value.message : String(value)
  )).join(' ');
  finish(1, `Chart error: ${message}`);
});

chart.onUpdate(() => {
  const periods = chart.periods.slice(0, barsRequested);
  if (periods.length === 0) return;

  const bars = periods.map((period, index) => ({
    time: period.time,
    open: period.open,
    high: period.max,
    low: period.min,
    close: period.close,
    volume: period.volume,
    potentially_open: index === 0,
  }));
  finish(0, {
    source: 'tradingview',
    symbol,
    timeframe,
    fetched_at: new Date().toISOString(),
    market: {
      description: chart.infos.description || null,
      currency: chart.infos.currency_id || null,
    },
    bars,
  });
});

const deadline = setTimeout(() => {
  finish(1, `No chart data received within ${timeoutMs}ms`);
}, timeoutMs);

chart.setMarket(symbol, { timeframe, range: barsRequested });
