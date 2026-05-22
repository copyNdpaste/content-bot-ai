create table if not exists public.content_performance_snapshots (
  id uuid primary key default gen_random_uuid(),
  draft_path text not null,
  platform_id text not null,
  account text not null,
  platform_post_id text not null,
  snapshot_type text not null default 'latest',
  snapshot_at timestamptz not null default now(),
  views integer not null default 0,
  reach integer not null default 0,
  likes integer not null default 0,
  comments integer not null default 0,
  shares integer not null default 0,
  saves integer not null default 0,
  reposts integer not null default 0,
  quotes integer not null default 0,
  interactions integer not null default 0,
  engagement_rate numeric not null default 0,
  score numeric not null default 0,
  raw_metrics jsonb not null default '{}'::jsonb,
  metric_errors jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (draft_path, snapshot_type)
);

create index if not exists idx_content_performance_snapshots_platform
  on public.content_performance_snapshots(platform_id, account, snapshot_at desc);

create index if not exists idx_content_performance_snapshots_score
  on public.content_performance_snapshots(score desc);

create table if not exists public.content_performance_reports (
  id uuid primary key default gen_random_uuid(),
  report_key text not null unique,
  report_type text not null,
  period_start date,
  period_end date,
  summary_text text not null,
  raw_items jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table if exists public.content_generation_artifacts
  add column if not exists platform_post_id text,
  add column if not exists permalink text,
  add column if not exists posted_at text,
  add column if not exists last_error text;
