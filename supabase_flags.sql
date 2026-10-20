-- ═══════════════════════════════════════════════════════════════════════════
-- Flags: tickers you flag while going sector by sector (Sector Focus tab), or add by hand.
-- One row per stock and market. Delete = unflag. Run once in the Supabase SQL Editor. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════
create table if not exists flags (
  id           bigserial   primary key,
  user_id      text        not null,
  market       text        not null,
  symbol       text        not null,
  sector       text,
  source       text,                       -- where it was flagged: sector name, 'manual', …
  note         text        not null default '',
  rank         integer,
  build_range  boolean     not null default false,   -- picked: draw the range and set the alert
  flagged_at   date        not null default current_date,
  updated_at   timestamptz not null default now(),
  unique (user_id, market, symbol)
);
create index if not exists flags_recent on flags (user_id, market, flagged_at desc);

alter table flags enable row level security;
drop policy if exists "app access" on flags;
create policy "app access" on flags for all to anon using (true) with check (true);
grant select, insert, update, delete on flags to anon;
grant usage, select on sequence flags_id_seq to anon;
