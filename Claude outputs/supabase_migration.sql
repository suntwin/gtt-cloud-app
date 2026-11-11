-- Build Tomorrow: run once in Supabase → SQL Editor before using the new tab.

-- 1. Daily snapshot: one row per stock per day, with its label and key metrics
create table if not exists daily_snapshots (
  id          bigserial primary key,
  user_id     text        not null,
  snap_date   date        not null,
  symbol      text        not null,
  list        text        not null,          -- 1_BUY_SIGNAL, 2_SAVE, 3_WAIT, 4_RESETUP, 5_REMOVE, CANDIDATE, CHECK, SCANNED
  selected    boolean     not null default false,   -- on tomorrow's list
  scan_count  int,
  metrics     jsonb,
  created_at  timestamptz not null default now(),
  unique (user_id, snap_date, symbol)
);
create index if not exists daily_snapshots_user_date on daily_snapshots (user_id, snap_date desc);

-- 2. Saved watchlist: keep breakout-day data and soft-remove instead of deleting
alter table saved_breakouts
  add column if not exists status         text default 'saved',   -- saved | removed
  add column if not exists bo_date        date,
  add column if not exists bo_close       numeric,
  add column if not exists bo_prev_close  numeric,                -- close before the breakout day (failure level)
  add column if not exists bo_vol_ratio   numeric,
  add column if not exists bo_adr         numeric,
  add column if not exists last_seen      date,
  add column if not exists removed_date   date,
  add column if not exists removed_reason text;
update saved_breakouts set status = 'saved' where status is null;

-- 3. Access. The app uses the publishable (anon) key.
--    If RLS is ON for saved_breakouts, give daily_snapshots the same policy you use there.
--    The lines below mirror an "anon can read/write" setup — adjust if yours is stricter.
alter table daily_snapshots enable row level security;
drop policy if exists "app access" on daily_snapshots;
create policy "app access" on daily_snapshots for all to anon using (true) with check (true);
