-- ═══════════════════════════════════════════════════════════════════════════
-- NSE price bands (circuit limits) from NSE's daily security list:
--   https://nsearchives.nseindia.com/content/equities/sec_list_DDMMYYYY.csv
-- The NSE page downloads the newest file once per session and replaces these rows.
-- band = 2 / 5 / 10 / 20 (%) · NULL with band_text 'No Band' = no circuit (F&O stocks).
-- Run once in the Supabase SQL Editor. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════
create table if not exists nse_price_bands (
  symbol      text        not null,
  series      text        not null default 'EQ',
  name        text,
  band        double precision,
  band_text   text,
  remarks     text,
  file_date   date        not null,
  updated_at  timestamptz not null default now(),
  primary key (symbol, series)
);
create index if not exists nse_price_bands_file on nse_price_bands (file_date);

alter table nse_price_bands enable row level security;
drop policy if exists "app access" on nse_price_bands;
create policy "app access" on nse_price_bands for all to anon using (true) with check (true);
grant select, insert, update, delete on nse_price_bands to anon;
