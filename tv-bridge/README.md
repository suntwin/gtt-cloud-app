# tv-bridge

Makes the Scanner's **TradingView** button switch the chart in TradingView Desktop instead of opening a browser tab.

`tv-bridge.js` is a small Node program (Node 22+) listening on `127.0.0.1:9333` (this PC only). It accepts requests only
from myswingdesk.app, *.frappe.cloud and localhost, and only does one thing: set the chart symbol over TradingView's
debug port (9222 — the same one the TradingView MCP uses). If TradingView is closed, it starts it in debug mode first.

## One-time setup
Right-click `install.ps1` → **Run with PowerShell**. It:
- adds a Startup entry, so at every login TradingView opens in debug mode and the bridge starts (no windows);
- adds a **TradingView (debug)** shortcut on the Desktop and Start menu — pin that to the taskbar and use it instead of
  the normal TradingView icon.

## Files
- `start-tradingview.ps1` — starts TradingView with `--remote-debugging-port=9222`. If a Microsoft Store install ignores the
  flag, it runs a private copy from `%LOCALAPPDATA%\TradingView-debug` (copied once, ~330 MB; recopied after updates).
- `start-all.vbs` — what the Startup entry runs (TradingView + bridge, hidden).
- `start-hidden.vbs` — just the bridge, hidden.

## If the button opens the browser
- http://127.0.0.1:9333/ping should show `{"ok":true,...}` — if not, run `node tv-bridge.js` in this folder to see the error.
- TradingView opened from the normal icon has no debug port: close it and use **TradingView (debug)**.
- Chrome: site settings for myswingdesk.app → Local network access → Allow.
- Different port: `setx TV_CDP_PORT 9223`.
