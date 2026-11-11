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

function cdp(wsUrl, calls) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl), out = []
    let id = 0
    const timer = setTimeout(() => { ws.close(); reject(new Error('TradingView did not answer')) }, 8000)
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

async function setSymbol(sym, focus = true) {
  let t
  try { t = await findTarget() } catch { await launchTV(); t = await findTarget() }
  await waitForChart(t)
  const expr = `new Promise((ok, no) => { try { const c = TradingViewApi.activeChart();
    const done = () => ok(c.symbol()); c.setSymbol(${JSON.stringify(sym)}, done); setTimeout(done, 4000) } catch (e) { no(String(e)) } })`
  const calls = [{ method: 'Runtime.evaluate', params: { expression: expr, awaitPromise: true, returnByValue: true } }]
  if (focus) calls.unshift({ method: 'Page.bringToFront' })
  const r = (await cdp(t.webSocketDebuggerUrl, calls)).at(-1)
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.value || 'setSymbol failed')
  if (focus && process.platform === 'win32')  // raise the TradingView window (fixed command, no user input)
    exec(`powershell -NoProfile -Command "(New-Object -ComObject WScript.Shell).AppActivate('TradingView') | Out-Null"`, { windowsHide: true })
  return r.result.value
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
  if (u.pathname === '/ping') return send(200, { ok: true, app: 'tv-bridge' })
  if (u.pathname !== '/open') return send(404, { ok: false })
  const sym = (u.searchParams.get('symbol') || '').trim()
  if (!SYM.test(sym)) return send(400, { ok: false, error: 'bad symbol' })
  try { send(200, { ok: true, symbol: await setSymbol(sym, u.searchParams.get('focus') !== '0') }) } catch (e) { send(502, { ok: false, error: e.message }) }
}).listen(PORT, '127.0.0.1', () => console.log(`tv-bridge on http://127.0.0.1:${PORT} → TradingView debug port ${CDP_PORTS.join('/')}`))
