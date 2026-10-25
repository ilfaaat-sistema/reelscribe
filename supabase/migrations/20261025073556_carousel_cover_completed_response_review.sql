-- Закрытое операторское завершение после просмотра точного исходника.
-- Платный error архивируется целиком и продолжает учитываться в расходе.
create or replace function public.resolve_carousel_cover_known(p_id text,p_expected_created_at timestamptz,
  p_status text,p_source_text text,p_image_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_row public.carousel_cover_translations%rowtype;
  v_audit_version text;
  v_now timestamptz;
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
  v_now := clock_timestamp();
  select * into v_row from public.carousel_cover_translations
    where media_id=p_id and prompt_version='cover-v1' for update;
  if not found or v_row.created_at<>p_expected_created_at then
    raise exception 'cover_resolution_mismatch' using errcode='22023';
  end if;
  if v_row.slot_held then
    raise exception 'cover_resolution_forbidden' using errcode='22023';
  end if;
  if v_row.status='image_unavailable' and v_row.claim_token is null then
    -- Старая бесплатная ветка сохраняет одноразовую защиту.
    if exists(select 1 from public.carousel_cover_recovery_events where media_id=p_id) then
      raise exception 'cover_resolution_forbidden' using errcode='22023';
    end if;
  elsif v_row.status='error' and v_row.claim_token is not null then
    -- Чужой кадр и неизвестный ещё живой запрос не могут стать ручным результатом.
    if v_row.image_hash is distinct from p_image_hash
      or not (coalesce(v_row.usage->'provider_response_complete'='true'::jsonb,false)
        or (v_row.created_at+interval '600 seconds'<=v_now
          and coalesce(v_row.lease_expires_at,v_row.created_at+interval '600 seconds')<=v_now)) then
      raise exception 'cover_resolution_forbidden' using errcode='22023';
    end if;
  else
    raise exception 'cover_resolution_forbidden' using errcode='22023';
  end if;
  v_audit_version := 'cover-recovery-audit-'||gen_random_uuid()::text;
  update public.carousel_cover_translations set prompt_version=v_audit_version
    where media_id=p_id and prompt_version='cover-v1';
  -- Уже сохранённое событие первого восстановления не перезаписывается.
  insert into public.carousel_cover_recovery_events(media_id,original_created_at,reason)
    values(p_id,p_expected_created_at,'operator_visual_review') on conflict(media_id) do nothing;
  insert into public.carousel_cover_translations(media_id,prompt_version,status,source_text,text_ru,
    image_hash,usage,finished_at,source_kind,reason_code)
    values(p_id,'cover-v1',p_status,nullif(p_source_text,''),'',p_image_hash,
      jsonb_build_object('provenance','operator_visual_review','previous_audit_version',v_audit_version),
      v_now,case when v_row.claim_token is null then 'public_first_cover' else v_row.source_kind end,
      'operator_visual_review');
  return jsonb_build_object('status',p_status,'source_text',nullif(p_source_text,''),'text_ru','',
    'reason_code','operator_visual_review','attempt_recorded',false);
end;
$$;
revoke all on function public.resolve_carousel_cover_known(text,timestamptz,text,text,text) from public,anon,authenticated;
grant execute on function public.resolve_carousel_cover_known(text,timestamptz,text,text,text) to service_role;
