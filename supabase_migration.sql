-- ═══════════════════════════════════════════════════════════════════════════
-- GTT Scanner — watchlist database  (run once in Supabase → SQL Editor)
-- Safe to re-run. Works for NSE now and USA later (every table has `market`).
--
-- Watchlists
--   Breakouts watchlist  (from the Post Breakout scanner)
--     EP         Episodic pivot — gap / big move on huge volume. Saved for later.
--     TIGHT_BO   Breakout from a tight range with rising 10 & 20 MA. Enter; if missed, save.
--     WEMA_BO    Breakout from the 10-week EMA. If good and missed, save.
--     ATH        All-time-high breakout (no overhead supply).
--     CONTINUATION  Second leg after an earlier breakout — spotted on another breakout day, or
--                in Anticipation when it gets tight around the breakout candle.
--   UNTAGGED    Old saved_breakouts rows copied across — retag them when you can.
--
--   Tightness list (from the Anticipation scanner) is NOT stored here. It is a
--   one-day list, rebuilt every evening and saved in daily_snapshots
--   (scanner = 'ANTICIPATION', selected = true).
--
-- Exchange: every row stores the TradingView exchange prefix.
--   NSE market → 'NSE' (filled automatically).  USA → NASDAQ / NYSE / AMEX per stock.
--
-- Status of a watchlist entry:  active → traded | removed | expired
-- ═══════════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────────
-- 1. WATCHLIST — ONE row per stock; its setup tags live in setup_types (e.g. {TIGHT_BO,WEMA_BO})
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists watchlist (
  id              bigserial primary key,
  user_id         text        not null,
  market          text        not null default 'NSE'
                  check (market in ('NSE','USA')),
  exchange        text        not null default 'NSE'
                  check (exchange in ('NSE','BSE','NASDAQ','NYSE','AMEX','ARCA','BATS')),
  symbol          text        not null,
  setup_type      text        not null
                  check (setup_type in ('EP','TIGHT_BO','WEMA_BO','ATH','CONTINUATION','UNTAGGED')),
  source_scanner  text        not null default 'MANUAL'
                  check (source_scanner in ('BREAKOUT','ANTICIPATION','MANUAL','MIGRATED')),
  status          text        not null default 'active'
                  check (status in ('active','traded','removed','expired')),
  tags            text[]      not null default '{}',     -- free tags, e.g. {missed, sector-leader, A+}

  -- when it was added and the day that triggered it (breakout day / tight day)
  added_date      date        not null default current_date,
  trigger_date    date,

  -- trigger-day snapshot (what the stock looked like when it was saved)
  trigger_close   numeric,
  prev_close      numeric,          -- close before the trigger day — failure level for breakouts
  trigger_high    numeric,          -- fill when the scan provides it
  trigger_low     numeric,          -- stop = low of the day
  chg_pct         numeric,
  gap_pct         numeric,          -- open vs prev close (EP)
  vol_ratio       numeric,
  adr             numeric,
  rel_tightness   numeric,          -- NR4 / ADR
  dist_10wema_pct numeric,
  dist_10ma_pct   numeric,
  dist_20ma_pct   numeric,
  ma10_rising     boolean,
  ma20_rising     boolean,
  scan_count      int check (scan_count between 0 and 3),
  rating          int check (rating between 1 and 5),   -- your star rating after the chart check (3, 4, 5)
  sector          text,
  metrics         jsonb       not null default '{}',     -- anything else from the scan

  -- tracking
  last_seen       date,             -- last day it appeared in a scan
  last_status     text,             -- latest daily label, e.g. RESETUP, WAIT, CHECK
  resetup_count   int         not null default 0,
  expires_on      date,             -- auto-expire after this date (cleanup)
  traded_date     date,
  removed_date    date,
  removed_reason  text,             -- manual | failed | stale | gapped | expired | traded
  notes           text,

  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- (the "one active entry per stock" index is created in the upgrade block below)
create index if not exists watchlist_lookup
  on watchlist (user_id, market, status, setup_type);
create index if not exists watchlist_symbol on watchlist (user_id, market, symbol);

-- upgrade from an earlier version of this script (safe if already current)
update watchlist set exchange = case market when 'NSE' then 'NSE' else 'NASDAQ' end where exchange is null;
alter table watchlist alter column exchange set default 'NSE';
alter table watchlist alter column exchange set not null;
update watchlist set status = 'removed', removed_date = current_date, removed_reason = 'tightness not kept in watchlist'
 where setup_type = 'TIGHTNESS' and status = 'active';
update watchlist set setup_type = 'UNTAGGED' where setup_type = 'TIGHTNESS';
alter table watchlist drop constraint if exists watchlist_setup_type_check;
alter table watchlist add constraint watchlist_setup_type_check
  check (setup_type in ('EP','TIGHT_BO','WEMA_BO','ATH','CONTINUATION','UNTAGGED'));
alter table watchlist drop constraint if exists watchlist_exchange_check;
alter table watchlist add constraint watchlist_exchange_check
  check (exchange in ('NSE','BSE','NASDAQ','NYSE','AMEX','ARCA','BATS'));

alter table watchlist add column if not exists rating int;
alter table watchlist drop constraint if exists watchlist_rating_check;
alter table watchlist add constraint watchlist_rating_check check (rating between 1 and 5);

-- One entry per stock: all its setup tags in one array. setup_type = the first tag (kept for older code).
alter table watchlist add column if not exists setup_types text[] not null default '{}';
alter table watchlist add column if not exists last_trigger_date date;   -- latest breakout day (continuations)
update watchlist set setup_types = array[setup_type] where cardinality(setup_types) = 0;
update watchlist set last_trigger_date = coalesce(trigger_date, added_date) where last_trigger_date is null;

-- merge older per-tag duplicates into the oldest active row of each stock
with g as (
  select user_id, market, symbol, min(id) as keep_id,
         array(select distinct t from watchlist w2, unnest(w2.setup_types) t
                where w2.user_id = w.user_id and w2.market = w.market and w2.symbol = w.symbol
                  and w2.status = 'active' order by t) as all_types,
         max(rating) as best_rating, min(coalesce(trigger_date, added_date)) as first_day,
         max(coalesce(last_trigger_date, trigger_date, added_date)) as last_day
  from watchlist w where status = 'active'
  group by user_id, market, symbol having count(*) > 1
), k as (
  update watchlist w set setup_types = g.all_types, rating = coalesce(g.best_rating, w.rating),
         trigger_date = least(coalesce(w.trigger_date, g.first_day), g.first_day), last_trigger_date = g.last_day
  from g where w.id = g.keep_id
  returning w.id
)
update watchlist w set status = 'removed', removed_date = current_date, removed_reason = 'merged into one entry'
from g where w.user_id = g.user_id and w.market = g.market and w.symbol = g.symbol
  and w.status = 'active' and w.id <> g.keep_id;

drop index if exists watchlist_one_active;
create unique index if not exists watchlist_one_active_stock
  on watchlist (user_id, market, symbol) where status = 'active';
alter table watchlist drop constraint if exists watchlist_setup_types_check;
alter table watchlist add constraint watchlist_setup_types_check
  check (setup_types <@ array['EP','TIGHT_BO','WEMA_BO','ATH','CONTINUATION','UNTAGGED']::text[]);
create index if not exists watchlist_types on watchlist using gin (setup_types);

-- keep setup_type = first tag, and never leave setup_types empty
create or replace function watchlist_sync_types() returns trigger language plpgsql as $$
begin
  if new.setup_types is null or cardinality(new.setup_types) = 0 then
    new.setup_types := array[coalesce(new.setup_type, 'UNTAGGED')];
  end if;
  if cardinality(new.setup_types) > 1 then
    new.setup_types := array_remove(new.setup_types, 'UNTAGGED');   -- a real tag replaces UNTAGGED
  end if;
  new.setup_type := new.setup_types[1];
  return new;
end $$;
drop trigger if exists watchlist_types_sync on watchlist;
create trigger watchlist_types_sync before insert or update on watchlist
  for each row execute function watchlist_sync_types();

-- exchange defaults from the market when the app doesn't send one
create or replace function watchlist_default_exchange() returns trigger language plpgsql as $$
begin
  if new.exchange is null or new.exchange = '' then
    new.exchange := case new.market when 'NSE' then 'NSE' else 'NASDAQ' end;
  end if;
  new.symbol := upper(new.symbol);
  return new;
end $$;
drop trigger if exists watchlist_exchange on watchlist;
create trigger watchlist_exchange before insert or update of exchange, market, symbol on watchlist
  for each row execute function watchlist_default_exchange();


-- ───────────────────────────────────────────────────────────────────────────
-- 2. WATCHLIST EVENTS — audit trail (added, seen, re-setup, traded, removed…)
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists watchlist_events (
  id           bigserial primary key,
  watchlist_id bigint      not null references watchlist(id) on delete cascade,
  event_date   date        not null default current_date,
  event        text        not null
               check (event in ('added','seen','resetup','triggered','traded','removed','expired','retagged','note')),
  detail       text,
  metrics      jsonb       not null default '{}',
  created_at   timestamptz not null default now()
);
create index if not exists watchlist_events_entry on watchlist_events (watchlist_id, event_date desc);


-- ───────────────────────────────────────────────────────────────────────────
-- 3. DAILY SNAPSHOTS — every stock each scanner saw each day, with its label
--    (this is the history that stops trades being missed)
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists daily_snapshots (
  id          bigserial primary key,
  user_id     text        not null,
  snap_date   date        not null,
  symbol      text        not null,
  list        text        not null,          -- label for the day
  selected    boolean     not null default false,   -- on the next day's working watchlist
  scan_count  int,
  metrics     jsonb,
  created_at  timestamptz not null default now()
);
alter table daily_snapshots
  add column if not exists market  text not null default 'NSE',
  add column if not exists scanner text not null default 'ANTICIPATION',
  add column if not exists exchange text not null default 'NSE';
do $$ begin
  alter table daily_snapshots add constraint daily_snapshots_market_chk  check (market  in ('NSE','USA'));
exception when duplicate_object then null; end $$;
do $$ begin
  alter table daily_snapshots add constraint daily_snapshots_scanner_chk check (scanner in ('BREAKOUT','ANTICIPATION'));
exception when duplicate_object then null; end $$;
-- replace the old single-market key if an earlier version created it
alter table daily_snapshots drop constraint if exists daily_snapshots_user_id_snap_date_symbol_key;
create unique index if not exists daily_snapshots_key
  on daily_snapshots (user_id, market, scanner, snap_date, symbol);
create index if not exists daily_snapshots_by_date
  on daily_snapshots (user_id, market, snap_date desc);


-- ───────────────────────────────────────────────────────────────────────────
-- 4. Keep updated_at current
-- ───────────────────────────────────────────────────────────────────────────
create or replace function set_updated_at() returns trigger language plpgsql as $$
begin new.updated_at := now(); return new; end $$;
drop trigger if exists watchlist_updated_at on watchlist;
create trigger watchlist_updated_at before update on watchlist
  for each row execute function set_updated_at();


-- ───────────────────────────────────────────────────────────────────────────
-- 5. GENERATE A WATCHLIST — one call per button
--    select * from get_watchlist('nse_user','NSE','EP');
--    select tv_list from get_watchlist_tv('nse_user','NSE','TIGHTNESS');
--    p_setup_type = null → every active entry
-- ───────────────────────────────────────────────────────────────────────────
drop view if exists v_active_watchlist cascade;   -- rebuilt below (its columns follow the table)
create view v_active_watchlist with (security_invoker = true) as
select w.*,
       w.exchange || ':' || w.symbol as tv_symbol,
       (current_date - coalesce(w.last_trigger_date, w.trigger_date, w.added_date)) as age_days
from watchlist w
where w.status = 'active';

create or replace function get_watchlist(p_user text, p_market text, p_setup_type text default null)
returns setof v_active_watchlist language sql stable as $$
  select * from v_active_watchlist
  where user_id = p_user and market = p_market
    and (p_setup_type is null or p_setup_type = any(setup_types))
  order by rating desc nulls last, coalesce(last_trigger_date, trigger_date, added_date) desc, symbol;
$$;

create or replace function get_watchlist_tv(p_user text, p_market text, p_setup_type text default null)
returns table (setup_type text, n int, tv_list text) language sql stable as $$
  select t, count(*)::int, string_agg(v.tv_symbol, ',' order by v.rating desc nulls last, v.symbol)
  from v_active_watchlist v, unnest(v.setup_types) t
  where v.user_id = p_user and v.market = p_market
    and (p_setup_type is null or t = p_setup_type)
  group by t order by t;
$$;


-- ───────────────────────────────────────────────────────────────────────────
-- 6. SAVE TO WATCHLIST — insert or refresh the active entry, log the event
--    select add_to_watchlist('nse_user','NSE','RELIANCE','TIGHT_BO','BREAKOUT',
--                            '2026-09-24', '{"trigger_close":2950,"prev_close":2880,"vol_ratio":3.1}');
-- ───────────────────────────────────────────────────────────────────────────
create or replace function add_to_watchlist(
  p_user text, p_market text, p_symbol text, p_setup_type text,
  p_source text default 'MANUAL', p_trigger_date date default current_date,
  p_data jsonb default '{}', p_tags text[] default '{}', p_expire_days int default null)
returns bigint language plpgsql as $$
declare v_id bigint;
begin
  select id into v_id from watchlist
   where user_id = p_user and market = p_market and symbol = upper(p_symbol) and status = 'active';

  if v_id is null then
    insert into watchlist (user_id, market, exchange, symbol, setup_type, setup_types, source_scanner, trigger_date, last_trigger_date,
      trigger_close, prev_close, trigger_high, trigger_low, chg_pct, gap_pct, vol_ratio, adr,
      rel_tightness, dist_10wema_pct, dist_10ma_pct, dist_20ma_pct, ma10_rising, ma20_rising,
      scan_count, rating, sector, metrics, tags, last_seen, expires_on)
    values (p_user, p_market, p_data->>'exchange', upper(p_symbol), p_setup_type, array[p_setup_type], p_source, p_trigger_date, p_trigger_date,
      (p_data->>'trigger_close')::numeric, (p_data->>'prev_close')::numeric,
      (p_data->>'trigger_high')::numeric,  (p_data->>'trigger_low')::numeric,
      (p_data->>'chg_pct')::numeric, (p_data->>'gap_pct')::numeric, (p_data->>'vol_ratio')::numeric,
      (p_data->>'adr')::numeric, (p_data->>'rel_tightness')::numeric,
      (p_data->>'dist_10wema_pct')::numeric, (p_data->>'dist_10ma_pct')::numeric, (p_data->>'dist_20ma_pct')::numeric,
      (p_data->>'ma10_rising')::boolean, (p_data->>'ma20_rising')::boolean,
      (p_data->>'scan_count')::int, (p_data->>'rating')::int, p_data->>'sector', p_data, p_tags, p_trigger_date,
      case when p_expire_days is null then null else p_trigger_date + p_expire_days end)
    returning id into v_id;
    insert into watchlist_events (watchlist_id, event_date, event, detail, metrics)
    values (v_id, p_trigger_date, 'added', p_source, p_data);
  else
    -- same stock: add the tag to its one entry; a LATER breakout day refreshes the breakout numbers
    update watchlist w set
      setup_types = case when p_setup_type = any(w.setup_types) then w.setup_types
                         else array_append(w.setup_types, p_setup_type) end,
      last_seen = greatest(coalesce(w.last_seen, p_trigger_date), p_trigger_date),
      tags      = (select array(select distinct unnest(w.tags || p_tags))),
      metrics   = w.metrics || p_data,
      rating    = coalesce((p_data->>'rating')::int, w.rating),
      last_trigger_date = greatest(coalesce(w.last_trigger_date, w.trigger_date, p_trigger_date), p_trigger_date),
      trigger_close = case when p_trigger_date > coalesce(w.last_trigger_date, w.trigger_date, p_trigger_date)
                           then coalesce((p_data->>'trigger_close')::numeric, w.trigger_close) else w.trigger_close end,
      prev_close    = case when p_trigger_date > coalesce(w.last_trigger_date, w.trigger_date, p_trigger_date)
                           then coalesce((p_data->>'prev_close')::numeric, w.prev_close) else w.prev_close end,
      vol_ratio     = case when p_trigger_date > coalesce(w.last_trigger_date, w.trigger_date, p_trigger_date)
                           then coalesce((p_data->>'vol_ratio')::numeric, w.vol_ratio) else w.vol_ratio end
    where w.id = v_id;
    insert into watchlist_events (watchlist_id, event_date, event, detail, metrics)
    values (v_id, p_trigger_date, 'retagged', p_setup_type || ' added by ' || p_source, p_data);
  end if;
  return v_id;
end $$;


-- ───────────────────────────────────────────────────────────────────────────
-- 7. CLEAN UP
--    remove_from_watchlist: one entry (manual / traded / failed …)
--    cleanup_watchlist:     expire old entries in bulk; returns what it touched
--      select * from cleanup_watchlist('nse_user','NSE');               -- defaults
--      select * from cleanup_watchlist('nse_user','NSE', 20, 10, 'EP');  -- only EPs
--    purge_snapshots:       delete old snapshot rows (keeps labelled rows by default)
-- ───────────────────────────────────────────────────────────────────────────
create or replace function remove_from_watchlist(
  p_id bigint, p_reason text default 'manual', p_status text default 'removed')
returns void language plpgsql as $$
begin
  if p_status not in ('removed','traded','expired') then
    raise exception 'status must be removed, traded or expired';
  end if;
  update watchlist set status = p_status,
         removed_date = current_date, removed_reason = p_reason,
         traded_date = case when p_status = 'traded' then current_date else traded_date end
   where id = p_id and status = 'active';
  insert into watchlist_events (watchlist_id, event, detail)
  values (p_id, case p_status when 'traded' then 'traded' when 'expired' then 'expired' else 'removed' end, p_reason);
end $$;

create or replace function cleanup_watchlist(
  p_user text, p_market text,
  p_max_age_days int default 30,      -- older than this since trigger → expire
  p_unseen_days int default 10,       -- not seen in any scan for this long → expire
  p_setup_type text default null)
returns table (id bigint, symbol text, setup_type text, reason text) language plpgsql as $$
begin
  return query
  with c as (
    select w.id, w.symbol, w.setup_type,
      case
        when w.expires_on is not null and w.expires_on < current_date then 'expired'
        when current_date - coalesce(w.last_trigger_date, w.trigger_date, w.added_date) > p_max_age_days then 'stale'
        when w.last_seen is not null and current_date - w.last_seen > p_unseen_days then 'unseen'
      end as reason
    from watchlist w
    where w.user_id = p_user and w.market = p_market and w.status = 'active'
      and (p_setup_type is null or p_setup_type = any(w.setup_types))
  ), u as (
    update watchlist w set status = 'expired', removed_date = current_date, removed_reason = c.reason
    from c where w.id = c.id and c.reason is not null
    returning w.id, w.symbol, w.setup_type, c.reason
  ), e as (
    insert into watchlist_events (watchlist_id, event, detail)
    select u.id, 'expired', u.reason from u
  )
  select u.id, u.symbol, u.setup_type, u.reason from u;
end $$;

create or replace function purge_snapshots(
  p_user text, p_market text, p_keep_days int default 365, p_keep_labelled boolean default true)
returns int language plpgsql as $$
declare n int;
begin
  delete from daily_snapshots
   where user_id = p_user and market = p_market
     and snap_date < current_date - p_keep_days
     and (not p_keep_labelled or list = 'SCANNED');
  get diagnostics n = row_count;
  return n;
end $$;


-- ───────────────────────────────────────────────────────────────────────────
-- 8. Existing saved_breakouts — keep working, and copy into the new watchlist
-- ───────────────────────────────────────────────────────────────────────────
alter table saved_breakouts
  add column if not exists status         text default 'saved',
  add column if not exists bo_date        date,
  add column if not exists bo_close       numeric,
  add column if not exists bo_prev_close  numeric,
  add column if not exists bo_vol_ratio   numeric,
  add column if not exists bo_adr         numeric,
  add column if not exists last_seen      date,
  add column if not exists removed_date   date,
  add column if not exists removed_reason text;
update saved_breakouts set status = 'saved' where status is null;

insert into watchlist (user_id, market, exchange, symbol, setup_type, source_scanner, added_date, trigger_date,
                       trigger_close, prev_close, vol_ratio, adr, last_seen, notes)
select distinct on (s.user_id, upper(s.symbol))
       s.user_id,
       case when s.user_id ilike 'us%' then 'USA' else 'NSE' end,
       case when s.user_id ilike 'us%' then 'NASDAQ' else 'NSE' end,
       upper(s.symbol), 'UNTAGGED', 'MIGRATED',
       coalesce(left(s.saved_date::text, 10)::date, current_date),
       s.bo_date, s.bo_close, s.bo_prev_close, s.bo_vol_ratio, s.bo_adr, s.last_seen,
       'Copied from saved_breakouts — set the setup type'
from saved_breakouts s
where coalesce(s.status, 'saved') <> 'removed'
  and not exists (select 1 from watchlist w
                   where w.user_id = s.user_id and w.symbol = upper(s.symbol) and w.status = 'active')
order by s.user_id, upper(s.symbol), s.saved_date desc;


-- ───────────────────────────────────────────────────────────────────────────
-- 9. Access — the app uses the publishable (anon) key.
--    Mirrors an "app can read/write" setup. If saved_breakouts uses stricter
--    policies, copy those instead.
-- ───────────────────────────────────────────────────────────────────────────
alter table watchlist        enable row level security;
alter table watchlist_events enable row level security;
alter table daily_snapshots  enable row level security;
drop policy if exists "app access" on watchlist;
drop policy if exists "app access" on watchlist_events;
drop policy if exists "app access" on daily_snapshots;
create policy "app access" on watchlist        for all to anon using (true) with check (true);
create policy "app access" on watchlist_events for all to anon using (true) with check (true);
create policy "app access" on daily_snapshots  for all to anon using (true) with check (true);
grant select on v_active_watchlist to anon;
grant execute on function get_watchlist(text,text,text), get_watchlist_tv(text,text,text),
  add_to_watchlist(text,text,text,text,text,date,jsonb,text[],int),
  remove_from_watchlist(bigint,text,text), cleanup_watchlist(text,text,int,int,text),
  purge_snapshots(text,text,int,boolean) to anon;
