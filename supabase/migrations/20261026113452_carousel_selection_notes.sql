-- Общая заметка сохраняется отдельно от снимка Instagram и состояния отбора.
alter table public.carousel_likes
  add column note text not null default '' check (char_length(note) <= 3000),
  add column note_updated_at timestamptz,
  add column note_revision integer not null default 0 check (note_revision >= 0);

create or replace function public.set_carousel_like(
  p_id text, p_selected boolean, p_snapshot jsonb default null, p_period text default null
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_row public.carousel_likes%rowtype;
  v_snapshot jsonb;
  v_period text;
  v_key text;
  v_value text;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_selected is null then
    raise exception 'invalid_selection' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('reelscribe.carousel_likes', 0));
  select * into v_row from public.carousel_likes where instagram_media_id=p_id for update;
  if not p_selected then
    update public.carousel_likes set active=false where instagram_media_id=p_id and active;
    return jsonb_build_object('id',p_id,'selected',false);
  end if;
  if v_row.instagram_media_id is null or not v_row.active then
    v_snapshot := coalesce(p_snapshot,v_row.snapshot);
    v_period := coalesce(p_period,v_row.source_period);
    if v_snapshot is null or jsonb_typeof(v_snapshot) <> 'object'
       or octet_length(v_snapshot::text)>65536
       or v_period is null or v_period not in ('7d','14d','archive14d')
       or v_snapshot->>'id' is distinct from p_id
       or v_snapshot->>'media_type' is distinct from 'CAROUSEL_ALBUM'
       or v_snapshot - array['id','username','permalink','shortcode','timestamp','caption','media_type',
         'media_url','thumbnail_url','likes','comments','views','followers','collected_at'] <> '{}'::jsonb then
      raise exception 'invalid_snapshot' using errcode = '22023';
    end if;
    foreach v_key in array array['username','permalink','shortcode','timestamp','caption','media_url','thumbnail_url','collected_at'] loop
      if v_snapshot ? v_key and jsonb_typeof(v_snapshot->v_key) not in ('string','null') then
        raise exception 'invalid_snapshot_text' using errcode = '22023';
      end if;
    end loop;
    if length(v_snapshot->>'username')>30 or
       (v_snapshot->>'shortcode' is not null and v_snapshot->>'shortcode' !~ '^[A-Za-z0-9_-]+$') then
      raise exception 'invalid_snapshot_identity' using errcode = '22023';
    end if;
    foreach v_key in array array['likes','comments','views','followers'] loop
      if v_snapshot ? v_key and jsonb_typeof(v_snapshot->v_key) <> 'null' then
        if jsonb_typeof(v_snapshot->v_key) <> 'number' then
          raise exception 'invalid_snapshot_metric' using errcode = '22023';
        end if;
        if (v_snapshot->>v_key)::numeric < 0 or (v_snapshot->>v_key)::numeric > 9007199254740991 then
          raise exception 'invalid_snapshot_metric' using errcode = '22023';
        end if;
      end if;
    end loop;
    foreach v_key in array array['permalink','media_url','thumbnail_url'] loop
      v_value := v_snapshot->>v_key;
      if v_value is not null and (
        (v_key='permalink' and v_value !~* '^https://([a-z0-9-]+\.)*instagram\.com(/|$)') or
        (v_key<>'permalink' and v_value !~* '^https://([a-z0-9-]+\.)*(instagram\.com|cdninstagram\.com|fbcdn\.net)(/|$)')
      ) then raise exception 'invalid_snapshot_url' using errcode = '22023'; end if;
    end loop;
    if v_row.instagram_media_id is null then
      if (select count(*) from public.carousel_likes)>=5000 then
        raise exception 'collection_limit' using errcode = '22023';
      end if;
      insert into public.carousel_likes(instagram_media_id,snapshot,source_period)
        values(p_id,v_snapshot,v_period) returning * into v_row;
    else
      update public.carousel_likes set active=true, selected_at=now(), snapshot=v_snapshot, source_period=v_period
        where instagram_media_id=p_id returning * into v_row;
    end if;
  end if;
  return jsonb_build_object('id',v_row.instagram_media_id,'selected',true,'selected_at',v_row.selected_at,
    'snapshot',v_row.snapshot,'source_period',v_row.source_period,
    'note',v_row.note,'note_updated_at',v_row.note_updated_at,'note_revision',v_row.note_revision);
end;
$$;

-- Узкая операция сохраняет прежнюю общую модель доступа без прямой записи таблицы.
create function public.set_carousel_note(p_id text, p_note text, p_expected_revision integer)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_row public.carousel_likes%rowtype;
  v_note text;
  v_item jsonb;
begin
  if p_id is null or p_id !~ '^[0-9]{1,40}$' or p_note is null
     or char_length(p_note)>3000 or p_expected_revision is null or p_expected_revision<0 then
    raise exception 'invalid_note' using errcode='22023';
  end if;
  v_note := btrim(p_note, E' \t\r\n');
  perform pg_advisory_xact_lock(hashtextextended('reelscribe.carousel_likes', 0));
  select * into v_row from public.carousel_likes where instagram_media_id=p_id for update;
  if not found then return jsonb_build_object('code','not_found'); end if;
  if not v_row.active then return jsonb_build_object('code','selection_inactive'); end if;
  v_item := jsonb_build_object('id',p_id,'note',v_row.note,'note_revision',v_row.note_revision,'note_updated_at',v_row.note_updated_at);
  -- Повтор запроса после потерянного ответа не создаёт новое изменение.
  if v_row.note=v_note then return jsonb_build_object('item',v_item); end if;
  if v_row.note_revision<>p_expected_revision then
    return jsonb_build_object('code','note_conflict','item',v_item);
  end if;
  update public.carousel_likes set note=v_note,note_updated_at=now(),note_revision=note_revision+1
    where instagram_media_id=p_id returning * into v_row;
  return jsonb_build_object('item',jsonb_build_object('id',p_id,'note',v_row.note,
    'note_revision',v_row.note_revision,'note_updated_at',v_row.note_updated_at));
end;
$$;
revoke all on function public.set_carousel_note(text,text,integer) from public;
grant execute on function public.set_carousel_note(text,text,integer) to anon,authenticated;
