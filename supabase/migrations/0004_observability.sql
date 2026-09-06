-- ============================================================================
-- Наблюдаемость: история прогонов воркера и источник скачивания у расшифровки.
-- Добавлено 2026-09-04. Из docs/specs/2026-09-04_автономный-разбор.md.
--
-- Зачем. 04.09.2026 на вопрос «чем скачаны эти рилсы и во что обошёлся прогон»
-- ушло несколько часов, и ответ вышел неверным. Причина в том, что отвечать
-- было нечему:
--
--   * источник скачивания (`apify-single` / `apify-profile` / `instaloader` /
--     `starapi` / `yt-dlp`) нигде не сохранялся — он существовал только внутри
--     `info` в памяти процесса;
--   * история прогонов жила исключительно в логах GitHub Actions, которые
--     доступны лишь после завершения прогона и удаляются через 90 дней;
--   * пока прогон идёт, снаружи не видно ни его расхода, ни того, каким
--     источником он работает.
--
-- Обе вещи здесь — данные для диагностики, а не для продуктовой логики. Ни один
-- существующий запрос от них не зависит: колонка nullable, таблица новая.
-- Код пишет в них best-effort (см. app/workers/run.py): если таблицы ещё нет
-- или база недоступна, разбор продолжается как раньше.
-- ============================================================================

-- ── Источник скачивания у конкретной расшифровки ────────────────────────────
-- Заполняется воркером в момент успешного скачивания. NULL у всех записей,
-- сделанных до 04.09.2026, — восстановить их задним числом нельзя.
alter table public.transcripts
    add column if not exists source text;

comment on column public.transcripts.source is
    'Каким источником каскада скачано аудио: apify-single | apify-profile | instaloader | starapi | yt-dlp';

-- ── История прогонов воркера ────────────────────────────────────────────────
-- Одна строка на запуск `python -m app.workers.run --drain`. Строка создаётся в
-- начале прогона и обновляется по ходу, поэтому таблица отвечает и на вопрос
-- «что происходит прямо сейчас», который по логам Actions задать нельзя.
create table if not exists public.worker_runs (
    id              uuid primary key default gen_random_uuid(),
    run_id          text,                    -- GITHUB_RUN_ID, если прогон на Actions; NULL локально
    started_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    finished_at     timestamptz,             -- NULL, пока прогон идёт
    outcome         text,                    -- drained | timeout | crashed | NULL (идёт)
    jobs_done       integer not null default 0,
    jobs_no_audio   integer not null default 0,
    jobs_failed     integer not null default 0,
    sources         jsonb not null default '{}'::jsonb,  -- {"apify-single": 120, "starapi": 19}
    apify_spent_usd numeric,                 -- разница расхода Apify за прогон, доллары
    note            text
);

-- Свежие прогоны сверху — единственный порядок, в котором эту таблицу читают.
create index if not exists worker_runs_started_at_idx
    on public.worker_runs (started_at desc);

comment on table public.worker_runs is
    'История прогонов воркера: что разобрано, каким источником, во что обошлось. Пишется best-effort.';

-- ── Доступ ──────────────────────────────────────────────────────────────────
-- Supabase включает RLS на новых таблицах, и без политики воркер (ходит под anon-ключом)
-- получает 42501 «new row violates row-level security policy» — история прогонов молча
-- остаётся пустой. Именно на этом 06.09.2026 таблица провисела впустую до первой проверки.
-- Политика — копия того, что стоит у соседних таблиц (`jobs` → anon_all, `key_cooldown` →
-- anon_full_access_key_cooldown): сервис сознательно работает без авторизации, доступ у
-- всех, у кого есть ссылка (решение от 2026-07-03, см. CLAUDE.md).
drop policy if exists anon_full_access_worker_runs on public.worker_runs;
create policy anon_full_access_worker_runs on public.worker_runs
    for all to anon, authenticated using (true) with check (true);
