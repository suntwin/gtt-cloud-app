-- ═══════════════════════════════════════════════════════════════════════════
-- Learning loop for Sector Focus (focus_review.py). Run once in the Supabase SQL Editor. Safe to re-run.
-- focus_snapshots  = what Sector Focus said about every scanned stock each session (score, rules, setup, MA hold,
--                    flagged / picked) — so a later breakout can be compared with what we said the day before
-- breakout_reviews = every breakout in the scan, sorted into a bucket (held back by rules / shown not flagged /
--                    flagged / not in the scan the day before)
-- ═══════════════════════════════════════════════════════════════════════════
create table if not exists focus_snapshots (
  user_id    text not null,
  market     text not null,
  snap_date  date not null,
  symbol     text not null,
  sector     text,
  score      double precision,
  setup      text,
  ma_hold    text,
  rules      jsonb,
  flagged    boolean not null default false,
  picked     boolean not null default false,
  adr        double precision,
  rel_tight  double precision,
  last       double precision,
  updated_at timestamptz not null default now(),
  primary key (user_id, market, snap_date, symbol)
);
create table if not exists breakout_reviews (
  user_id      text not null,
  market       text not null,
  snap_date    date not null,
  symbol       text not null,
  sector       text,
  chg          double precision,
  vol_x        double precision,
  adr          double precision,
  bucket       text,
  prev_date    date,
  prev_score   double precision,
  prev_setup   text,
  prev_ma_hold text,
  failed_rules text,
  updated_at   timestamptz not null default now(),
  primary key (user_id, market, snap_date, symbol)
);
alter table focus_snapshots enable row level security;
drop policy if exists "app access" on focus_snapshots;
create policy "app access" on focus_snapshots for all to anon using (true) with check (true);
grant select, insert, update, delete on focus_snapshots to anon;
alter table breakout_reviews enable row level security;
drop policy if exists "app access" on breakout_reviews;
create policy "app access" on breakout_reviews for all to anon using (true) with check (true);
grant select, insert, update, delete on breakout_reviews to anon;
