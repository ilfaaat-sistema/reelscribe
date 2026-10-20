-- Серверный кэш подписи; публичный клиент не может записать исходник/перевод или сбросить лимит.
create table public.carousel_caption_translations (
  instagram_media_id text not null check (instagram_media_id ~ '^[0-9]{1,40}$'),
  source_hash text not null check (source_hash ~ '^[0-9a-f]{64}$'),
  translation text not null check (length(btrim(translation)) > 0 and length(translation) <= 60000),
  source_language text check (source_language ~ '^[a-z-]{2,12}$'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (instagram_media_id, source_hash)
);

create table public.carousel_caption_translation_attempts (
  id uuid primary key,
  instagram_media_id text not null check (instagram_media_id ~ '^[0-9]{1,40}$'),
  source_hash text not null check (source_hash ~ '^[0-9a-f]{64}$'),
  started_at timestamptz not null,
  lease_until timestamptz not null,
  finished_at timestamptz,
  outcome text check (outcome in ('ready','failed','quota','source_changed')),
  check (lease_until > started_at),
  check ((finished_at is null) = (outcome is null))
);
create index carousel_caption_attempts_started on public.carousel_caption_translation_attempts(started_at);
create index carousel_caption_attempts_pending on public.carousel_caption_translation_attempts
  (instagram_media_id,source_hash,lease_until) where finished_at is null;

alter table public.carousel_caption_translations enable row level security;
alter table public.carousel_caption_translation_attempts enable row level security;
revoke all on public.carousel_caption_translations from public,anon,authenticated;
revoke all on public.carousel_caption_translation_attempts from public,anon,authenticated;
grant select,insert,update,delete on public.carousel_caption_translations to service_role;
grant select,insert,update,delete on public.carousel_caption_translation_attempts to service_role;

create function public.reserve_carousel_caption_translation(p_id text,p_source_hash text,p_attempt_id uuid)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
  v_now timestamptz;
  v_row public.carousel_caption_translations%rowtype;
  v_wait integer;
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
  if (select count(*) from public.carousel_caption_translation_attempts
      where started_at>v_now-interval '1 hour')>=12 then
    select greatest(1,ceil(extract(epoch from min(started_at)+interval '1 hour'-v_now))::integer)
      into v_wait from public.carousel_caption_translation_attempts where started_at>v_now-interval '1 hour';
    return jsonb_build_object('state','limited','retry_after',v_wait);
  end if;
  if (select count(*) from public.carousel_caption_translation_attempts
      where finished_at is null and lease_until>v_now)>=2 then
    select greatest(1,ceil(extract(epoch from min(lease_until)-v_now))::integer)
      into v_wait from public.carousel_caption_translation_attempts where finished_at is null and lease_until>v_now;
    return jsonb_build_object('state','limited','retry_after',v_wait);
  end if;
  -- Просроченная попытка остаётся в часовом счётчике, новая получает новый token.
  -- Старые завершённые/истёкшие строки за пределами суток больше не нужны для лимита.
  delete from public.carousel_caption_translation_attempts
    where started_at<v_now-interval '1 day' and (finished_at is not null or lease_until<=v_now);
  insert into public.carousel_caption_translation_attempts(id,instagram_media_id,source_hash,started_at,lease_until)
    values(p_attempt_id,p_id,p_source_hash,v_now,v_now+interval '15 minutes');
  return jsonb_build_object('state','reserved','attempt_id',p_attempt_id);
end;
$$;

create function public.finish_carousel_caption_translation(
  p_id text,p_source_hash text,p_attempt_id uuid,p_translation text,p_source_language text,p_outcome text
) returns jsonb language plpgsql security invoker set search_path='' as $$
declare
  v_now timestamptz;
  v_attempt public.carousel_caption_translation_attempts%rowtype;
  v_row public.carousel_caption_translations%rowtype;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_source_hash is null
     or p_source_hash !~ '^[0-9a-f]{64}$' or p_attempt_id is null
     or p_outcome is null or p_outcome not in ('ready','failed','quota','source_changed') then
    raise exception 'invalid_translation_finish' using errcode='22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('reelscribe.carousel_caption_translation',0));
  v_now := clock_timestamp();
  select * into v_attempt from public.carousel_caption_translation_attempts
    where id=p_attempt_id and instagram_media_id=p_id and source_hash=p_source_hash for update;
  if not found or v_attempt.finished_at is not null or v_attempt.lease_until<=v_now then
    return jsonb_build_object('state','stale');
  end if;
  -- Проверка срока и token под тем же lock ограждает результат прежнего владельца.
  if exists(select 1 from public.carousel_caption_translation_attempts
    where instagram_media_id=p_id and source_hash=p_source_hash and id<>p_attempt_id
      and started_at>=v_attempt.started_at) then
    return jsonb_build_object('state','stale');
  end if;
  if p_outcome='ready' then
    if p_translation is null or length(btrim(p_translation))=0 or length(p_translation)>60000
       or (p_source_language is not null and p_source_language !~ '^[a-z-]{2,12}$') then
      raise exception 'invalid_translation_result' using errcode='22023';
    end if;
    insert into public.carousel_caption_translations(instagram_media_id,source_hash,translation,source_language)
      values(p_id,p_source_hash,p_translation,p_source_language) on conflict do nothing;
    select * into v_row from public.carousel_caption_translations where instagram_media_id=p_id and source_hash=p_source_hash;
  end if;
  update public.carousel_caption_translation_attempts set finished_at=v_now,outcome=p_outcome where id=p_attempt_id;
  if p_outcome='ready' then
    return jsonb_build_object('state','ready','translation',v_row.translation,'source_language',v_row.source_language);
  end if;
  return jsonb_build_object('state','finished');
end;
$$;

revoke all on function public.reserve_carousel_caption_translation(text,text,uuid) from public,anon,authenticated;
revoke all on function public.finish_carousel_caption_translation(text,text,uuid,text,text,text) from public,anon,authenticated;
grant execute on function public.reserve_carousel_caption_translation(text,text,uuid) to service_role;
grant execute on function public.finish_carousel_caption_translation(text,text,uuid,text,text,text) to service_role;
