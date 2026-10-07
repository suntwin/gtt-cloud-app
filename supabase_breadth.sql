-- ═══════════════════════════════════════════════════════════════════════════
-- Market breadth: one row per market per session (stock counts, entered daily
-- in the market note, or uploaded in bulk from history). Run once in the
-- Supabase SQL Editor. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════
create table if not exists market_breadth (
  id            bigserial primary key,
  user_id       text        not null,
  market        text        not null check (market in ('NSE','USA')),
  session_date  date        not null,
  up45          integer,   -- stocks up 4.5% in the last session
  down45        integer,   -- stocks down 4.5% in the last session
  up20          integer,   -- stocks up 20% in the last 5 days
  down20        integer,   -- stocks down 20% in the last 5 days
  above20       integer,   -- stocks above the 20 DMA
  below20       integer,   -- stocks below the 20 DMA
  above50       integer,   -- stocks above the 50 DMA
  below50       integer,   -- stocks below the 50 DMA
  source        text        not null default 'manual',   -- manual | upload
  updated_at    timestamptz not null default now()
);
create unique index if not exists market_breadth_one_day on market_breadth (user_id, market, session_date);

alter table market_breadth enable row level security;
drop policy if exists "app access" on market_breadth;
create policy "app access" on market_breadth for all to anon using (true) with check (true);
grant select, insert, update, delete on market_breadth to anon;
grant usage, select on sequence market_breadth_id_seq to anon;
