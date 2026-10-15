-- Shortlister tab: allow scanner = 'SHORTLIST' in daily_snapshots (safe to re-run).
alter table daily_snapshots drop constraint if exists daily_snapshots_scanner_chk;
alter table daily_snapshots add constraint daily_snapshots_scanner_chk
  check (scanner in ('BREAKOUT','ANTICIPATION','SHORTLIST'));
