-- ============================================================================
-- ReelScribe — снимок схемы БД (Supabase / Postgres)
-- Снят 2026-08-10.
--
-- ЧТО ЗДЕСЬ ТОЧНО, А ЧТО РЕКОНСТРУКЦИЯ
--
--   [ПОДТВЕРЖДЕНО]  Состав и порядок колонок таблиц import_sessions, reels,
--                   transcripts, jobs — снят с живой базы через PostgREST
--                   (CSV-заголовок ответа). Это факт, а не догадка.
--
--   [РЕКОНСТРУКЦИЯ] Типы колонок, PK/FK, UNIQUE, NOT NULL, дефолты, индексы,
--                   RLS-политики и выражения generated-колонок. anon-ключ их не
--                   отдаёт, а SUPABASE_SERVICE_ROLE_KEY в окружении пуст,
--                   поэтому они восстановлены по §5 ТЗ (docs/ТЗ_ReelScribe.md)
--                   и по коду (backend/app/api/*, backend/app/services/jobs.py).
--
--   [РЕКОНСТРУКЦИЯ] Таблица reel_notes целиком — на момент снятия она пуста,
--                   PostgREST не отдал заголовок. Состав взят из §5 ТЗ и из
--                   backend/app/api/reels.py:202 (upsert по reel_id).
--
-- ПРИМЕНЯТЬ К БОЕВОЙ БАЗЕ ЭТОТ ФАЙЛ НЕЛЬЗЯ — он описывает уже существующую
-- схему и годится для поднятия чистого окружения с нуля. Точный дамп снимается
-- service_role-ключом (PostgREST OpenAPI) или через Supabase MCP `list_tables`.
-- ============================================================================

create extension if not exists "pgcrypto";  -- gen_random_uuid()

-- ── Сессии импорта ──────────────────────────────────────────────────────────
-- Одна строка = одна партия ссылок, загруженная пользователем.
create table if not exists public.import_sessions (
    id          uuid primary key default gen_random_uuid(),
    created_at  timestamptz not null default now(),
    source_type text,                    -- paste | txt | csv | json | xlsx
    engine      text,                    -- движок ASR, выбранный на импорте
    model       text,                    -- модель движка (medium | large-v3 | ...)
    translate   boolean default true,    -- переводить иностранные расшифровки
    pull_stats  boolean default true,    -- подтягивать просмотры/автора/подписчиков
    comment     text,                    -- комментарий пользователя ко всей партии
    total       integer,                 -- сколько ссылок в партии
    status      text default 'running'   -- running | done
);

-- ── Рилсы ───────────────────────────────────────────────────────────────────
-- shortcode — ключ дедупликации: повторный импорт той же ссылки не создаёт
-- вторую строку и не запускает повторное скачивание (железное правило проекта).
create table if not exists public.reels (
    id               uuid primary key default gen_random_uuid(),
    shortcode        text not null unique,
    url              text,
    type             text,               -- reel | p | tv
    caption          text,               -- ТЕКСТ ПОСТА (подпись автора), не расшифровка
    author_handle    text,
    author_followers integer,
    views            integer,
    likes            integer,
    comments         integer,
    posted_at        date,
    first_session_id uuid references public.import_sessions (id) on delete set null,
    created_at       timestamptz not null default now(),

    -- Метрики: код их только читает (backend/app/api/reels.py, export_.py) и
    -- никогда не пишет — значит в базе это generated-колонки. Формулы — §9 ТЗ.
    -- [РЕКОНСТРУКЦИЯ] сами выражения с живой базы не снимались.
    er   numeric generated always as (
             views::numeric / nullif(author_followers, 0)
         ) stored,                       -- залётность: просмотры ÷ подписчики
    lpf  numeric generated always as (
             likes::numeric / nullif(author_followers, 0) * 100
         ) stored,                       -- лайки ÷ подписчики, %
    cpf  numeric generated always as (
             comments::numeric / nullif(author_followers, 0) * 100
         ) stored,                       -- комментарии ÷ подписчики, %
    eng  numeric generated always as (
             (likes + comments)::numeric / nullif(views, 0) * 100
         ) stored                        -- вовлечённость: (лайки+комм) ÷ просмотры, %
);

-- ── Расшифровки ─────────────────────────────────────────────────────────────
-- transcript = распознанная речь. Не путать с reels.caption (текст поста).
create table if not exists public.transcripts (
    id           uuid primary key default gen_random_uuid(),
    reel_id      uuid not null references public.reels (id) on delete cascade,
    engine       text,
    model        text,
    language     text,                   -- определённый язык оригинала
    duration_sec integer,
    text         text,                   -- оригинальная расшифровка
    text_ru      text,                   -- перевод на русский (если оригинал не RU)
    summary      text,                   -- саммари (не реализовано, /summary отдаёт 501)
    tags         text[],                 -- теги саммари (опц.)
    ocr_text     text,                   -- текст с экрана видео (фаза 3, опц.)
    status       text default 'queued',  -- queued|downloading|transcribing|done|failed
    fail_reason  text,                   -- причина при failed: приватный/удалён/нет аудио
    created_at   timestamptz not null default now()
);

-- ── Заметки по рилсу ────────────────────────────────────────────────────────
-- [РЕКОНСТРУКЦИЯ ЦЕЛИКОМ] таблица пуста, состав снять не удалось.
-- reel_id — первичный ключ: код делает upsert по нему (api/reels.py:202),
-- то есть заметка у рилса ровно одна.
create table if not exists public.reel_notes (
    reel_id    uuid primary key references public.reels (id) on delete cascade,
    note       text,
    updated_at timestamptz not null default now()
);

-- ── Очередь заданий ─────────────────────────────────────────────────────────
create table if not exists public.jobs (
    id              uuid primary key default gen_random_uuid(),
    reel_id         uuid references public.reels (id) on delete cascade,
    session_id      uuid references public.import_sessions (id) on delete cascade,
    state           text default 'queued',   -- queued | in_progress | done | failed
    attempts        integer not null default 0,
    next_attempt_at timestamptz default now(),  -- backoff при ретраях
    error           text
);

-- ── Индексы ─────────────────────────────────────────────────────────────────
-- [РЕКОНСТРУКЦИЯ] выведены из реальных запросов кода, с живой базы не снимались.
create index if not exists idx_transcripts_reel_id  on public.transcripts (reel_id);
create index if not exists idx_reels_first_session  on public.reels (first_session_id);
-- воркер забирает задания так: state + next_attempt_at (app/services/jobs.py)
create index if not exists idx_jobs_claim           on public.jobs (state, next_attempt_at);
create index if not exists idx_jobs_session_id      on public.jobs (session_id);

-- ── RLS ─────────────────────────────────────────────────────────────────────
-- [РЕКОНСТРУКЦИЯ] Фактические политики с живой базы не снимались.
--
-- Контекст: аутентификации в продукте нет осознанно (решение 2026-07-03,
-- CLAUDE.md) — доступ есть у любого, у кого есть ссылка. Бэкенд и воркер ходят
-- в базу на anon-ключе (backend/app/core/db.py), значит политики обязаны
-- разрешать anon и чтение, и запись. Ниже — минимальный набор, дающий именно
-- такое поведение. Перед применением к новому окружению сверить с боевой базой.
alter table public.import_sessions enable row level security;
alter table public.reels           enable row level security;
alter table public.transcripts     enable row level security;
alter table public.reel_notes      enable row level security;
alter table public.jobs            enable row level security;

do $$
declare t text;
begin
    foreach t in array array['import_sessions', 'reels', 'transcripts', 'reel_notes', 'jobs']
    loop
        execute format(
            'create policy %I on public.%I for all to anon, authenticated using (true) with check (true)',
            'anon_full_access_' || t, t
        );
    end loop;
end $$;
