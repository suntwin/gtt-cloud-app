-- ═══════════════════════════════════════════════════════════════════════════
-- Positions: open trades you're managing (Positions tab) and their partial / full exits.
-- Run once in the Supabase SQL Editor. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════
create table if not exists positions (
  id          bigserial   primary key,
  user_id     text        not null,
  market      text        not null,
  symbol      text        not null,
  sector      text,
  industry    text,
  setup       text,
  entry_date  date        not null default current_date,
  avg_price   numeric     not null,
  qty         numeric     not null default 0,     -- shares bought (0 = not tracking size)
  qty_open    numeric     not null default 0,     -- shares still held
  stop        numeric,                            -- current stop
  init_stop   numeric,                            -- stop at entry (for R-multiples)
  fail_level  numeric,                            -- breakout day's pre-breakout close, if it was a saved breakout
  note        text        not null default '',
  status      text        not null default 'open', -- open | closed
  closed_at   date,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists positions_open on positions (user_id, market, status);

create table if not exists position_exits (
  id           bigserial   primary key,
  position_id  bigint      not null references positions(id) on delete cascade,
  exit_date    date        not null default current_date,
  qty          numeric     not null default 0,
  price        numeric     not null,
  note         text        not null default '',
  created_at   timestamptz not null default now()
);
create index if not exists position_exits_pos on position_exits (position_id);

alter table positions enable row level security;
alter table position_exits enable row level security;
drop policy if exists "app access" on positions;
drop policy if exists "app access" on position_exits;
create policy "app access" on positions for all to anon using (true) with check (true);
create policy "app access" on position_exits for all to anon using (true) with check (true);
grant select, insert, update, delete on positions, position_exits to anon;
grant usage, select on sequence positions_id_seq, position_exits_id_seq to anon;
