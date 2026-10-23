-- Суточный бюджет подписей по МСК и атомарная защита фоновой очереди от платного повтора.
-- Существующие кэш, история, ручной RPC, singleflight и fencing сохраняются.
create index carousel_caption_attempts_key_started
  on public.carousel_caption_translation_attempts(instagram_media_id,source_hash,started_at desc);

create or replace function public.reserve_carousel_caption_translation(p_id text,p_source_hash text,p_attempt_id uuid)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
  v_now timestamptz;
  v_row public.carousel_caption_translations%rowtype;
  v_wait integer;
  v_day_start timestamptz;
  v_next_day timestamptz;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_source_hash is null
     or p_source_hash !~ '^[0-9a-f]{64}$' or p_attempt_id is null then
    raise exception 'invalid_translation_request' using errcode='22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('reelscribe.carousel_caption_translation',0));
  v_now := clock_timestamp();
  v_day_start := date_trunc('day',v_now at time zone 'Europe/Moscow') at time zone 'Europe/Moscow';
  v_next_day := (date_trunc('day',v_now at time zone 'Europe/Moscow')+interval '1 day') at time zone 'Europe/Moscow';
  select * into v_row from public.carousel_caption_translations
    where instagram_media_id=p_id and source_hash=p_source_hash;
  if found then
    return jsonb_build_object('state','ready','translation',v_row.translation,'source_language',v_row.source_language);
  end if;
  if exists(select 1 from public.carousel_caption_translation_attempts
    where instagram_media_id=p_id and source_hash=p_source_hash and finished_at is null and lease_until>v_now) then
    return jsonb_build_object('state','in_progress');
  end if;
  -- Каждая зарезервированная попытка расходует календарный бюджет, независимо от исхода.
  if (select count(*) from public.carousel_caption_translation_attempts
      where started_at>=v_day_start and started_at<v_next_day)>=500 then
    v_wait := greatest(1,ceil(extract(epoch from v_next_day-v_now))::integer);
    return jsonb_build_object('state','limited','retry_after',v_wait);
  end if;
  if (select count(*) from public.carousel_caption_translation_attempts
      where finished_at is null and lease_until>v_now)>=2 then
    select greatest(1,ceil(extract(epoch from min(lease_until)-v_now))::integer)
      into v_wait from public.carousel_caption_translation_attempts where finished_at is null and lease_until>v_now;
    return jsonb_build_object('state','limited','retry_after',v_wait);
  end if;
  -- История не удаляется: автоматический перевод не повторяет ни одну прежнюю попытку ID+hash.
  insert into public.carousel_caption_translation_attempts(id,instagram_media_id,source_hash,started_at,lease_until)
    values(p_attempt_id,p_id,p_source_hash,v_now,v_now+interval '15 minutes');
  return jsonb_build_object('state','reserved','attempt_id',p_attempt_id);
end;
$$;

create function public.reserve_carousel_caption_translation_auto(p_id text,p_source_hash text,p_attempt_id uuid)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
  v_now timestamptz;
  v_row public.carousel_caption_translations%rowtype;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_source_hash is null
     or p_source_hash !~ '^[0-9a-f]{64}$' or p_attempt_id is null then
    raise exception 'invalid_translation_request' using errcode='22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('reelscribe.carousel_caption_translation',0));
  v_now := clock_timestamp();
  select * into v_row from public.carousel_caption_translations
    where instagram_media_id=p_id and source_hash=p_source_hash;
  if found then
    return jsonb_build_object('state','ready','translation',v_row.translation,'source_language',v_row.source_language);
  end if;
  if exists(select 1 from public.carousel_caption_translation_attempts
    where instagram_media_id=p_id and source_hash=p_source_hash and finished_at is null and lease_until>v_now) then
    return jsonb_build_object('state','in_progress');
  end if;
  if exists(select 1 from public.carousel_caption_translation_attempts
    where instagram_media_id=p_id and source_hash=p_source_hash) then
    return jsonb_build_object('state','previous_attempt');
  end if;
  -- Тот же reentrant transaction lock удерживается до вставки внутри ручного RPC.
  return public.reserve_carousel_caption_translation(p_id,p_source_hash,p_attempt_id);
end;
$$;

revoke all on function public.reserve_carousel_caption_translation_auto(text,text,uuid) from public,anon,authenticated;
grant execute on function public.reserve_carousel_caption_translation_auto(text,text,uuid) to service_role;
