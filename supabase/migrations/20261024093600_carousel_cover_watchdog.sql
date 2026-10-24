-- Независимый watchdog очереди обложек. По умолчанию контроллер и cron выключены.
-- Эта миграция не меняет сторонние cron, очередь, строки переводов или бюджет.
create table public.carousel_cover_background_control (
  singleton boolean primary key default true check (singleton),
  enabled boolean not null default false,
  mode text not null default 'continuous' check (mode = 'continuous'),
  last_dispatch_at timestamptz,
  not_before timestamptz,
  heartbeat timestamptz,
  gate_token uuid,
  last_seen_source_refs jsonb not null default '{}'::jsonb,
  summary jsonb not null default '{}'::jsonb,
  reason text not null default 'disabled',
  check (jsonb_typeof(last_seen_source_refs) = 'object'),
  check (jsonb_typeof(summary) = 'object')
);
alter table public.carousel_cover_background_control enable row level security;
revoke all on public.carousel_cover_background_control from public, anon, authenticated;
grant select, insert, update on public.carousel_cover_background_control to service_role;
insert into public.carousel_cover_background_control(singleton) values(true);

create function public.carousel_cover_background_status()
returns jsonb language sql security invoker set search_path = '' as $$
  select jsonb_build_object('enabled', enabled, 'mode', mode, 'last_dispatch_at', last_dispatch_at,
    'not_before', not_before, 'heartbeat', heartbeat, 'reason', reason,
    'summary', summary, 'last_seen_source_refs', last_seen_source_refs)
  from public.carousel_cover_background_control where singleton;
$$;

create function public.carousel_cover_background_claim_tick()
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  row_control public.carousel_cover_background_control;
  current_at timestamptz;
  token uuid;
begin
  select * into row_control from public.carousel_cover_background_control where singleton for update;
  current_at := clock_timestamp(); -- Только после row lock, включая конкурентные публичные tick.
  if not found or not row_control.enabled or row_control.mode <> 'continuous' then
    return jsonb_build_object('claimed', false, 'reason', 'disabled');
  end if;
  if row_control.not_before is not null and row_control.not_before > current_at then
    return jsonb_build_object('claimed', false, 'reason', 'dispatch_gate');
  end if;
  token := gen_random_uuid();
  -- Gate закрывается ДО внешнего HTTP. Потеря ответа не разрешает двойной dispatch.
  update public.carousel_cover_background_control set gate_token = token,
    not_before = current_at + interval '5 minutes', heartbeat = current_at
  where singleton;
  return jsonb_build_object('claimed', true, 'token', token);
end;
$$;

create function public.carousel_cover_background_finish_tick(
  p_token uuid, p_reason text, p_refs jsonb, p_summary jsonb
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  key_name text;
  scalar jsonb;
  current_at timestamptz;
begin
  if p_reason is null or p_reason not in ('disabled', 'repository_disabled', 'active_run', 'quota',
      'busy', 'paused', 'auth_failed', 'unknown_only', 'empty', 'dispatch_sent',
      'github_unavailable', 'watchdog_error', 'source_changed', 'work_pending') then
    raise exception 'Некорректная причина watchdog';
  end if;
  if p_refs is null or jsonb_typeof(p_refs) <> 'object' or p_summary is null or jsonb_typeof(p_summary) <> 'object' then
    raise exception 'Некорректная сводка watchdog';
  end if;
  for key_name, scalar in select key, value from jsonb_each(p_refs) loop
    if key_name not in ('archive7d', 'archive14d', 'live') or jsonb_typeof(scalar) <> 'string'
       or (scalar #>> '{}') !~ '^([0-9a-f]{40}|[0-9a-f]{64})$' then
      raise exception 'Некорректный SHA источника watchdog';
    end if;
  end loop;
  for key_name, scalar in select key, value from jsonb_each(p_summary) loop
    if key_name not in ('pending', 'running', 'unknown', 'terminal', 'remaining')
       or jsonb_typeof(scalar) <> 'number' or (scalar #>> '{}') !~ '^[0-9]{1,7}$' then
      raise exception 'Некорректный счётчик watchdog';
    end if;
  end loop;
  perform 1 from public.carousel_cover_background_control where singleton and gate_token = p_token for update;
  if not found then
    return jsonb_build_object('recorded', false);
  end if;
  current_at := clock_timestamp();
  update public.carousel_cover_background_control set reason = p_reason, heartbeat = current_at,
    last_dispatch_at = case when p_reason = 'dispatch_sent' then current_at else last_dispatch_at end,
    last_seen_source_refs = case when p_refs = '{}'::jsonb then last_seen_source_refs else p_refs end,
    summary = case when p_summary = '{}'::jsonb then summary else p_summary end,
    gate_token = null
  where singleton and gate_token = p_token;
  return jsonb_build_object('recorded', true);
end;
$$;

revoke all on function public.carousel_cover_background_status() from public, anon, authenticated;
revoke all on function public.carousel_cover_background_claim_tick() from public, anon, authenticated;
revoke all on function public.carousel_cover_background_finish_tick(uuid,text,jsonb,jsonb) from public, anon, authenticated;
grant execute on function public.carousel_cover_background_status() to service_role;
grant execute on function public.carousel_cover_background_claim_tick() to service_role;
grant execute on function public.carousel_cover_background_finish_tick(uuid,text,jsonb,jsonb) to service_role;

-- pg_cron и pg_net уже установлены в целевом проекте. Никаких секретов в URL/command.
do $$
declare own_job bigint;
begin
  select cron.schedule('carousel-cover-background-watchdog', '*/5 * * * *',
    $command$select net.http_post(
      url := 'https://reelscribe-ai.vercel.app/api/radar/carousel-cover-background/tick',
      headers := '{"Content-Type":"application/json"}'::jsonb,
      body := '{}'::jsonb,
      timeout_milliseconds := 120000
    );$command$) into own_job;
  -- Root включает только после появления router и приёмки схемного контракта.
  perform cron.alter_job(own_job, active := false);
end;
$$;
