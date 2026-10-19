-- ═══════════════════════════════════════════════════════════════════════════
-- TradingView alerts, copied from TradingView Desktop by sync_tv_alerts.py (runs on your PC).
-- tv_alerts      = your active alerts right now (rows that disappear in TradingView are removed)
-- tv_alert_sync  = one row per sync run (when, how many, error if TradingView wasn't reachable)
-- Run once in the Supabase SQL Editor. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════
create table if not exists tv_alerts (
  alert_id     bigint      primary key,
  symbol       text        not null,          -- as TradingView has it, e.g. NSE:SEDEMAC, BATS:APPN
  exchange     text        not null default '',
  ticker       text        not null default '',
  market       text        not null default '',   -- NSE · USA · CRYPTO
  resolution   text,                          -- 1, 5, 60, 1D, 1W …
  alert_type   text,                          -- drawing · price · study
  line_tool    text,                          -- HorzLine · TrendLine · (blank for price alerts)
  price1       double precision,              -- line start (or the alert price)
  price2       double precision,              -- line end
  cond_type    text,                          -- cross · greater · less …
  frequency    text,                          -- on_first_fire · every_time …
  message      text,
  active       boolean     not null default true,
  created      timestamptz,
  last_fired   timestamptz,
  expiration   timestamptz,
  raw          jsonb,
  synced_at    timestamptz not null default now()
);
create index if not exists tv_alerts_market on tv_alerts (market, ticker);

create table if not exists tv_alert_sync (
  id         bigserial   primary key,
  synced_at  timestamptz not null default now(),
  n_alerts   integer     not null default 0,
  ok         boolean     not null default true,
  error      text
);
create index if not exists tv_alert_sync_recent on tv_alert_sync (synced_at desc);

alter table tv_alerts enable row level security;
drop policy if exists "app access" on tv_alerts;
create policy "app access" on tv_alerts for all to anon using (true) with check (true);
grant select, insert, update, delete on tv_alerts to anon;

alter table tv_alert_sync enable row level security;
drop policy if exists "app access" on tv_alert_sync;
create policy "app access" on tv_alert_sync for all to anon using (true) with check (true);
grant select, insert, update, delete on tv_alert_sync to anon;
grant usage, select on sequence tv_alert_sync_id_seq to anon;
