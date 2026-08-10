-- ============================================================================
-- Rate-limit импортов по IP — перенос состояния из памяти процесса в базу.
-- Добавлено 2026-08-10.
--
-- Зачем: API переехал на serverless (Vercel), где каждый вызов может попасть в
-- свежий инстанс. Словарь в памяти (`_rate_hits` в app/api/import_.py) там
-- обнулялся бы на каждом холодном старте и не защищал бы ничего, а импорт
-- тратит платные квоты владельца — правило «защита кошелька обязательна».
--
-- Логика в коде: 5 импортов с одного IP за 600 секунд (RATE_MAX_IMPORTS /
-- RATE_WINDOW_SEC в app/api/import_.py). Старые отметки удаляются при каждой
-- проверке, поэтому таблица остаётся крошечной и чистить её отдельно не нужно.
-- ============================================================================

create table if not exists public.rate_limits (
    id         bigserial primary key,
    ip         text        not null,
    created_at timestamptz not null default now()
);

-- Запрос всегда идёт по паре (ip, окно времени) — индекс покрывает его целиком.
create index if not exists idx_rate_limits_ip_created
    on public.rate_limits (ip, created_at);

-- Уборка старых отметок идёт по одному created_at, без ip.
create index if not exists idx_rate_limits_created
    on public.rate_limits (created_at);

-- RLS: как и остальные таблицы проекта — аутентификации нет осознанно
-- (решение 2026-07-03), бэкенд ходит в базу на anon-ключе.
alter table public.rate_limits enable row level security;

create policy anon_full_access_rate_limits on public.rate_limits
    for all to anon, authenticated using (true) with check (true);
