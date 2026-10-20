-- Общий список без аккаунтов: публичное чтение, записи только через проверенные RPC.
create table public.carousel_hidden_authors (
  username text primary key check (username ~ '^[a-z0-9._]{1,30}$'),
  hidden_at timestamptz not null default now() check (isfinite(hidden_at)),
  active boolean not null default true
);
alter table public.carousel_hidden_authors enable row level security;
revoke all on public.carousel_hidden_authors from public, anon, authenticated;
grant select on public.carousel_hidden_authors to anon, authenticated;
create policy carousel_hidden_authors_read on public.carousel_hidden_authors
  for select to anon, authenticated using (active);

-- SECURITY DEFINER намеренно разрешает только ограниченные общие операции без auth.
create function public.set_carousel_author_hidden(p_username text, p_hidden boolean)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_row public.carousel_hidden_authors%rowtype;
begin
  if p_username is null or p_username !~ '^[a-z0-9._]{1,30}$' or p_hidden is null then
    raise exception 'invalid_author' using errcode='22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('reelscribe.carousel_hidden_authors',0));
  select * into v_row from public.carousel_hidden_authors where username=p_username;
  if not p_hidden then
    insert into public.carousel_hidden_authors(username,active) values(p_username,false)
      on conflict(username) do update set active=false;
    return jsonb_build_object('username',p_username,'hidden',false);
  end if;
  if v_row.username is null or not v_row.active then
    if (select count(*) from public.carousel_hidden_authors where active)>=1600 then
      raise exception 'hidden_authors_limit' using errcode='22023';
    end if;
    insert into public.carousel_hidden_authors(username) values(p_username)
      on conflict(username) do update set active=true,hidden_at=now()
      returning * into v_row;
  end if;
  return jsonb_build_object('username',v_row.username,'hidden_at',v_row.hidden_at);
end;
$$;
revoke all on function public.set_carousel_author_hidden(text,boolean) from public;
grant execute on function public.set_carousel_author_hidden(text,boolean) to anon, authenticated;

create function public.import_carousel_hidden_authors(p_authors jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_item jsonb;
  v_date timestamptz;
  v_authors jsonb;
begin
  if p_authors is null or jsonb_typeof(p_authors)<>'array' then
    raise exception 'invalid_authors' using errcode='22023';
  end if;
  if jsonb_array_length(p_authors)>1600 then
    raise exception 'import_limit' using errcode='22023';
  end if;
  -- Проверяется весь пакет, включая дубликаты и уже существующие имена, до записи.
  for v_item in select value from jsonb_array_elements(p_authors) loop
    if jsonb_typeof(v_item) is distinct from 'object'
       or v_item - array['username','hidden_at'] <> '{}'::jsonb
       or jsonb_typeof(v_item->'username') is distinct from 'string'
       or v_item->>'username' !~ '^[a-z0-9._]{1,30}$'
       or jsonb_typeof(v_item->'hidden_at') is distinct from 'string'
       or v_item->>'hidden_at' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$' then
      raise exception 'invalid_author' using errcode='22023';
    end if;
    v_date := (v_item->>'hidden_at')::timestamptz;
    if not isfinite(v_date) then
      raise exception 'invalid_date' using errcode='22023';
    end if;
  end loop;
  perform pg_advisory_xact_lock(hashtextextended('reelscribe.carousel_hidden_authors',0));
  if (select count(*) from public.carousel_hidden_authors where active) +
     (select count(distinct value->>'username') from jsonb_array_elements(p_authors)
      where not exists(select 1 from public.carousel_hidden_authors h where h.username=value->>'username'))>1600 then
    raise exception 'hidden_authors_limit' using errcode='22023';
  end if;
  insert into public.carousel_hidden_authors(username,hidden_at)
    select distinct on (value->>'username') value->>'username',(value->>'hidden_at')::timestamptz
    from jsonb_array_elements(p_authors) with ordinality as entries(value,position)
    order by value->>'username',position
    on conflict(username) do nothing;
  select coalesce(jsonb_agg(jsonb_build_object('username',username,'hidden_at',hidden_at)
    order by hidden_at desc,username),'[]'::jsonb) into v_authors
    from public.carousel_hidden_authors where active;
  return jsonb_build_object('authors',v_authors,'total',jsonb_array_length(v_authors));
end;
$$;
revoke all on function public.import_carousel_hidden_authors(jsonb) from public;
grant execute on function public.import_carousel_hidden_authors(jsonb) to anon, authenticated;
