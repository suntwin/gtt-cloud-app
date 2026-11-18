// tv-bridge — lets the Scanner's "TradingView" button switch the symbol in TradingView Desktop.
// Runs on your PC, listens only on 127.0.0.1, and does exactly one thing: set the chart symbol.
// Needs Node 22+ and TradingView Desktop started with remote debugging (the same port the TradingView MCP uses).
const http = require('http')
const { exec } = require('child_process')

const PORT = +process.env.TV_BRIDGE_PORT || 9333
const CDP_PORTS = process.env.TV_CDP_PORT ? [+process.env.TV_CDP_PORT] : [9222, 9223, 9229]
const MATCH = new RegExp(process.env.TV_MATCH || 'tradingview\\.com/chart')
const ORIGINS = [/^https:\/\/([a-z0-9-]+\.)?myswingdesk\.app$/, /^https:\/\/[a-z0-9-]+\.frappe\.cloud$/, /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/]
const SYM = /^([A-Z0-9_]{1,20}:)?[A-Z0-9_.&!-]{1,40}$/i

if (typeof WebSocket === 'undefined') { console.error(`tv-bridge needs Node 22 or newer (you have ${process.version}).`); process.exit(1) }

async function findTarget() {
  for (const p of CDP_PORTS) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${p}/json/list`, { signal: AbortSignal.timeout(1500) })).json()
      const t = list.find((x) => x.type === 'page' && MATCH.test(x.url))
      if (t) return t
    } catch {}
  }
  throw new Error(`TradingView Desktop not reachable on debug port ${CDP_PORTS.join('/')} — is it running with remote debugging?`)
}

async function waitForChart(t) {                       // a freshly opened TradingView needs a few seconds
  for (let i = 0; i < 30; i++) {
    const [r] = await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: "!!(window.TradingViewApi && TradingViewApi.activeChart && TradingViewApi.activeChart())", returnByValue: true } }]).catch(() => [{}])
    if (r?.result?.value) return
    await new Promise((ok) => setTimeout(ok, 1000))
  }
}

function cdp(wsUrl, calls, ms = 8000) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl), out = []
    let id = 0
    const timer = setTimeout(() => { ws.close(); reject(new Error('TradingView did not answer')) }, ms)
    const next = () => {
      if (id === calls.length) { clearTimeout(timer); ws.close(); return resolve(out) }
      ws.send(JSON.stringify({ id: ++id, ...calls[id - 1] }))
    }
    ws.onopen = next
    ws.onerror = () => { clearTimeout(timer); reject(new Error('Could not connect to TradingView')) }
    ws.onmessage = (m) => {
      const r = JSON.parse(m.data)
      if (r.id !== id) return
      if (r.error) { clearTimeout(timer); ws.close(); return reject(new Error(r.error.message)) }
      out.push(r.result); next()
    }
  })
}

let launching = null
function launchTV() {                                   // start TradingView in debug mode (never restarts an open one)
  if (process.platform !== 'win32') return Promise.resolve()
  launching ||= new Promise((ok) => exec(`powershell -NoProfile -ExecutionPolicy Bypass -File "${__dirname}\\start-tradingview.ps1" -NoKill`,
    { windowsHide: true, timeout: 120000 }, (e, out) => { console.log((out || '').trim()); launching = null; ok() }))
  return launching
}

// ── AVWAP read-back: after a switch, read the DAILY pane's bars and anchor a VWAP at the highest high of the last 100 bars
// (hlc3 × volume). Same rule as the chart's AVWAP-from-high. Returns null when the pane isn't loaded in time.
const DAILY = `(function(){ var A = TradingViewApi, n = A.chartsCount ? A.chartsCount() : 1
  for (var i = 0; i < n; i++) { var c = A.chart(i); if (c.resolution() === '1D' || c.resolution() === 'D') return c }
  return null })()`
const SIG = `(function(){ var c = ${DAILY}; if (!c) return ''; var b = c._chartWidget.model().mainSeries().bars();
  return c.symbol() + '|' + b.size() + '|' + JSON.stringify(b.valueAt(b.lastIndex())) })()`
const CALC = (want, prev) => `(function(){ var c = ${DAILY}; if (!c) return { wait: false, none: 'no daily pane' }
  var ms = c._chartWidget.model().mainSeries(), b = ms.bars(), sym = c.symbol(), prev = ${JSON.stringify(prev)}
  var data = b.size() + '|' + JSON.stringify(b.valueAt(b.lastIndex()))
  var base = function (s) { return String(s).split(':').pop().toUpperCase() }, want = base(${JSON.stringify(want)})
  var stale = prev && base(prev.split('|')[0]) !== want && prev.slice(prev.indexOf('|') + 1) === data   // still the old ticker's bars
  if (base(sym) !== want || b.size() < 30 || stale || (ms.isLoading && ms.isLoading()) || (ms.isCompleted && !ms.isCompleted())) return { wait: true }
  var bars = [], end = b.lastIndex(), st = Math.max(b.firstIndex(), end - 99)
  for (var i = st; i <= end; i++) { var v = b.valueAt(i); if (v) bars.push(v) }
  var hi = -1, k = -1; bars.forEach(function (v, j) { if (v[2] >= hi) { hi = v[2]; k = j } })
  var pv = 0, vv = 0; for (var j = k; j < bars.length; j++) { var x = bars[j], w = x[5] || 0; pv += (x[2] + x[3] + x[4]) / 3 * w; vv += w }
  if (!vv) return { wait: false, none: 'no volume' }
  var last = bars[bars.length - 1], r = bars.slice(-20), adr = r.reduce(function (t, x) { return t + (x[2] / x[3] - 1) }, 0) / r.length * 100
  var av = pv / vv, d = function (t) { return new Date(t * 1000).toISOString().slice(0, 10) }
  return { symbol: sym, avwap: +av.toFixed(4), anchor_date: d(bars[k][0]), anchor_high: hi, close: last[4], bar_date: d(last[0]),
           adr_pct: +adr.toFixed(3), dist_adr: +((last[4] - av) / (last[4] * adr / 100)).toFixed(3) } })()`

async function readAvwap(t, want, prev) {
  for (let i = 0; i < 20; i++) {                        // up to ~6 s for the daily pane to load the new symbol
    const [r] = await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: CALC(want, prev), returnByValue: true } }]).catch(() => [{}])
    const v = r?.result?.value
    if (v && !v.wait) return v.none ? null : v
    await new Promise((ok) => setTimeout(ok, 300))
  }
  return null
}

// ── NSE circuit lines: when the Scanner opens a stock it passes the stock's price band (the Circuit % column). The base is
// the last finished session's close (Yahoo daily bars; during the session today's bar is left out), and the upper / lower
// circuit are shown as a small label at the top right of the chart. It is temporary (not saved in the layout) and replaced on the next open.
async function circuitBase(sym) {
  const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(yahooSym(String(sym).split(':').pop(), 'NSE'))}?range=10d&interval=1d`,
    { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(10000) })
  if (!r.ok) return null
  const res = (await r.json())?.chart?.result?.[0]
  const q = res?.indicators?.quote?.[0], ts = res?.timestamp
  if (!q || !ts) return null
  let bars = ts.map((t, i) => [t, q.close[i]]).filter((b) => b[1] != null)
  const reg = res.meta?.currentTradingPeriod?.regular, nowS = Date.now() / 1000
  if (reg && nowS < reg.end && bars.length && bars[bars.length - 1][0] >= reg.start) bars = bars.slice(0, -1)   // session not finished
  return bars.length ? bars[bars.length - 1][1] : null
}
async function drawCircuit(t, sym, band) {
  const base = await circuitBase(sym).catch(() => null)
  const arg = base ? { up: +(base * (1 + band / 100)).toFixed(2), lo: +(base * (1 - band / 100)).toFixed(2), band, base: +base.toFixed(2) } : null
  const expr = `(async (A) => { const c = TradingViewApi.activeChart()
    for (const id of (window.__sdCircuit || [])) { try { c.removeEntity(id) } catch (e) {} }
    window.__sdCircuit = []
    if (!A) return 'cleared'
    const col = A.band <= 5 ? '#ef5350' : A.band <= 10 ? '#ffa726' : '#bcaaa4'
    const f = (x) => Number(x).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    // a small label pinned to the top right of the chart (under the Market box), not lines across the price
    window.__sdCircuit = [await Promise.resolve(c.createAnchoredShape({ x: 0.79, y: 0.155 }, { shape: 'anchored_text',
      text: 'Circuit ' + A.band + '%  ·  UC ' + f(A.up) + '  ·  LC ' + f(A.lo), lock: true, disableSave: true, disableUndo: true,
      overrides: { color: col, fontsize: 12, bold: true, fillBackground: true, backgroundColor: 'rgba(20,20,20,0.85)', drawBorder: true, borderColor: col } }))]
    return 'drawn' })(${JSON.stringify(arg)})`
  await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: expr, awaitPromise: true, returnByValue: true } }]).catch(() => {})
}
async function setSymbol(sym, focus = true, withAvwap = false, band = 0) {
  let t
  try { t = await findTarget() } catch { await launchTV(); t = await findTarget() }
  await waitForChart(t)
  let prev = ''
  if (withAvwap) prev = (await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: SIG, returnByValue: true } }]).catch(() => [{}]))[0]?.result?.value || ''
  const expr = `new Promise((ok, no) => { try { const c = TradingViewApi.activeChart();
    const done = () => ok(c.symbol()); c.setSymbol(${JSON.stringify(sym)}, done); setTimeout(done, 4000) } catch (e) { no(String(e)) } })`
  const calls = [{ method: 'Runtime.evaluate', params: { expression: expr, awaitPromise: true, returnByValue: true } }]
  if (focus) calls.unshift({ method: 'Page.bringToFront' })
  const r = (await cdp(t.webSocketDebuggerUrl, calls)).at(-1)
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.value || 'setSymbol failed')
  if (focus && process.platform === 'win32')  // raise the TradingView window (fixed command, no user input)
    exec(`powershell -NoProfile -Command "(New-Object -ComObject WScript.Shell).AppActivate('TradingView') | Out-Null"`, { windowsHide: true })
  const symbol = r.result.value
  // circuit lines for NSE stocks opened from the Scanner (cleared for anything else); not awaited, so the chart opens at once
  drawCircuit(t, sym, /^NSE:/i.test(sym) && band > 0 ? band : 0).catch(() => {})
  return { symbol, avwap: withAvwap ? await readAvwap(t, sym, prev) : undefined }
}

// ── Watchlists: the Leading / Mediocre / Lagging / Inbox lists (prefix SD = NSE, US = US) read from your TradingView account,
// so the Scanner can show which list each stock is on. Read-only.
const LIST_PREFIX = /^(SD|US|CR)$/
async function readLists(prefix) {
  let t
  try { t = await findTarget() } catch { return null }
  const expr = `fetch('/api/v1/symbols_list/custom/', { credentials: 'include' }).then((r) => r.json()).then((all) => {
    const out = {}
    for (const l of all) { const m = String(l.name || '').match(/^${prefix} (Leading|Mediocre|Lagging|Inbox)$/); if (m) out[m[1]] = (l.symbols || []).filter((s) => !String(s).startsWith('###')) }
    return out })`
  const [r] = await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: expr, awaitPromise: true, returnByValue: true } }])
  return r?.result?.value || null
}

// ── Edit the tier lists on the fly (from the scanner, the phone or an ntfy button): move a ticker to Leading / Mediocre /
// Lagging / Inbox, or take it off all four. Runs inside the TradingView page, like the watchlist tool (same list API).
const LIST_TO = /^(Leading|Mediocre|Lagging|Inbox|Remove)$/
async function pageListOp({ P, S, TO }) {        // stringified and run in TradingView
  const bare = (x) => String(x).toUpperCase().split(':').pop()
  const mem = (x) => !String(x).startsWith('###')
  const r = await fetch('/api/v1/symbols_list/custom/', { credentials: 'include' })
  if (!r.ok) return { ok: false, error: 'lists HTTP ' + r.status }
  const mine = {}
  for (const l of await r.json()) { const m = String(l.name || '').match(new RegExp('^' + P + ' (Leading|Mediocre|Lagging|Inbox)$')); if (m) mine[m[1]] = l }
  let full = S.includes(':') ? S.toUpperCase() : null
  const was = []
  for (const [tier, l] of Object.entries(mine)) {
    const hit = (l.symbols || []).find((x) => mem(x) && bare(x) === bare(S))
    if (hit) { was.push(tier); full = full || hit }
  }
  if (TO !== 'Remove' && !full) {
    if (P === 'CR') return { ok: false, error: 'crypto needs the full TradingView symbol (e.g. BINANCE:BTCUSDT)' }
    if (P === 'SD') full = 'NSE:' + bare(S)
    else {
      try {
        const j = await (await fetch('https://symbol-search.tradingview.com/symbol_search/v3/?text=' + encodeURIComponent(bare(S)) +
          '&hl=0&exchange=&lang=en&search_type=stocks&domain=production&sort_by_country=US')).json()
        const h = (j.symbols || []).find((x) => x.symbol === bare(S) && x.country === 'US' && ['NASDAQ', 'NYSE', 'AMEX', 'NYSE Arca'].includes(x.exchange)
          && !['BATS', 'CBOE'].includes(x.prefix || ''))
        if (h) full = (h.exchange === 'NYSE Arca' ? 'AMEX' : h.exchange) + ':' + bare(S)
      } catch {}
      if (!full) return { ok: false, error: 'could not find ' + bare(S) + ' on a US exchange' }
    }
  }
  const save = async (url, body) => (await fetch(url, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).status
  for (const [tier, l] of Object.entries(mine)) {
    if (tier === TO || !was.includes(tier)) continue
    const st = await save('/api/v1/symbols_list/custom/' + l.id + '/replace/?unsafe=true', (l.symbols || []).filter((x) => !mem(x) || bare(x) !== bare(S)))
    if (st !== 200 && st !== 201) return { ok: false, error: P + ' ' + tier + ': HTTP ' + st }
  }
  if (TO !== 'Remove' && !was.includes(TO)) {
    const l = mine[TO]
    const st = l ? await save('/api/v1/symbols_list/custom/' + l.id + '/replace/?unsafe=true', [...(l.symbols || []), full])
                 : await save('/api/v1/symbols_list/custom/', { name: P + ' ' + TO, symbols: [full] })
    if (st !== 200 && st !== 201) return { ok: false, error: P + ' ' + TO + ': HTTP ' + st }
  }
  return { ok: true, sym: full || bare(S), was, to: TO }
}
// Add several symbols to one list of a prefix (skips any already on one of the prefix's four lists) — one save.
async function pageListAdd({ P, TO, SYMS }) {
  const bare = (x) => String(x).toUpperCase().split(':').pop()
  const r = await fetch('/api/v1/symbols_list/custom/', { credentials: 'include' })
  if (!r.ok) return { ok: false, error: 'lists HTTP ' + r.status }
  const mine = {}
  for (const l of await r.json()) { const m = String(l.name || '').match(new RegExp('^' + P + ' (Leading|Mediocre|Lagging|Inbox)$')); if (m) mine[m[1]] = l }
  const have = new Set(Object.values(mine).flatMap((l) => (l.symbols || []).filter((x) => !String(x).startsWith('###')).map(bare)))
  const add = [...new Set(SYMS.map((x) => String(x).toUpperCase()))].filter((x) => x.includes(':') && !have.has(bare(x)))
  if (!add.length) return { ok: true, added: [] }
  const l = mine[TO]
  const body = l ? [...(l.symbols || []), ...add] : { name: P + ' ' + TO, symbols: add }
  const st = (await fetch(l ? '/api/v1/symbols_list/custom/' + l.id + '/replace/?unsafe=true' : '/api/v1/symbols_list/custom/',
    { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).status
  return st === 200 || st === 201 ? { ok: true, added: add } : { ok: false, error: P + ' ' + TO + ': HTTP ' + st }
}
async function listAdd(prefix, to, syms) {
  let t
  try { t = await findTarget() } catch { return null }
  const expr = `(${pageListAdd.toString()})(${JSON.stringify({ P: prefix, TO: to, SYMS: syms })})`
  const [r] = await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: expr, awaitPromise: true, returnByValue: true } }], 20000)
  return r?.result?.value || null
}
async function listOp(prefix, sym, to) {
  let t
  try { t = await findTarget() } catch { return null }
  const expr = `(${pageListOp.toString()})(${JSON.stringify({ P: prefix, S: sym, TO: to })})`
  const [r] = await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: expr, awaitPromise: true, returnByValue: true } }], 20000)
  return r?.result?.value || null
}

// ── Crypto AVWAP watch: daily bars straight from the exchange (public APIs, no key), for "BYBIT:ACSUSDT", "BINANCE:X.P",
// "OKX:BTCUSDT"…; other exchanges fall back to the same coin on Binance, then Bybit, then OKX. Days are UTC (as on TradingView).
const CSYM = /^[A-Z0-9_]{2,20}:[A-Z0-9]{2,30}(\.P)?$/
async function jget(url) { const r = await fetch(url, { signal: AbortSignal.timeout(10000) }); if (!r.ok) throw new Error(url + ' ' + r.status); return r.json() }
const KL = {                                                        // → [[t(sec), o, h, l, c, v], …] oldest first
  BINANCE: async (pair, perp, n = 102) => (await jget(`https://${perp ? 'fapi' : 'api'}.binance.com/${perp ? 'fapi/v1' : 'api/v3'}/klines?symbol=${pair}&interval=1d&limit=${n}`))
    .map((k) => [k[0] / 1000, +k[1], +k[2], +k[3], +k[4], +k[5]]),
  BYBIT: async (pair, perp, n = 102) => ((await jget(`https://api.bybit.com/v5/market/kline?category=${perp ? 'linear' : 'spot'}&symbol=${pair}&interval=D&limit=${n}`)).result?.list || [])
    .map((k) => [+k[0] / 1000, +k[1], +k[2], +k[3], +k[4], +k[5]]).reverse(),
  OKX: async (pair, perp, n = 100) => {
    const q = pair.replace(/(USDT|USDC|USD)$/, '-$1') + (perp ? '-SWAP' : '')
    return ((await jget(`https://www.okx.com/api/v5/market/candles?instId=${q}&bar=1Dutc&limit=${Math.min(n, 100)}`)).data || [])
      .map((k) => [+k[0] / 1000, +k[1], +k[2], +k[3], +k[4], +k[5]]).reverse()
  },
}
async function cryptoBars(tvSym, n = 102) {
  const [ex, rest] = tvSym.split(':'), perp = rest.endsWith('.P'), pair = rest.replace(/\.P$/, '')
  const usdt = pair.replace(/(USDT|USDC|USD|PERP)$/, '') + 'USDT'
  const tries = [[ex, pair, perp], ['BINANCE', usdt, false], ['BINANCE', usdt, true], ['BYBIT', usdt, false], ['BYBIT', usdt, true], ['OKX', usdt, false]]
  for (const [e, p, pp] of tries) {
    if (!KL[e]) continue
    try { const b = await KL[e](p, pp, n); if (b.length >= 25) return { bars: b, src: `${e}:${p}${pp ? '.P' : ''}` } } catch {}
  }
  return null
}
function cryptoWatchRow(bars) {
  const today = Math.floor(Date.now() / 864e5) * 86400
  const live = bars[bars.length - 1][0] >= today ? bars[bars.length - 1] : null     // today's unfinished UTC bar
  const done = live ? bars.slice(0, -1) : bars
  const look = done.slice(-100)
  let k = 0
  for (let i = 0; i < look.length; i++) if (look[i][2] >= look[k][2]) k = i                 // highest high (most recent if tied)
  let pv = 0, vv = 0
  for (let i = k; i < look.length; i++) { const b = look[i]; pv += (b[2] + b[3] + b[4]) / 3 * b[5]; vv += b[5] }
  if (!vv) return null
  const r20 = done.slice(-20), d = (t) => new Date(t * 1000).toISOString().slice(0, 10)
  const adr = r20.reduce((t, b) => t + (b[2] / b[3] - 1), 0) / r20.length * 100
  const last = live || done[done.length - 1]
  return { avwap: +(pv / vv).toPrecision(8), anchor_date: d(look[k][0]), anchor_high: look[k][2], adr_pct: +adr.toFixed(3),
           prev: done[done.length - 1][4], close: last[4], high: last[2], low: last[3], volume: live ? live[5] : 0,
           avg_vol: r20.reduce((t, b) => t + b[5], 0) / r20.length, frac: Math.max(0.03, (Date.now() / 1000 - today) / 86400), live: !!live }
}
async function cryptoWatch(syms) {
  const out = {}
  for (let i = 0; i < syms.length; i += 6) {
    const part = await Promise.all(syms.slice(i, i + 6).map(async (s) => {
      try { const r = await cryptoBars(s); const row = r && cryptoWatchRow(r.bars); return [s, row && { ...row, src: r.src }] } catch { return [s, null] }
    }))
    for (const [s, v] of part) if (v) out[s] = v
  }
  return out
}

// ── Sort the CR lists like `watchlists.bat` sorts SD / US (Martin's EMA stacking), from the exchange's daily bars:
// Leading = above the 10 and 20 EMA, 10 > 20, above the 50 EMA · Lagging = below the 50 EMA and the 150 SMA · else Mediocre.
// Inbox + the three lists are re-filed; inside a list the coins nearest their 10 EMA (in ADRs) come first, split into
// ≥ $50M and $1–50M a day. Coins under $1M a day are dropped; coins with no data stay where they were.
const ema = (xs, n) => { const k = 2 / (n + 1); let e = xs[0]; for (let i = 1; i < xs.length; i++) e = xs[i] * k + e * (1 - k); return e }
function crTier(bars) {
  const c = bars.map((b) => b[4]), last = c[c.length - 1]
  if (c.length < 50) return null
  const e10 = ema(c, 10), e20 = ema(c, 20), e50 = ema(c, 50), s150 = c.length >= 150 ? c.slice(-150).reduce((a, b) => a + b, 0) / 150 : null
  const r20 = bars.slice(-20), adr = r20.reduce((t, b) => t + (b[2] / b[3] - 1), 0) / r20.length * 100
  const liq = bars.slice(-30).reduce((t, b) => t + b[5] * b[4], 0) / Math.min(30, bars.length) / 1e6
  const tier = last > e10 && last > e20 && e10 > e20 && last > e50 ? 'Leading' : last < e50 && (s150 === null || last < s150) ? 'Lagging' : 'Mediocre'
  return { tier, liq: +liq.toFixed(1), d10: adr ? (last / e10 - 1) * 100 / adr : 0 }
}
async function pageLists({ P }) {
  const r = await fetch('/api/v1/symbols_list/custom/', { credentials: 'include' })
  if (!r.ok) return null
  const out = {}
  for (const l of await r.json()) { const m = String(l.name || '').match(new RegExp('^' + P + ' (Leading|Mediocre|Lagging|Inbox)$')); if (m) out[m[1]] = { id: l.id, symbols: l.symbols || [] } }
  return out
}
async function pageWriteLists({ P, LISTS }) {
  const r = await fetch('/api/v1/symbols_list/custom/', { credentials: 'include' })
  const have = {}
  for (const l of await r.json()) have[l.name] = l.id
  const bad = []
  for (const [t, syms] of Object.entries(LISTS)) {
    const id = have[P + ' ' + t]
    const st = (await fetch(id ? '/api/v1/symbols_list/custom/' + id + '/replace/?unsafe=true' : '/api/v1/symbols_list/custom/',
      { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(id ? syms : { name: P + ' ' + t, symbols: syms }) })).status
    if (st !== 200 && st !== 201) bad.push(t + ' ' + st)
  }
  return { ok: !bad.length, error: bad.join(', ') }
}
async function inPage(fn, arg, ms = 20000) {
  const t = await findTarget()
  const [r] = await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: `(${fn.toString()})(${JSON.stringify(arg)})`, awaitPromise: true, returnByValue: true } }], ms)
  return r?.result?.value
}
async function crSort(write) {
  const lists = await inPage(pageLists, { P: 'CR' })
  if (!lists) return { ok: false, error: 'could not read the CR lists' }
  const mem = (t) => (lists[t]?.symbols || []).filter((x) => !String(x).startsWith('###')).map((x) => String(x).toUpperCase())
  const cur = {}
  for (const t of ['Leading', 'Mediocre', 'Lagging']) for (const s of mem(t)) cur[s] = t
  const inbox = mem('Inbox'), syms = [...new Set([...Object.keys(cur), ...inbox])]
  if (!syms.length) return { ok: true, empty: true }
  const rows = {}
  for (let i = 0; i < syms.length; i += 6) {
    const part = await Promise.all(syms.slice(i, i + 6).map(async (s) => { try { const b = await cryptoBars(s, 200); return [s, b && crTier(b.bars)] } catch { return [s, null] } }))
    for (const [s, v] of part) if (v) rows[s] = v
  }
  const nodata = syms.filter((s) => !rows[s]), thin = syms.filter((s) => rows[s] && rows[s].liq < 1)
  const now = {}
  for (const s of syms) if (rows[s] && rows[s].liq >= 1) now[s] = rows[s].tier
  const rank = { Leading: 0, Mediocre: 1, Lagging: 2 }, up = [], down = [], fresh = []
  for (const [s, t] of Object.entries(now)) { const p = cur[s]; if (!p) fresh.push([s, t]); else if (rank[t] < rank[p]) up.push([s, p, t]); else if (rank[t] > rank[p]) down.push([s, p, t]) }
  const out = {}
  for (const t of ['Leading', 'Mediocre', 'Lagging']) {
    const part = Object.keys(now).filter((s) => now[s] === t).map((s) => ({ s, ...rows[s] }))
    const sec = []
    for (const [title, keep] of [['###LIQUID ≥$50M', (r) => r.liq >= 50], ['###$1-50M', (r) => r.liq < 50]]) {
      const p = part.filter(keep).sort((a, b) => Math.abs(a.d10) - Math.abs(b.d10))
      if (p.length) sec.push(title, ...p.map((r) => r.s))
    }
    out[t] = [...sec, ...nodata.filter((s) => cur[s] === t)]
  }
  out.Inbox = nodata.filter((s) => !cur[s])
  if (write) { const w = await inPage(pageWriteLists, { P: 'CR', LISTS: out }); if (!w?.ok) return { ok: false, error: w?.error || 'TradingView did not save' } }
  const short = (x) => x.split(':').pop()
  return { ok: true, counts: Object.fromEntries(['Leading', 'Mediocre', 'Lagging'].map((t) => [t, Object.values(now).filter((v) => v === t).length])),
           up: up.map(([s, a, b]) => `${short(s)} (${a}→${b})`), down: down.map(([s, a, b]) => `${short(s)} (${a}→${b})`),
           new: fresh.map(([s, t]) => `${short(s)} (→${t})`), thin: thin.map(short), nodata: nodata.map(short), written: !!write }
}

// ── Crypto: MarketInOut gives "ACS.CR"; TradingView needs an exchange pair. Look each coin up with TradingView's own
// symbol search (from inside the TradingView page) and pick the best market: perpetuals on the big exchanges first,
// then spot. Read-only; the Scanner saves the answers so each coin is looked up once.
const CR_BASE = /^[A-Z0-9]{1,20}$/
async function resolveCrypto(bases) {
  let t
  try { t = await findTarget() } catch { return null }
  const expr = `(async () => {
    const PREF = ['BINANCE:P', 'BYBIT:P', 'OKX:P', 'BITGET:P', 'BINANCE:S', 'BYBIT:S', 'OKX:S', 'COINBASE:S', 'KUCOIN:S', 'GATE:S', 'MEXC:S',
                  'BITGET:S', 'HTX:S', 'KUCOIN:P', 'MEXC:P', 'BINGX:P', 'BINGX:S', 'GATE:P']
    const one = async (b) => {
      try {
        const r = await fetch('https://symbol-search.tradingview.com/symbol_search/v3/?text=' + b + 'USDT&hl=0&exchange=&lang=en&search_type=crypto&domain=production')
        const j = await r.json(), c = []
        for (const x of j.symbols || []) {
          const sym = String(x.symbol || '').replace(/<\\/?em>/g, '').toUpperCase(), ex = String(x.prefix || x.exchange || '').toUpperCase()
          const perp = sym === b + 'USDT.P', spot = sym === b + 'USDT' || (ex === 'COINBASE' && sym === b + 'USD')
          if (!ex || !(perp || spot)) continue
          const i = PREF.indexOf(ex + ':' + (perp ? 'P' : 'S'))
          c.push({ tv: ex + ':' + sym, rank: i < 0 ? 50 + (perp ? 0 : 1) : i })
        }
        c.sort((a, z) => a.rank - z.rank)
        return [b, c[0] ? c[0].tv : null]
      } catch (e) { return [b, undefined] }
    }
    const out = {}, list = ${JSON.stringify(bases)}
    for (let i = 0; i < list.length; i += 8) for (const [b, v] of await Promise.all(list.slice(i, i + 8).map(one))) if (v !== undefined) out[b] = v
    return out })()`
  const [r] = await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: expr, awaitPromise: true, returnByValue: true } }], 45000)
  return r?.result?.value || null
}

// ── Live quotes for the Scanner's AVWAP watch: one TradingView screener request for a list of tickers (no chart flipping).
const QUOTE_SYM = /^[A-Z0-9_.&-]{1,30}$/
async function quotes(market, syms) {
  let t
  try { t = await findTarget() } catch { return null }
  const us = market === 'USA' || market === 'US'
  const body = { markets: [us ? 'america' : 'india'], columns: ['name', 'close', 'high', 'low', 'volume', 'average_volume_10d_calc', 'change'],
    filter: [{ left: 'name', operation: 'in_range', right: syms }, { left: 'exchange', operation: 'in_range', right: us ? ['NASDAQ', 'NYSE', 'AMEX'] : ['NSE'] }]
      .concat(us ? [{ left: 'is_primary', operation: 'equal', right: true }] : []), range: [0, 1000] }
  const expr = `fetch('https://scanner.tradingview.com/${us ? 'america' : 'india'}/scan', { method: 'POST', credentials: 'include',
    headers: { 'Content-Type': 'text/plain' }, body: ${JSON.stringify(JSON.stringify(body))} }).then((r) => r.json()).then((j) => {
      const out = {}
      for (const x of j.data || []) { const d = x.d; out[String(d[0]).toUpperCase()] = { close: d[1], high: d[2], low: d[3], volume: d[4], avg_vol: d[5], chg: d[6] } }
      return out })`
  const [r] = await cdp(t.webSocketDebuggerUrl, [{ method: 'Runtime.evaluate', params: { expression: expr, awaitPromise: true, returnByValue: true } }], 20000)
  return r?.result?.value || null
}

// ── AVWAP without opening charts: daily bars from Yahoo Finance (this PC's internet), same rule as the chart read-back —
// anchored at the highest high of the last 100 daily bars, hlc3 × volume. Used to fill names you haven't browsed.
function yahooSym(sym, market) {
  const s = String(sym).toUpperCase()
  return market === 'NSE' ? `${s}.NS` : s.replace(/\./g, '-')
}
async function avwapFromYahoo(sym, market) {
  const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(yahooSym(sym, market))}?range=8mo&interval=1d`,
    { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(15000) })
  if (!r.ok) return null
  const res = (await r.json())?.chart?.result?.[0]
  const q = res?.indicators?.quote?.[0], ts = res?.timestamp
  if (!q || !ts) return null
  let bars = ts.map((t, i) => [t, q.high[i], q.low[i], q.close[i], q.volume[i]]).filter((b) => b[1] != null && b[2] != null && b[3] != null)
  // during the session leave today's unfinished bar out: the AVWAP to beat is the one as of the last close
  const reg = res.meta?.currentTradingPeriod?.regular, nowS = Date.now() / 1000
  if (reg && nowS >= reg.start && nowS < reg.end && bars.length && bars[bars.length - 1][0] >= reg.start) bars = bars.slice(0, -1)
  bars = bars.slice(-100)
  if (bars.length < 30) return null
  let hi = -1, k = -1
  bars.forEach((b, j) => { if (b[1] >= hi) { hi = b[1]; k = j } })
  let pv = 0, vv = 0
  for (let j = k; j < bars.length; j++) { const [, h, l, c, v] = bars[j]; pv += (h + l + c) / 3 * (v || 0); vv += v || 0 }
  if (!vv) return null
  const last = bars[bars.length - 1], r20 = bars.slice(-20)
  const adr = r20.reduce((t, b) => t + (b[1] / b[2] - 1), 0) / r20.length * 100, av = pv / vv
  const d = (t) => new Date(t * 1000).toISOString().slice(0, 10)
  return { avwap: +av.toFixed(4), anchor_date: d(bars[k][0]), anchor_high: hi, close: last[3], bar_date: d(last[0]),
           adr_pct: +adr.toFixed(3), dist_adr: +((last[3] - av) / (last[3] * adr / 100)).toFixed(3), source: 'yahoo' }
}
async function avwapBatch(market, syms) {
  const out = {}
  for (let i = 0; i < syms.length; i += 6) {
    const part = await Promise.all(syms.slice(i, i + 6).map((s) => avwapFromYahoo(s, market).then((v) => [s, v]).catch(() => [s, null])))
    for (const [s, v] of part) if (v) out[s] = v
  }
  return out
}

http.createServer(async (req, res) => {
  const origin = req.headers.origin
  const ok = !origin || ORIGINS.some((o) => o.test(origin))
  const send = (code, body) => {
    res.writeHead(code, { 'Content-Type': 'application/json', ...(ok && origin ? { 'Access-Control-Allow-Origin': origin, Vary: 'Origin' } : {}),
      'Access-Control-Allow-Private-Network': 'true', 'Access-Control-Allow-Methods': 'GET', 'Access-Control-Allow-Headers': '*' })
    res.end(JSON.stringify(body))
  }
  if (!ok) return send(403, { ok: false, error: 'origin not allowed' })
  if (req.method === 'OPTIONS') return send(204, {})
  const u = new URL(req.url, 'http://x')
  if (u.pathname === '/ping') return send(200, { ok: true, app: 'tv-bridge', lists: true, crypto: true, quotes: true, avwap: true, listop: true, cwatch: true, listadd: true, crsort: true, circuit: true })
  if (u.pathname === '/quotes') {
    const mk = (u.searchParams.get('market') || 'NSE').toUpperCase()
    const syms = [...new Set((u.searchParams.get('syms') || '').toUpperCase().split(',').map((x) => x.trim()).filter((x) => QUOTE_SYM.test(x)))].slice(0, 400)
    if (!syms.length) return send(400, { ok: false, error: 'syms needed' })
    try { const q = await quotes(mk, syms); return send(q ? 200 : 502, { ok: !!q, quotes: q }) } catch (e) { return send(502, { ok: false, error: e.message }) }
  }
  if (u.pathname === '/avwap') {
    const mk = (u.searchParams.get('market') || 'NSE').toUpperCase() === 'NSE' ? 'NSE' : 'USA'
    const syms = [...new Set((u.searchParams.get('syms') || '').toUpperCase().split(',').map((x) => x.trim()).filter((x) => QUOTE_SYM.test(x)))].slice(0, 300)
    if (!syms.length) return send(400, { ok: false, error: 'syms needed' })
    try { return send(200, { ok: true, avwap: await avwapBatch(mk, syms) }) } catch (e) { return send(502, { ok: false, error: e.message }) }
  }
  if (u.pathname === '/resolve') {
    const bases = [...new Set((u.searchParams.get('bases') || '').toUpperCase().split(',').map((x) => x.trim()).filter((x) => CR_BASE.test(x)))].slice(0, 60)
    if (!bases.length) return send(400, { ok: false, error: 'bases needed' })
    try { const map = await resolveCrypto(bases); return send(map ? 200 : 502, { ok: !!map, map }) } catch (e) { return send(502, { ok: false, error: e.message }) }
  }
  if (u.pathname === '/crsort') {
    try { const r = await crSort(u.searchParams.get('write') === '1'); return send(r.ok ? 200 : 502, r) } catch (e) { return send(502, { ok: false, error: e.message }) }
  }
  if (u.pathname === '/listadd') {
    const pre = u.searchParams.get('prefix') || '', to = u.searchParams.get('to') || 'Inbox'
    const syms = (u.searchParams.get('symbols') || '').toUpperCase().split(',').map((x) => x.trim()).filter((x) => SYM.test(x)).slice(0, 300)
    if (!LIST_PREFIX.test(pre) || !/^(Leading|Mediocre|Lagging|Inbox)$/.test(to) || !syms.length) return send(400, { ok: false, error: 'prefix, to, symbols' })
    try { const r = await listAdd(pre, to, syms); return send(r?.ok ? 200 : 502, r || { ok: false, error: 'TradingView not reachable' }) } catch (e) { return send(502, { ok: false, error: e.message }) }
  }
  if (u.pathname === '/listop') {
    const pre = u.searchParams.get('prefix') || '', to = u.searchParams.get('to') || '', sym = (u.searchParams.get('symbol') || '').trim().toUpperCase()
    if (!LIST_PREFIX.test(pre) || !LIST_TO.test(to) || !SYM.test(sym)) return send(400, { ok: false, error: 'prefix SD|US, to Leading|Mediocre|Lagging|Inbox|Remove, symbol' })
    try { const r = await listOp(pre, sym, to); return send(r?.ok ? 200 : 502, r || { ok: false, error: 'TradingView not reachable' }) } catch (e) { return send(502, { ok: false, error: e.message }) }
  }
  if (u.pathname === '/cwatch') {
    const syms = [...new Set((u.searchParams.get('syms') || '').toUpperCase().split(',').map((x) => x.trim()).filter((x) => CSYM.test(x)))].slice(0, 200)
    if (!syms.length) return send(400, { ok: false, error: 'syms needed (e.g. BINANCE:BTCUSDT)' })
    try { return send(200, { ok: true, data: await cryptoWatch(syms) }) } catch (e) { return send(502, { ok: false, error: e.message }) }
  }
  if (u.pathname === '/lists') {
    const pre = u.searchParams.get('prefix') || ''
    if (!LIST_PREFIX.test(pre)) return send(400, { ok: false, error: 'prefix must be SD, US or CR' })
    try { const lists = await readLists(pre); return send(lists ? 200 : 502, { ok: !!lists, lists }) } catch (e) { return send(502, { ok: false, error: e.message }) }
  }
  if (u.pathname !== '/open') return send(404, { ok: false })
  const sym = (u.searchParams.get('symbol') || '').trim()
  if (!SYM.test(sym)) return send(400, { ok: false, error: 'bad symbol' })
  try { send(200, { ok: true, ...(await setSymbol(sym, u.searchParams.get('focus') !== '0', u.searchParams.get('avwap') === '1', +u.searchParams.get('band') || 0)) }) } catch (e) { send(502, { ok: false, error: e.message }) }
}).listen(PORT, '127.0.0.1', () => console.log(`tv-bridge on http://127.0.0.1:${PORT} → TradingView debug port ${CDP_PORTS.join('/')}`))
