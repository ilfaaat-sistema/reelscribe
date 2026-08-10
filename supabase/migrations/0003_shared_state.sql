-- ============================================================================
-- Общее состояние воркеров: cooldown API-ключей и кэш подписчиков.
-- Добавлено 2026-08-10. Задачи A и D из
-- docs/specs/2026-08-10_сохранённые-рилсы-и-общая-квота.md
--
-- Зачем: и cooldown ключей, и кэш подписчиков жили в памяти процесса
-- (_cooldown/_actor_cooldown/_followers_cache в app/pipeline/apify_client.py и
-- app/pipeline/starapi_downloader.py). Пока воркер был один, это работало.
-- Когда воркеров стало четыре, каждый видел свою картину:
--
--   * cooldown — все четверо независимо упирались в исчерпанный ключ и по
--     очереди узнавали об этом на своих ошибках;
--   * кэш подписчиков — каждый скрейпил профиль одного и того же автора заново,
--     а это самый дорогой вызов каскада.
--
-- Замер 10.08.2026: четыре воркера подняли цену с $0.0188 до $0.053-0.075 за
-- рилс, то есть переплата вышла примерно втрое.
--
-- Обе таблицы читаются и пишутся best-effort: если база недоступна, код
-- откатывается на прежнее поведение в памяти и скачивание не падает.
-- ============================================================================

-- ── Cooldown ключей ─────────────────────────────────────────────────────────
-- ВАЖНО: key_ref — это отпечаток ключа (sha256, первые 16 символов), а НЕ сам
-- ключ. Секреты в базу не попадают: даже с полным доступом к таблице
-- восстановить ключ нельзя.
--
-- actor разделяет два разных бана, и это разделение принципиально:
--   ''          — исчерпан кредит АККАУНТА, ключ не годится ни для чего;
--   '<actor id>' — нет прав/аренды на конкретный платный actor, остальные
--                  actor'ы этим же ключом работают нормально.
-- Схлопывать их в один флаг нельзя: отказ платного одиночного актора забанил бы
-- токен для основного профильного скрейпа, то есть убил бы весь каскад загрузки.
create table if not exists public.key_cooldown (
    provider   text        not null,               -- 'apify' | 'rapidapi'
    key_ref    text        not null,               -- sha256(ключ)[:16], не секрет
    actor      text        not null default '',    -- '' = аккаунт целиком
    until      timestamptz not null,
    updated_at timestamptz not null default now(),
    primary key (provider, key_ref, actor)
);

-- Выборка всегда «что сейчас на cooldown» — то есть фильтр по until.
create index if not exists idx_key_cooldown_until
    on public.key_cooldown (until);

-- ── Кэш подписчиков ─────────────────────────────────────────────────────────
-- expires_at is null означает «успех, хранить бессрочно»: число подписчиков не
-- меняется настолько быстро, чтобы платить за него повторно. Неудача (followers
-- is null) хранится с TTL — повторить попытку имеет смысл ровно тогда, когда
-- ключи снова доступны, а не молчать про этого автора вечно.
create table if not exists public.followers_cache (
    username   text primary key,
    followers  integer,                            -- null = попытка была неудачной
    expires_at timestamptz,                        -- null = бессрочно
    updated_at timestamptz not null default now()
);

-- RLS: как и остальные таблицы проекта — аутентификации нет осознанно
-- (решение 2026-07-03), бэкенд ходит в базу на anon-ключе.
alter table public.key_cooldown enable row level security;
alter table public.followers_cache enable row level security;

create policy anon_full_access_key_cooldown on public.key_cooldown
    for all to anon, authenticated using (true) with check (true);

create policy anon_full_access_followers_cache on public.followers_cache
    for all to anon, authenticated using (true) with check (true);
