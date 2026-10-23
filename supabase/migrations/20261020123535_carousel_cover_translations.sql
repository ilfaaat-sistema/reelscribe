-- Пилот: 20 попыток всего. Live: 500 попыток за календарные сутки Europe/Moscow.
create table public.carousel_cover_budget (
  singleton boolean primary key default true check (singleton),
  attempts bigint not null default 0 check (attempts >= 0),
  mode text not null default 'pilot' check (mode in ('pilot','live'))
);
insert into public.carousel_cover_budget(singleton) values(true);
create table public.carousel_cover_translations (
  media_id text not null check (media_id ~ '^[0-9]{1,40}$'),
  prompt_version text not null check (length(prompt_version) between 1 and 80),
  status text not null check (status in ('running','ready','no_text','unreadable','image_unavailable','error')),
  source_text text check (length(source_text) <= 6000),
  text_ru text check (length(text_ru) <= 6000),
  claim_token uuid,
  slot_held boolean not null default false,
  image_hash text check (image_hash ~ '^[a-f0-9]{64}$'),
  model text,
  usage jsonb not null default '{}'::jsonb check (jsonb_typeof(usage)='object' and octet_length(usage::text)<=4096),
  duration_ms integer check (duration_ms >= 0),
  created_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  primary key(media_id,prompt_version),
  check (status<>'ready' or (coalesce(length(source_text),0)>0 and coalesce(length(text_ru),0)>0)),
  check (not slot_held or claim_token is not null)
);
create index carousel_cover_attempts_by_time on public.carousel_cover_translations(created_at)
  where claim_token is not null;
alter table public.carousel_cover_budget enable row level security;
alter table public.carousel_cover_translations enable row level security;
revoke all on public.carousel_cover_budget,public.carousel_cover_translations from public,anon,authenticated;
grant select,insert,update on public.carousel_cover_budget,public.carousel_cover_translations to service_role;

-- Границы дня считаются в Москве, результат не зависит от TimeZone SQL-сессии.
-- Helper не меняет бюджет; резервирование всегда передаёт фактическое серверное время.
create function public.carousel_cover_day_window(p_at timestamptz)
returns table(start_at timestamptz, reset_at timestamptz)
language sql stable strict security invoker set search_path = '' as $$
  select date_trunc('day', p_at at time zone 'Europe/Moscow') at time zone 'Europe/Moscow',
    (date_trunc('day', p_at at time zone 'Europe/Moscow') + interval '1 day') at time zone 'Europe/Moscow';
$$;

create function public.carousel_cover_budget_state()
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_budget public.carousel_cover_budget%rowtype;
  v_start timestamptz;
  v_reset timestamptz;
  v_reset_iso text;
  v_today bigint;
  v_limited boolean;
begin
  -- Это только чтение. Окончательное решение принимает reserve под общей блокировкой.
  select * into strict v_budget from public.carousel_cover_budget where singleton;
  select start_at,reset_at into v_start,v_reset from public.carousel_cover_day_window(clock_timestamp());
  select count(*) into v_today from public.carousel_cover_translations
    where claim_token is not null and created_at >= v_start and created_at < v_reset;
  v_limited := case when v_budget.mode='pilot' then v_budget.attempts >= 20 else v_today >= 500 end;
  v_reset_iso := to_char(v_reset at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"');
  return jsonb_build_object('status',case when v_limited then 'limit_reached' else 'available' end,
    'mode',v_budget.mode,'pilot_limit',20,'daily_limit',500,'reset_at',v_reset_iso,
    'retry_after',case when v_limited and v_budget.mode='live' then v_reset_iso else null end,
    'attempts',v_budget.attempts,'daily_attempts',v_today);
end;
$$;

-- Переключение режима не сбрасывает lifetime, дневные попытки, версии или результаты.
create function public.set_carousel_cover_mode(p_mode text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
begin
  if p_mode is null or p_mode not in ('pilot','live') then
    raise exception 'invalid_cover_mode' using errcode='22023';
  end if;
  perform 1 from public.carousel_cover_budget where singleton for update;
  update public.carousel_cover_budget set mode=p_mode where singleton;
  return public.carousel_cover_budget_state();
end;
$$;

-- Все операции, способные занять слот, и смена режима используют одну строку бюджета.
-- SECURITY INVOKER: RPC и таблицы доступны только service_role, без обхода RLS для других ролей.
create function public.reserve_carousel_cover(p_id text,p_version text,p_model text,p_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_budget public.carousel_cover_budget%rowtype;
  v_row public.carousel_cover_translations%rowtype;
  v_now timestamptz;
  v_start timestamptz;
  v_reset timestamptz;
  v_today bigint;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_version is null or length(p_version) not between 1 and 80
     or p_model is null or length(p_model) not between 1 and 200 or p_hash is null or p_hash !~ '^[a-f0-9]{64}$' then
    raise exception 'invalid_cover_request' using errcode='22023';
  end if;
  select * into strict v_budget from public.carousel_cover_budget where singleton for update;
  select * into v_row from public.carousel_cover_translations where media_id=p_id and prompt_version=p_version;
  if found then
    return jsonb_build_object('status',v_row.status,'source_text',v_row.source_text,'text_ru',v_row.text_ru);
  end if;
  -- now() фиксирует начало транзакции: ожидающий lock запрос мог уже пересечь полночь.
  v_now := clock_timestamp();
  if v_budget.mode='pilot' and v_budget.attempts >= 20 then
    return jsonb_build_object('status','limit_reached','retry_after',null);
  end if;
  if v_budget.mode='live' then
    select start_at,reset_at into v_start,v_reset from public.carousel_cover_day_window(v_now);
    select count(*) into v_today from public.carousel_cover_translations
      where claim_token is not null and created_at >= v_start and created_at < v_reset;
    if v_today >= 500 then
      return jsonb_build_object('status','limit_reached','retry_after',
        to_char(v_reset at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'));
    end if;
  end if;
  if (select count(*) from public.carousel_cover_translations where slot_held)>=2 then
    return jsonb_build_object('status','busy');
  end if;
  update public.carousel_cover_budget set attempts=attempts+1 where singleton;
  insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,slot_held,model,image_hash,created_at)
    values(p_id,p_version,'running',gen_random_uuid(),true,p_model,p_hash,v_now) returning * into v_row;
  return jsonb_build_object('status','running','claim_token',v_row.claim_token);
end;
$$;

create function public.unavailable_carousel_cover(p_id text,p_version text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_row public.carousel_cover_translations%rowtype;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_version is null or length(p_version) not between 1 and 80 then
    raise exception 'invalid_cover_request' using errcode='22023';
  end if;
  perform 1 from public.carousel_cover_budget where singleton for update;
  insert into public.carousel_cover_translations(media_id,prompt_version,status,finished_at)
    values(p_id,p_version,'image_unavailable',clock_timestamp()) on conflict do nothing;
  select * into strict v_row from public.carousel_cover_translations where media_id=p_id and prompt_version=p_version;
  return jsonb_build_object('status',v_row.status,'source_text',v_row.source_text,'text_ru',v_row.text_ru);
end;
$$;

create function public.finish_carousel_cover(p_id text,p_version text,p_token uuid,p_status text,
  p_source text,p_text text,p_usage jsonb,p_duration_ms integer,p_uncertain boolean default false)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_row public.carousel_cover_translations%rowtype;
begin
  if p_status is null or p_status not in ('ready','no_text','unreadable','error') or p_token is null
     or p_uncertain is null or (p_uncertain and p_status<>'error') then
    raise exception 'invalid_cover_result' using errcode='22023';
  end if;
  perform 1 from public.carousel_cover_budget where singleton for update;
  update public.carousel_cover_translations set status=p_status,
    source_text=case when p_status='ready' then p_source else null end,
    text_ru=case when p_status='ready' then p_text else null end,
    usage=coalesce(p_usage,'{}'::jsonb),duration_ms=p_duration_ms,finished_at=clock_timestamp(),slot_held=p_uncertain
    where media_id=p_id and prompt_version=p_version and claim_token=p_token and status='running'
    returning * into v_row;
  if not found then raise exception 'cover_claim_mismatch' using errcode='22023'; end if;
  return jsonb_build_object('status',v_row.status,'source_text',v_row.source_text,'text_ru',v_row.text_ru);
end;
$$;
revoke all on function public.reserve_carousel_cover(text,text,text,text) from public,anon,authenticated;
revoke all on function public.unavailable_carousel_cover(text,text) from public,anon,authenticated;
revoke all on function public.finish_carousel_cover(text,text,uuid,text,text,text,jsonb,integer,boolean) from public,anon,authenticated;
grant execute on function public.reserve_carousel_cover(text,text,text,text) to service_role;
grant execute on function public.unavailable_carousel_cover(text,text) to service_role;
grant execute on function public.finish_carousel_cover(text,text,uuid,text,text,text,jsonb,integer,boolean) to service_role;

revoke all on function public.carousel_cover_day_window(timestamptz) from public,anon,authenticated;
revoke all on function public.carousel_cover_budget_state() from public,anon,authenticated;
revoke all on function public.set_carousel_cover_mode(text) from public,anon,authenticated;
grant execute on function public.carousel_cover_day_window(timestamptz) to service_role;
grant execute on function public.carousel_cover_budget_state() to service_role;
grant execute on function public.set_carousel_cover_mode(text) to service_role;
