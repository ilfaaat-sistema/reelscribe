-- ТЗ 06: метки происхождения рилса — из какой учётки Instagram и какой сохранённой папки он взят.
-- Связь многие-ко-многим: один рилс лежит сразу в нескольких папках, а имена папок повторяются
-- в разных учётках, поэтому ключ метки — пара (account, name), а не одна строка.

create table if not exists public.reel_sources (
  reel_id   uuid not null references public.reels(id) on delete cascade,
  account   text not null,                                     -- ilfaaat_sistema | neyro_set7
  kind      text not null check (kind in ('folder', 'direct')),
  name      text not null,                                     -- имя папки (уже без хвостовых пробелов); для direct — 'Директ'
  added_at  timestamptz,                                       -- когда сохранено/отправлено, если выгрузка это знает
  primary key (reel_id, account, kind, name)
);

create index if not exists idx_reel_sources_account on public.reel_sources (account);
create index if not exists idx_reel_sources_name    on public.reel_sources (name);

-- Бэкенд ходит anon-ключом: без политики любой запрос вернёт 42501 (как было с worker_runs в 0004).
alter table public.reel_sources enable row level security;

drop policy if exists anon_full_access_reel_sources on public.reel_sources;
create policy anon_full_access_reel_sources on public.reel_sources
  for all to anon, authenticated using (true) with check (true);

-- Счётчики для блока «Мои источники»: сколько рилсов в каждой папке и сколько из них расшифровано.
create or replace function public.reel_sources_stats()
returns table (
  account text,
  kind    text,
  name    text,
  total   bigint,
  done    bigint,
  queued  bigint,
  failed  bigint
)
language sql
stable
security invoker
set search_path = public
as $$
  select
    s.account,
    s.kind,
    s.name,
    count(*)                                                              as total,
    count(*) filter (where t.status = 'done')                             as done,
    count(*) filter (where t.status is null
                        or t.status in ('queued', 'downloading',
                                        'transcribing', 'translating'))   as queued,
    count(*) filter (where t.status in ('failed', 'no_audio'))            as failed
  from public.reel_sources s
  left join public.transcripts t on t.reel_id = s.reel_id
  group by s.account, s.kind, s.name
  order by s.account, s.kind, s.name;
$$;

grant execute on function public.reel_sources_stats() to anon, authenticated;
