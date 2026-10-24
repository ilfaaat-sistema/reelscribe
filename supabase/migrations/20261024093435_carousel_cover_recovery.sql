-- Совместимое расширение: старые RPC и ключ кэша сохраняются; готовые строки не меняются.
alter table public.carousel_cover_translations
  add column lease_expires_at timestamptz,
  add column reason_code text,
  add column source_kind text;
alter table public.carousel_cover_translations drop constraint carousel_cover_translations_status_check;
alter table public.carousel_cover_translations add constraint carousel_cover_translations_status_check
  check (status in ('running','ready','not_needed','no_text','unreadable','image_unavailable','error'));
alter table public.carousel_cover_translations add constraint carousel_cover_not_needed_text
  check (status <> 'not_needed' or (coalesce(length(source_text),0)>0 and coalesce(length(text_ru),0)=0));
-- Срок legacy claim выводится из исходной даты, расход и история не меняются.
update public.carousel_cover_translations set lease_expires_at=created_at+interval '600 seconds'
  where slot_held and lease_expires_at is null;

create or replace function public.carousel_cover_daily_limit(p_at timestamptz)
returns integer language sql stable strict security invoker set search_path = '' as $$
  select case when (p_at at time zone 'Europe/Moscow')::date in (date '2026-10-23',date '2026-10-24')
    then 2500 else 500 end;
$$;

-- Возврат слота не доказывает завершённый ответ провайдера и не возвращает попытку.
create function public.reap_carousel_cover_leases()
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_count integer;
begin
  perform 1 from public.carousel_cover_budget where singleton for update;
  update public.carousel_cover_translations set status='error',slot_held=false,
    reason_code='lease_expired_unknown',finished_at=clock_timestamp()
    where slot_held and status in ('running','error')
      and coalesce(lease_expires_at,created_at+interval '600 seconds') <= clock_timestamp();
  get diagnostics v_count = row_count;
  return jsonb_build_object('status','ok','reaped',v_count);
end;
$$;

create or replace function public.carousel_cover_budget_state()
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_budget public.carousel_cover_budget%rowtype;
  v_now timestamptz;
  v_limit integer;
  v_start timestamptz;
  v_reset timestamptz;
  v_reset_iso text;
  v_today bigint;
  v_limited boolean;
  v_active integer;
begin
  -- Это только чтение. Окончательное решение принимает reserve под общей блокировкой.
  select * into strict v_budget from public.carousel_cover_budget where singleton;
  v_now := clock_timestamp();
  v_limit := public.carousel_cover_daily_limit(v_now);
  select start_at,reset_at into v_start,v_reset from public.carousel_cover_day_window(v_now);
  select count(*) into v_today from public.carousel_cover_translations
    where claim_token is not null and created_at >= v_start and created_at < v_reset;
  v_limited := case when v_budget.mode='pilot' then v_budget.attempts >= 20 else v_today >= v_limit end;
  select count(*) into v_active from public.carousel_cover_translations where slot_held;
  v_reset_iso := to_char(v_reset at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"');
  return jsonb_build_object('status',case when v_limited then 'limit_reached' else 'available' end,
    'mode',v_budget.mode,'pilot_limit',20,'daily_limit',v_limit,'reset_at',v_reset_iso,
    'retry_after',case when v_limited and v_budget.mode='live' then v_reset_iso else null end,
    'attempts',v_budget.attempts,'daily_attempts',v_today,'active_slots',v_active,'available_slots',greatest(0,2-v_active));
end;
$$;

create or replace function public.reserve_carousel_cover(p_id text,p_version text,p_model text,p_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_budget public.carousel_cover_budget%rowtype;
  v_row public.carousel_cover_translations%rowtype;
  v_now timestamptz;
  v_start timestamptz;
  v_reset timestamptz;
  v_today bigint;
  v_limit integer;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_version is null or length(p_version) not between 1 and 80
     or p_model is null or length(p_model) not between 1 and 200 or p_hash is null or p_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'invalid_cover_request' using errcode='22023';
  end if;
  select * into strict v_budget from public.carousel_cover_budget where singleton for update;
  perform public.reap_carousel_cover_leases();
  select * into v_row from public.carousel_cover_translations where media_id=p_id and prompt_version=p_version;
  if found then
    return jsonb_build_object('status',v_row.status,'source_text',v_row.source_text,'text_ru',v_row.text_ru,'reason_code',v_row.reason_code,'attempt_recorded',v_row.claim_token is not null,
      'retry_after',case when v_row.reason_code='provider_rate_limit' then v_row.usage->>'retry_after' else null end);
  end if;
  -- now() фиксирует начало транзакции: ожидающий lock запрос мог уже пересечь полночь.
  v_now := clock_timestamp();
  v_limit := public.carousel_cover_daily_limit(v_now);
  if v_budget.mode='pilot' and v_budget.attempts >= 20 then
    return jsonb_build_object('status','limit_reached','retry_after',null);
  end if;
  if v_budget.mode='live' then
    select start_at,reset_at into v_start,v_reset from public.carousel_cover_day_window(v_now);
    select count(*) into v_today from public.carousel_cover_translations
      where claim_token is not null and created_at >= v_start and created_at < v_reset;
    if v_today >= v_limit then
      return jsonb_build_object('status','limit_reached','retry_after',
        to_char(v_reset at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'));
    end if;
  end if;
  if (select count(*) from public.carousel_cover_translations where slot_held)>=2 then
    return jsonb_build_object('status','busy');
  end if;
  update public.carousel_cover_budget set attempts=attempts+1 where singleton;
  insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,slot_held,model,image_hash,created_at,lease_expires_at)
    values(p_id,p_version,'running',gen_random_uuid(),true,p_model,p_hash,v_now,v_now+interval '600 seconds') returning * into v_row;
  return jsonb_build_object('status','running','claim_token',v_row.claim_token,'attempt_recorded',true);
end;
$$;

create or replace function public.finish_carousel_cover(p_id text,p_version text,p_token uuid,p_status text,
  p_source text,p_text text,p_usage jsonb,p_duration_ms integer,p_uncertain boolean default false)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_row public.carousel_cover_translations%rowtype;
begin
  if p_status is null or p_status not in ('ready','not_needed','no_text','unreadable','error') or p_token is null
     or p_uncertain is null or (p_uncertain and p_status<>'error') then
    raise exception 'invalid_cover_result' using errcode='22023';
  end if;
  perform 1 from public.carousel_cover_budget where singleton for update;
  update public.carousel_cover_translations set status=p_status,
    source_text=case when p_status in ('ready','not_needed') then p_source else null end,
    text_ru=case when p_status='ready' then p_text when p_status='not_needed' then '' else null end,
    usage=coalesce(p_usage,'{}'::jsonb),duration_ms=p_duration_ms,finished_at=clock_timestamp(),slot_held=p_uncertain,
    source_kind=case when p_usage->>'source_kind' in ('media_url','thumbnail_url','public_first_cover') then p_usage->>'source_kind' else null end,
    reason_code=case when p_uncertain then 'provider_outcome_unknown' when p_status='error' and p_usage->>'provider_status'='429'
      and p_usage->>'provider_response_complete'='true' then 'provider_rate_limit'
      when p_status='error' then 'provider_error' else null end
    where media_id=p_id and prompt_version=p_version and claim_token=p_token and status='running'
      and coalesce(lease_expires_at,created_at+interval '600 seconds') > clock_timestamp()
    returning * into v_row;
  if not found then raise exception 'cover_claim_mismatch' using errcode='22023'; end if;
  return jsonb_build_object('status',v_row.status,'source_text',v_row.source_text,'text_ru',v_row.text_ru,'reason_code',v_row.reason_code,'attempt_recorded',true,
    'retry_after',case when v_row.reason_code='provider_rate_limit' then v_row.usage->>'retry_after' else null end);
end;
$$;

create table public.carousel_cover_recovery_events (
  media_id text primary key,
  original_created_at timestamptz not null,
  reason text not null check(length(reason) between 1 and 500),
  rearmed_at timestamptz not null default clock_timestamp()
);
alter table public.carousel_cover_recovery_events enable row level security;
revoke all on public.carousel_cover_recovery_events from public,anon,authenticated;
grant select,insert on public.carousel_cover_recovery_events to service_role;

-- CAS и одноразовый архив сохраняют исходный claim, расход, даты и весь ответ.
create function public.rearm_carousel_cover(p_id text,p_expected_created_at timestamptz,p_reason text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_row public.carousel_cover_translations%rowtype;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_expected_created_at is null
    or p_reason is null or length(btrim(p_reason)) not between 1 and 500 then
    raise exception 'invalid_cover_rearm' using errcode='22023';
  end if;
  perform 1 from public.carousel_cover_budget where singleton for update;
  select * into v_row from public.carousel_cover_translations
    where media_id=p_id and prompt_version='cover-v1' for update;
  if not found or v_row.created_at<>p_expected_created_at then
    raise exception 'cover_rearm_mismatch' using errcode='22023';
  end if;
  if exists(select 1 from public.carousel_cover_translations where media_id=p_id
    and prompt_version like 'cover-recovery-audit-%') then
    raise exception 'cover_rearm_already_used' using errcode='22023';
  end if;
  if v_row.slot_held or not ((v_row.status='image_unavailable' and v_row.claim_token is null)
    or v_row.status='error') then
    raise exception 'cover_rearm_forbidden' using errcode='22023';
  end if;
  update public.carousel_cover_translations set prompt_version='cover-recovery-audit-'||gen_random_uuid()::text
    where media_id=p_id and prompt_version='cover-v1';
  -- Причина оператора хранится отдельно от неизменённого снимка исходной строки.
  insert into public.carousel_cover_recovery_events(media_id,original_created_at,reason)
    values(p_id,p_expected_created_at,p_reason);
  return jsonb_build_object('status','rearmed');
end;
$$;
revoke all on function public.reap_carousel_cover_leases() from public,anon,authenticated;
revoke all on function public.rearm_carousel_cover(text,timestamptz,text) from public,anon,authenticated;
grant execute on function public.reap_carousel_cover_leases() to service_role;
grant execute on function public.rearm_carousel_cover(text,timestamptz,text) to service_role;

-- Старый двухаргументный RPC остаётся доступен старому приложению без изменения сигнатуры.
create function public.unavailable_carousel_cover_source(p_id text,p_version text,p_reason text,p_source_kind text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_result jsonb; v_row public.carousel_cover_translations%rowtype;
begin
  if p_reason is null or p_reason not in ('expired_signature','permanent_missing','timeout','unsupported','http_error','image_unavailable')
     or (p_source_kind is not null and p_source_kind not in ('media_url','thumbnail_url','public_first_cover')) then
    raise exception 'invalid_cover_source' using errcode='22023';
  end if;
  v_result := public.unavailable_carousel_cover(p_id,p_version);
  update public.carousel_cover_translations set reason_code=p_reason,source_kind=p_source_kind
    where media_id=p_id and prompt_version=p_version and status='image_unavailable' and claim_token is null;
  select * into strict v_row from public.carousel_cover_translations where media_id=p_id and prompt_version=p_version;
  return jsonb_build_object('status',v_row.status,'source_text',v_row.source_text,'text_ru',v_row.text_ru,
    'reason_code',v_row.reason_code,'attempt_recorded',v_row.claim_token is not null,
      'retry_after',case when v_row.reason_code='provider_rate_limit' then v_row.usage->>'retry_after' else null end);
end;
$$;
revoke all on function public.unavailable_carousel_cover_source(text,text,text,text) from public,anon,authenticated;
grant execute on function public.unavailable_carousel_cover_source(text,text,text,text) to service_role;

-- Только операторский manifest после просмотра точного изображения, без модельного claim.
create function public.resolve_carousel_cover_known(p_id text,p_expected_created_at timestamptz,
  p_status text,p_source_text text,p_image_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_row public.carousel_cover_translations%rowtype;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_expected_created_at is null
    or p_status is null or p_status not in ('not_needed','no_text')
    or p_image_hash is null or p_image_hash !~ '^[a-f0-9]{64}$'
    or p_source_text is null or length(p_source_text)>6000
    or (p_status='not_needed' and length(btrim(p_source_text))=0)
    or (p_status='no_text' and p_source_text<>'') then
    raise exception 'invalid_cover_resolution' using errcode='22023';
  end if;
  perform 1 from public.carousel_cover_budget where singleton for update;
  select * into v_row from public.carousel_cover_translations
    where media_id=p_id and prompt_version='cover-v1' for update;
  if not found or v_row.created_at<>p_expected_created_at then
    raise exception 'cover_resolution_mismatch' using errcode='22023';
  end if;
  if v_row.status<>'image_unavailable' or v_row.claim_token is not null or v_row.slot_held
    or exists(select 1 from public.carousel_cover_recovery_events where media_id=p_id) then
    raise exception 'cover_resolution_forbidden' using errcode='22023';
  end if;
  update public.carousel_cover_translations set prompt_version='cover-recovery-audit-'||gen_random_uuid()::text
    where media_id=p_id and prompt_version='cover-v1';
  insert into public.carousel_cover_recovery_events(media_id,original_created_at,reason)
    values(p_id,p_expected_created_at,'operator_visual_review');
  insert into public.carousel_cover_translations(media_id,prompt_version,status,source_text,text_ru,
    image_hash,usage,finished_at,source_kind)
    values(p_id,'cover-v1',p_status,nullif(p_source_text,''),'',p_image_hash,
      '{"provenance":"operator_visual_review"}'::jsonb,clock_timestamp(),'public_first_cover');
  return jsonb_build_object('status',p_status,'source_text',nullif(p_source_text,''),'text_ru','',
    'attempt_recorded',false);
end;
$$;
revoke all on function public.resolve_carousel_cover_known(text,timestamptz,text,text,text) from public,anon,authenticated;
grant execute on function public.resolve_carousel_cover_known(text,timestamptz,text,text,text) to service_role;


-- Закрытый краткоживущий источник после ограниченного официального Meta-refresh.
-- Исторические snapshots и готовый кэш переводов не перезаписываются.
create table public.carousel_cover_source_overrides (
  media_id text primary key check (media_id ~ '^[0-9]{1,40}$'),
  media_url text not null check (length(media_url) between 9 and 8192 and media_url ~ '^https://[^[:space:]]+$'),
  thumbnail_url text check (thumbnail_url is null or
    (length(thumbnail_url) between 9 and 8192 and thumbnail_url ~ '^https://[^[:space:]]+$')),
  refreshed_at timestamptz not null default clock_timestamp(),
  expires_at timestamptz not null,
  provenance text not null default 'meta_graph_refresh' check (provenance='meta_graph_refresh'),
  check (expires_at>refreshed_at)
);
alter table public.carousel_cover_source_overrides enable row level security;
revoke all on public.carousel_cover_source_overrides from public,anon,authenticated;
grant select,insert,update on public.carousel_cover_source_overrides to service_role;
