-- ═══════════════════════════════════════════════════════════════════════════
-- Hidden stocks: tickers you hide from Sector Focus step 2 (too thin, operator-driven, bad news …).
-- They stay hidden until you unhide them; ranks and 👑 badges are worked out without them.
-- Run once in the Supabase SQL Editor. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════
create table if not exists hidden_symbols (
  user_id    text not null,
  market     text not null,
  symbol     text not null,
  sector     text,
  reason     text not null default '',
  hidden_at  date not null default current_date,
  primary key (user_id, market, symbol)
);

alter table hidden_symbols enable row level security;
drop policy if exists "app access" on hidden_symbols;
create policy "app access" on hidden_symbols for all to anon using (true) with check (true);
grant select, insert, update, delete on hidden_symbols to anon;
