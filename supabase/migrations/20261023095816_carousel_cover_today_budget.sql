-- Согласованное разовое повышение только на 23.10.2026 по Москве.
-- С 24.10.2026 00:00 МСК (23.10.2026 21:00 UTC) снова действует 500.
-- Счётчики, записи, кэш, пилотные 20 и два глобальных слота сохраняются.
create function public.carousel_cover_daily_limit(p_at timestamptz)
returns integer language sql stable strict security invoker set search_path = '' as $$
  select case when (p_at at time zone 'Europe/Moscow')::date = date '2026-10-23'
    then 2500 else 500 end;
$$;
revoke all on function public.carousel_cover_daily_limit(timestamptz) from public,anon,authenticated;
grant execute on function public.carousel_cover_daily_limit(timestamptz) to service_role;

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
begin
  -- Это только чтение. Окончательное решение принимает reserve под общей блокировкой.
  select * into strict v_budget from public.carousel_cover_budget where singleton;
  v_now := clock_timestamp();
  v_limit := public.carousel_cover_daily_limit(v_now);
  select start_at,reset_at into v_start,v_reset from public.carousel_cover_day_window(v_now);
  select count(*) into v_today from public.carousel_cover_translations
    where claim_token is not null and created_at >= v_start and created_at < v_reset;
  v_limited := case when v_budget.mode='pilot' then v_budget.attempts >= 20 else v_today >= v_limit end;
  v_reset_iso := to_char(v_reset at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"');
  return jsonb_build_object('status',case when v_limited then 'limit_reached' else 'available' end,
    'mode',v_budget.mode,'pilot_limit',20,'daily_limit',v_limit,'reset_at',v_reset_iso,
    'retry_after',case when v_limited and v_budget.mode='live' then v_reset_iso else null end,
    'attempts',v_budget.attempts,'daily_attempts',v_today);
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
  select * into v_row from public.carousel_cover_translations where media_id=p_id and prompt_version=p_version;
  if found then
    return jsonb_build_object('status',v_row.status,'source_text',v_row.source_text,'text_ru',v_row.text_ru);
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
  insert into public.carousel_cover_translations(media_id,prompt_version,status,claim_token,slot_held,model,image_hash,created_at)
    values(p_id,p_version,'running',gen_random_uuid(),true,p_model,p_hash,v_now) returning * into v_row;
  return jsonb_build_object('status','running','claim_token',v_row.claim_token);
end;
$$;

revoke all on function public.carousel_cover_budget_state() from public,anon,authenticated;
revoke all on function public.reserve_carousel_cover(text,text,text,text) from public,anon,authenticated;
grant execute on function public.carousel_cover_budget_state() to service_role;
grant execute on function public.reserve_carousel_cover(text,text,text,text) to service_role;
