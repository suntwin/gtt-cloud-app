-- ═══════════════════════════════════════════════════════════════════════════
-- Journal: market note (required before the scanner runs) + running notes.
-- Run once in the Supabase SQL Editor. Safe to re-run.
-- kind = MARKET (daily market note) · SKIP (logged skip) · NOTE (any-time note)
-- ═══════════════════════════════════════════════════════════════════════════
create table if not exists market_journal (
  id            bigserial primary key,
  user_id       text        not null,
  market        text        not null check (market in ('NSE','USA')),
  session_date  date        not null,
  kind          text        not null check (kind in ('MARKET','SKIP','NOTE')),
  body          text        not null default '',
  symbols       text[]      not null default '{}',
  fields        jsonb       not null default '{}'::jsonb,   -- index trends, breadth, themes, regime
  auto          jsonb,                                   -- breadth measured by the scan itself
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- one market note (or skip) per market and session
create unique index if not exists market_journal_one_note
  on market_journal (user_id, market, session_date) where kind in ('MARKET','SKIP');
create index if not exists market_journal_recent on market_journal (user_id, market, created_at desc);
create index if not exists market_journal_symbols on market_journal using gin (symbols);

alter table market_journal enable row level security;
drop policy if exists "app access" on market_journal;
create policy "app access" on market_journal for all to anon using (true) with check (true);
grant select, insert, update, delete on market_journal to anon;
grant usage, select on sequence market_journal_id_seq to anon;
