-- Личные отметки каруселей. Instagram ID не связан с UUID таблицы reels.
create table public.carousel_selections (
  user_id uuid not null references auth.users(id) on delete cascade,
  instagram_media_id text not null check (instagram_media_id ~ '^[0-9]{1,40}$'),
  selected_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  removed_at timestamptz,
  revision bigint not null default 1 check (revision > 0),
  snapshot_version integer not null default 1 check (snapshot_version = 1),
  snapshot jsonb not null check (
    jsonb_typeof(snapshot) = 'object' and octet_length(snapshot::text) <= 65536
    and (snapshot->>'id' = instagram_media_id) is true
    and (snapshot->>'media_type' = 'CAROUSEL_ALBUM') is true
    and snapshot - array['id','username','permalink','shortcode','timestamp','caption','media_type',
      'media_url','thumbnail_url','likes','comments','views','followers','collected_at'] = '{}'::jsonb
    and (not (snapshot ? 'username') or jsonb_typeof(snapshot->'username') in ('string','null'))
    and (not (snapshot ? 'caption') or jsonb_typeof(snapshot->'caption') in ('string','null'))
    and (not (snapshot ? 'shortcode') or jsonb_typeof(snapshot->'shortcode') in ('string','null'))
    and (not (snapshot ? 'permalink') or jsonb_typeof(snapshot->'permalink') in ('string','null'))
    and (not (snapshot ? 'timestamp') or jsonb_typeof(snapshot->'timestamp') in ('string','null'))
  ),
  source_period text not null check (source_period in ('7d', '14d')),
  source_start_date date not null,
  source_end_date date not null,
  snapshot_at timestamptz not null default now(),
  primary key (user_id, instagram_media_id),
  check (source_start_date <= source_end_date)
);

create index carousel_selections_active_order
  on public.carousel_selections (user_id, selected_at desc, instagram_media_id)
  where removed_at is null;

alter table public.carousel_selections enable row level security;
revoke all on public.carousel_selections from public, anon, authenticated;
grant select on public.carousel_selections to authenticated;

create policy carousel_selections_select on public.carousel_selections
  for select to authenticated using (user_id = (select auth.uid()));
create policy carousel_selections_insert on public.carousel_selections
  for insert to authenticated with check (user_id = (select auth.uid()));
create policy carousel_selections_update on public.carousel_selections
  for update to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));
create policy carousel_selections_delete on public.carousel_selections
  for delete to authenticated using (user_id = (select auth.uid()));

-- Запись доступна только функциям; прямой REST таблицы открыт владельцу лишь на чтение.
-- SECURITY DEFINER проверяет auth.uid() и всегда ограничивает WHERE владельцем.
create function public.select_carousel(
  p_id text, p_snapshot jsonb, p_period text,
  p_start_date date, p_end_date date, p_expected_revision bigint
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_user uuid := auth.uid();
  v_row public.carousel_selections%rowtype;
  v_count bigint;
begin
  if v_user is null then raise exception 'authentication_required' using errcode = '28000'; end if;
  if p_id !~ '^[0-9]{1,40}$' or p_period not in ('7d', '14d')
     or p_start_date > p_end_date or p_snapshot->>'id' is distinct from p_id
     or p_snapshot->>'media_type' is distinct from 'CAROUSEL_ALBUM'
     or octet_length(p_snapshot::text) > 65536 then
    raise exception 'invalid_selection' using errcode = '22023';
  end if;
  -- Блокировка по паре предотвращает одновременное первое создание дублей.
  perform pg_advisory_xact_lock(hashtextextended(v_user::text || ':' || p_id, 0));
  select * into v_row from public.carousel_selections
    where user_id = v_user and instagram_media_id = p_id for update;
  if found then
    if v_row.removed_at is not null then
      if p_expected_revision is distinct from v_row.revision then
        raise exception 'revision_conflict' using errcode = '40001';
      end if;
      update public.carousel_selections set removed_at = null, selected_at = now(),
        updated_at = now(), revision = revision + 1, snapshot = p_snapshot,
        source_period = p_period, source_start_date = p_start_date,
        source_end_date = p_end_date, snapshot_at = now()
        where user_id = v_user and instagram_media_id = p_id returning * into v_row;
    end if;
  else
    if p_expected_revision is distinct from 0 then
      raise exception 'revision_conflict' using errcode = '40001';
    end if;
    insert into public.carousel_selections
      (user_id, instagram_media_id, snapshot, source_period, source_start_date, source_end_date)
      values (v_user, p_id, p_snapshot, p_period, p_start_date, p_end_date)
      returning * into v_row;
  end if;
  select count(*) into v_count from public.carousel_selections
    where user_id = v_user and removed_at is null;
  return jsonb_build_object('item', jsonb_build_object(
    'id', v_row.instagram_media_id, 'selected', true,
    'selected_at', v_row.selected_at, 'revision', v_row.revision,
    'snapshot', v_row.snapshot, 'source_period', v_row.source_period,
    'source_start_date', v_row.source_start_date, 'source_end_date', v_row.source_end_date,
    'snapshot_at', v_row.snapshot_at), 'selected_total', v_count);
end;
$$;

create function public.remove_carousel(p_id text, p_expected_revision bigint)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_user uuid := auth.uid();
  v_row public.carousel_selections%rowtype;
  v_count bigint;
begin
  if v_user is null then raise exception 'authentication_required' using errcode = '28000'; end if;
  if p_id !~ '^[0-9]{1,40}$' then raise exception 'invalid_selection' using errcode = '22023'; end if;
  select * into v_row from public.carousel_selections
    where user_id = v_user and instagram_media_id = p_id for update;
  if found and v_row.removed_at is null then
    if p_expected_revision is distinct from v_row.revision then
      raise exception 'revision_conflict' using errcode = '40001';
    end if;
    update public.carousel_selections set removed_at = now(), updated_at = now(),
      revision = revision + 1 where user_id = v_user and instagram_media_id = p_id
      returning * into v_row;
  end if;
  select count(*) into v_count from public.carousel_selections
    where user_id = v_user and removed_at is null;
  return jsonb_build_object('id', p_id, 'selected', false,
    'revision', coalesce(v_row.revision, 0), 'selected_total', v_count);
end;
$$;

revoke all on function public.select_carousel(text,jsonb,text,date,date,bigint) from public, anon;
revoke all on function public.remove_carousel(text,bigint) from public, anon;
grant execute on function public.select_carousel(text,jsonb,text,date,date,bigint) to authenticated;
grant execute on function public.remove_carousel(text,bigint) to authenticated;
