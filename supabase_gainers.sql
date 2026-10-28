-- ═══════════════════════════════════════════════════════════════════════════
-- Top gainers history: one row per stock per session from your MarketInOut EP scan.
-- Lets Market Themes show which sectors keep producing gainers (rotation) vs a one-day pop.
-- Run once in the Supabase SQL Editor. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════
create table if not exists top_gainers (
  user_id    text not null,
  market     text not null,
  snap_date  date not null,
  symbol     text not null,
  sector     text,
  industry   text,
  chg        real,
  vol_x      real,
  last       real,
  primary key (user_id, market, snap_date, symbol)
);
create index if not exists top_gainers_by_date on top_gainers (user_id, market, snap_date desc);

alter table top_gainers enable row level security;
drop policy if exists "app access" on top_gainers;
create policy "app access" on top_gainers for all to anon using (true) with check (true);
grant select, insert, update, delete on top_gainers to anon;
