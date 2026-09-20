# ТЗ 06. Метки источника рилса: учётка Instagram + папка сохранённого

**Статус:** в работе (заведено 20.09.2026)
**Ветка:** `module/sources`, worktree `worktrees/sources`
**Связанные документы:** [01-collections.md](01-collections.md) — предшественник, не реализован,
его модель (одна колонка `reels.collection`) настоящим ТЗ **заменяется** и помечается архивной.

## 1. Зачем

Владелец выгружает архивы своих учёток Instagram и хочет, чтобы база расшифровок помнила
происхождение каждого рилса: из какой учётки и из какой сохранённой папки он взят. Это даёт три
рабочих сценария:

1. **учиться** по сохранённому, читая расшифровки темами;
2. **искать по всей базе** («где-то сохранял видео про монтаж») — поиск уже есть, меткам нужно
   лишь сузить его до темы;
3. **отбирать под пересъёмку**: папки вроде «Повторить про нейросети» плюс существующий фильтр
   «🔥 залётные» дают готовый список идей.

Сегодня происхождение не хранится вовсе: у рилса есть только `first_session_id` — в какой сессии
импорта он приехал.

## 2. Данные, с которыми работаем

Выгрузки лежат вне git (правило `.gitignore`, личные данные): `Мои инстаграммы/<профиль>/`,
внутри `saved/saved_collections.json`, `saved/saved_posts.json`, `messages/**/*.json`.

Замер 20.09.2026:

| Источник | Папок | Уникальных shortcode |
|---|---|---|
| `ilfaaat_sistema` — папки + сохранённое вне папок | 36 | 2320 |
| `neyro_set7` — папки + сохранённое вне папок | 18 | 468 |
| Переписка с `neyro_set7` | — | 815 (690 больше нигде не сохранены) |

Свойства данных, определяющие модель:

- один рилс лежит **в нескольких папках сразу** → связь многие-ко-многим;
- 330 рилсов сохранены **в обеих учётках**;
- имена папок **повторяются между учётками** («ИИ нейросети», «Создание сайтов», «Нейросети»,
  «Переснять можно») → метка это **пара (учётка, папка)**, а не строка;
- имена папок в выгрузке идут с **хвостовым пробелом** («Создание сайтов ») → `strip()`;
- JSON в **mojibake** (UTF-8 прочитан как latin-1) → чинится `_fix_mojibake` из
  `backend/app/pipeline/normalize.py`;
- **имя папки не уникально даже внутри одной учётки**: у `ilfaaat_sistema` две разные коллекции
  Instagram названы «Нейросети» (127 и 34 медиа), ещё по две — «Фильмы» и «Отношения». Разбор
  обязан складывать их в одну метку, а не терять вторую: первый замер 20.09 потерял и насчитал
  по «Нейросети» 34 вместо 161;
- ссылки лежат не в поле `media` (оно пустое), а глубоко во вложенной группе `label_values`,
  поэтому обход ищет ссылки рекурсивно, а не по фиксированному пути; время сохранения берётся
  из `saved/saved_posts.json` и сшивается по `shortcode`.

### Что размечаем в этот заход

- `ilfaaat_sistema`: пять папок — «Нейросети» (161, две одноимённые коллекции), «3д» (13),
  «Нейросети для фото и видео» (54), «Повторить про нейросети» (38), «Создание сайтов» (53) —
  309 уникальных;
- `neyro_set7`: **все** папки (423);
- переписка с `neyro_set7`: метка «Директ» (815).

Замер разборщика 20.09: по `ilfaaat_sistema` (пять папок + директ) 1018 уникальных, из них новых
для базы 116; по `neyro_set7` (все папки + директ) 1220 уникальных, новых 153. Значительная часть
давно в базе — ТЗ 02 импортировало «директ + эти пять папок» 06.09. Остальные 22 папки `ilfaaat_sistema` в этот заход не идут — скрипт их видит,
добор потом делается тем же запуском с другим списком.

## 3. Модель данных

Миграция `supabase/migrations/0005_reel_sources.sql`. Номер 0004 занят `0004_observability.sql`;
неприменённый `0004_collections.sql` из ветки `module/collections` не используется, ветка не мёржится.

```sql
create table if not exists public.reel_sources (
  reel_id   uuid not null references public.reels(id) on delete cascade,
  account   text not null,                                    -- ilfaaat_sistema | neyro_set7
  kind      text not null check (kind in ('folder','direct')),
  name      text not null,                                    -- имя папки (strip); для direct — 'Директ'
  added_at  timestamptz,
  primary key (reel_id, account, kind, name)
);
create index if not exists idx_reel_sources_account on public.reel_sources(account);
create index if not exists idx_reel_sources_name    on public.reel_sources(name);

alter table public.reel_sources enable row level security;
create policy anon_full_access_reel_sources on public.reel_sources
  for all to anon, authenticated using (true) with check (true);
```

`reel_id` — **uuid**: в живой базе `reels.id` это uuid. RLS с политикой обязательна, бэкенд ходит
anon-ключом (та же история описана в `0004_observability.sql` про `worker_runs`).

Функция статистики:

```sql
create or replace function public.reel_sources_stats()
returns table (account text, kind text, name text,
               total bigint, done bigint, queued bigint, failed bigint)
language sql stable security invoker set search_path = public as $$
  select s.account, s.kind, s.name,
         count(*),
         count(*) filter (where t.status = 'done'),
         count(*) filter (where t.status is null or t.status in
                          ('queued','downloading','transcribing','translating')),
         count(*) filter (where t.status in ('failed','no_audio'))
  from public.reel_sources s
  left join public.transcripts t on t.reel_id = s.reel_id
  group by s.account, s.kind, s.name
  order by s.account, s.kind, s.name;
$$;
grant execute on function public.reel_sources_stats() to anon, authenticated;
```

Вьюху `reels_full` **не заводим**: метки читаются embed'ом по внешнему ключу, а лишний объект в
общем Supabase-проекте — лишняя мина (у view свои правила RLS и грантов).

## 4. Контракт (фиксирован, менять по ходу нельзя)

Три исполнителя работают параллельно и обязаны совпасть в именах.

| Что | Имя |
|---|---|
| Таблица | `public.reel_sources(reel_id, account, kind, name, added_at)` |
| Функция статистики | `public.reel_sources_stats()` → `account, kind, name, total, done, queued, failed` |
| Query-параметры `/api/reels` и `/api/export` | `account`, `folder` |
| Новый эндпоинт | `GET /api/sources` → `{items: [{account, kind, name, total, done, queued, failed}]}` |
| Поля рилса в JSON | `accounts: string[]`, `folders: string[]`, `from_direct: bool` |
| Колонки экспорта | `accounts` → «Аккаунт(ы)», `folders` → «Папка(и)», `source_direct` → «Директ» |
| Значения `kind` | `folder` \| `direct` (у `direct` поле `name` всегда `'Директ'`) |
| CSS-классы | `.srcbadges`, `.tag.t-acct`, `.tag.t-folder`, `.tag.t-direct` |
| Состояние фронта | `filters.account`, `filters.folder` в `Results.jsx` |

## 5. Работы

### A. Бэкенд (`backend/app/api/*`, `backend/app/models/schemas.py`)

- `ReelRow` (наследуется в `ReelDetail`): `accounts: list[str] = []`, `folders: list[str] = []`,
  `from_direct: bool = False`. Новые `SourceStat`, `SourcesResponse`.
- `reels.py`: в select дописать `reel_sources(account, kind, name)` — тем же механизмом, которым
  уже подтягиваются `transcripts(...)` и `reel_notes(...)`. Агрегация меток в `_build_row`
  и в `get_reel`.
- Фильтрация: параметры `account` и `folder`. Основной путь — `reel_sources!inner(...)` с
  `.eq('reel_sources.account', …)` / `.eq('reel_sources.name', …)`. Запасной путь (ID-lookup +
  `.in_('id', ids)` по образцу `_search_reel_ids`) допустим **только** если найденных id мало:
  при `account=ilfaaat_sistema` их тысячи, и список uuid не влезет в URL. Выбор пути подтвердить
  фактическим ответом сервера, а не рассуждением.
- Новый `GET /api/sources` через `db.rpc('reel_sources_stats')`.
- `export_.py`: embed в `_fetch`, параметры `account`/`folder` (только в ветке, где не заданы `ids`),
  колонки в `_to_flat`, заголовки в `_RU`.
- **Обратная совместимость обязательна**: без новых параметров все ответы должны остаться
  прежними.

### B. Скрипт разметки `backend/app/workers/import_saved.py`

CLI по образцу `backfill_metrics.py`: `--root`, `--account`, `--folders` (список или `all`),
`--dry-run`, `--out`.

- Директ (`messages/**/*.json`) — через готовую `parse_message_json()`, метка
  `kind='direct', name='Директ'`.
- Папки (`saved/saved_collections.json`) — **свой обход**: `parse_message_json` схлопывает всё в
  плоский список и теряет имя папки. Обход должен быть терпим к формату (искать списки объектов
  «имя + вложенный список медиа», а не хардкодить путь: Meta меняет схему экспорта год от года),
  с `strip()` имён и починкой mojibake.
- Рилс уже в базе → только `upsert` в `reel_sources` по составному ключу. Никакой перекачки и
  повторного распознавания — железное правило проекта про дедуп по `shortcode`.
- Рилса нет → создание `reels`/`transcripts`/`jobs` через существующий `_setup_jobs`
  из `import_service.py`, без дублирования логики дедупа. `import_sessions.source_type` ограничен
  CHECK'ом (`paste|txt|csv|json`) — новых значений не вводить, писать `json`, подробности
  («saved-import: <учётка>») — в `comment` сессии.
- `--dry-run` ничего не пишет в БД: собирает реестр и **первым делом сохраняет JSON-отчёт на
  диск**, только потом печатает сводку.

### C. Фронтенд

- `client.js`: `getSources()`. `getReels`/`exportUrl` прокидывают произвольные параметры — правок
  не требуют.
- `Results.jsx`: колонка «Источник» с бейджами; фильтры «учётка»/«папка» в панели «⚙ Фильтры»;
  чтение `?account=&folder=` из URL (`useSearchParams`), чтобы ссылки из блока источников
  сразу фильтровали; проброс фильтров в `ExportModal`.
- `Import.jsx`: блок «Мои источники» **вместо** врезки «История импортов» — учётки, внутри папки
  со счётчиками «всего / расшифровано», клик ведёт в `/results?account=…&folder=…`. Страница
  `/history` со списком сессий остаётся без изменений.
- `index.css`: `.srcbadges` и модификаторы `.tag`; карточки источников — на существующих
  `.hist`/`.hi-main`/`.hi-sub`, чтобы визуально совпадало с тем, что было.

## 6. Порядок и распределение

ТЗ и миграция — Opus (главная сессия). Затем **параллельно** три Sonnet-агента: A (бэкенд),
B (скрипт), C (фронт) — границы по файлам, пересечений нет. Приёмка стартует только когда все три
прислали уведомление о завершении; тишина в файлах завершением не считается. Ревью диффов, суждение
«нормально ли выглядит», мёрж и деплой — Opus.

## 7. Критерии приёмки

1. `curl` к REST: чтение `reel_sources` и вызов `rpc/reel_sources_stats` дают 200 (проверка RLS и
   грантов), а не 401/403/404.
2. Ответы `/api/reels` без новых параметров совпадают с baseline, снятым до правок — в том числе
   для `filter=done`, `filter=viral`, `q=…`, `sort=er`.
3. `--dry-run` кладёт на диск отчёт с разбивкой по учёткам и папкам, числом уже имеющихся и новых
   рилсов; в именах папок нет хвостовых пробелов.
4. Два прогона разметки подряд дают одинаковый `count(*)` в `reel_sources` (идемпотентность).
5. `GET /api/sources` возвращает ненулевые счётчики; `GET /api/reels?account=…&folder=…` —
   непустой список.
6. `pytest` и `ruff check` в `backend/`, `npm run build` во `frontend/` — зелёные.
7. Блок «Мои источники» и колонка «Источник» выглядят как остальной интерфейс (проверяет Opus
   по скриншотам до показа владельцу).

## 8. Деньги

Разметка уже имеющихся рилсов бесплатна — это SQL. Платит только добор новых: $0.0047 за рилс по
последнему замеру проекта, то есть 100 новых ≈ 40 ₽. Точное число даёт dry-run. **Добор
запускается только после явного «да» владельца с цифрой в рублях.**
