# ТЗ 11. Разведка Радара на официальном API Instagram: бесплатное обновление и автообновление

**Статус:** ТЗ

## Проблема / цель

Разведка (ТЗ 10) обновляет источники через Apify: ≈ 0,21 ₽ за рилс, 20 источников ≈ 66 ₽ за
нажатие, поэтому обновление сделано только кнопкой, а «набирает» по росту между замерами почти
не работает. При этом к проекту с 11.09.2026 подключён официальный Instagram Graph API
(Business Discovery): один бесплатный запрос по нику отдаёт подписчиков и до 100 публикаций с
просмотрами, лайками, комментариями и датой (замеры — `docs/specs/05-scout.md`).

Цель: «Обновить» ходит в официальный API бесплатно, Apify остаётся запасным; раз в сутки
источники обновляются сами; порог залётности поднят до осмысленного.

Решения владельца (05.10.2026): переключить на бесплатный API с Apify в запасе — да;
автообновление раз в сутки — да; порог залётности 10× — да.

## Пользовательский сценарий

1. Открывает «Ленту» утром — данные уже свежие: ночью источники обновились сами, на полосе
   «Обновлено 6 ч назад · авто».
2. Добавляет новый аккаунт → «Обновить» → на полосе «бесплатно»; через несколько секунд рилсы
   в ленте.
3. Если аккаунт личный (официальный API его не отдаёт), в «Источниках» у него пометка
   «через Apify», и цена на полосе считается только за такие аккаунты.
4. «Разобрать» работает как раньше: ссылка на видео добирается через Apify в момент разбора.
5. Бейдж залётного стоит только у действительно выстреливших рилсов (от 10× нормы).

## Функциональные требования

### Шаг 0 — сверка просмотров (ДО кода, делает Opus, бесплатно)

Один запрос Business Discovery по `directoreels` → сравнить `view_count` с `radar_reels.views`
тех же рилсов (собраны через Apify 05.10.2026). Результат сразу в JSON в scratchpad.
Если медиана относительного расхождения ≤ 10 % — продолжать. Если больше — остановиться и
показать владельцу цифры: смешивать источники в одной норме нельзя, решение за ним.
Заодно зафиксировать: сколько рилсов за 90 дней помещается в 1, 2, 3 страницы по 50.

### Клиент (`backend/app/radar/graph_api.py`, новый)

- Основа — готовый клиент `worktrees/scout/backend/app/pipeline/business_discovery.py`
  (240 строк, в git НЕ закоммичен — скопировать содержимое, файл-источник не трогать).
  Оставить: `AuthorNotFound`, `RateLimited`, `MediaItem`, `AuthorSnapshot`,
  `shortcode_from_permalink`, `fetch_author`, `fetch_author_all`, учёт `x-app-usage` с
  cooldown через `app.pipeline.shared_state`.
- Настройки — НЕ в `core/config.py`, а в `radar/settings.py` через `os.getenv`:
  `META_IG_TOKEN`, `META_IG_USER_ID`, `META_IG_API_VERSION` (дефолт `v23.0`),
  `META_IG_USAGE_SOFT_LIMIT=90`, `GRAPH_PAGE_SIZE=50`, `GRAPH_DEEP_PAGES=3`,
  `GRAPH_INCR_PAGES=1`, `GRAPH_SOURCES_PER_STEP=5`, `GRAPH_RECHECK_DAYS=30`.
- `is_configured()` ложно при пустом токене → всё поведение ровно как в ТЗ 10 (Apify).
- В ленту идут только публикации с `media_product_type == 'REELS'` (у остальных нет просмотров).
- Маппинг в строку `radar_reels`: `id` = shortcode из `permalink`, `username`, `url` = permalink,
  `caption`, `views` = `view_count`, `likes`, `comments`, `posted_at` = `timestamp`.
  `video_url`, `duration_sec`, `hashtags`, `music` НЕ передаются вовсе — апсерт не должен
  затирать уже сохранённые значения (проверить поведение `repo.bulk_upsert_reels`: набор
  ключей у строк внутри одного вызова одинаковый).

### Обновление (`refresh_service.py`)

- У источника новое поле `provider`: `graph` | `apify` | null (ещё не проверен) и
  `provider_checked_at`.
- План обновления: источники с `provider != 'apify'` при настроенном API идут в пачки нового
  вида `kind='graph'` (по `GRAPH_SOURCES_PER_STEP` ников); остальные — в прежние пачки Apify
  (рилсы + подписчики). Порядок: graph → apify-рилсы → apify-подписчики.
- Пачка `graph` выполняется синхронно внутри шага `GET /refresh/current` (захват старта тем же
  `claim_run_start`): по каждому нику `fetch_author_all` (глубокий проход — до
  `GRAPH_DEEP_PAGES` страниц, лёгкий — `GRAPH_INCR_PAGES`), затем: апсерт рилсов, снимки,
  подписчики и имя из того же ответа, `last_scraped_at`, `provider='graph'`.
  Отсечение по дате (90 / 14 дней) — на нашей стороне по `timestamp`.
- `AuthorNotFound` → `provider='apify'`, `provider_checked_at=now()`; ник в этом же обновлении
  НЕ докупается через Apify молча — попадает в `errors` с текстом «аккаунт личный или не найден
  в официальном API — при следующем обновлении пойдёт через Apify (платно)». Перепроверка
  `apify`-источника через API — раз в `GRAPH_RECHECK_DAYS`.
- `RateLimited` → оставшиеся ники пачки и следующие graph-пачки закрываются ошибкой «лимит
  официального API, повторите через час»; Apify-пачки идут дальше.
- Цена graph-пачки = 0. `GET /refresh/estimate` дополняется полями `graph_sources`,
  `apify_sources`; `total_rub` считается только по apify-источникам.
- `MIN_REFRESH_INTERVAL_H` для graph-источников — 1 час (бесплатно), для apify — прежние 6.

### Автообновление

- `POST /api/radar/refresh/auto` — запускает обновление ТОЛЬКО graph-источников (Apify по
  расписанию не вызывается никогда — защита кошелька), у строки `radar_refreshes` новое поле
  `trigger`: `manual` | `auto`. Не чаще одного авто за 20 часов (по базе); если идёт другое
  обновление — ничего не делает. Ответ 200 в любом случае: `{"started": bool, "reason": "…"}`.
- `POST /api/radar/refresh/tick` — продвигает идущее обновление на шаг (тот же `current_step`),
  при простое — мгновенный no-op.
- pg_cron (миграция 0012, по образцу `0009_radar_cron.sql`): `radar-refresh-auto` — раз в сутки
  в 01:00 UTC (04:00 МСК) → `/refresh/auto`; `radar-refresh-tick` — раз в минуту → `/refresh/tick`.
- `GET /refresh/current` отдаёт `trigger`; полоса пишет «Обновлено N ч назад · авто».

### Разбор

- У рилсов из официального API нет `video_url`. Проверить `radar/downloader.py` (строка ~158 и
  `refresh_video_urls`): пустая ссылка обязана вести в тот же добор через Apify, что и
  протухшая. Если нет — довести. Стоимость добора ≈ 0,21 ₽ за рилс, только при разборе.

### Порог залётности

- `VIRAL_RATIO_THRESHOLD = 10.0`, `VIRAL_LEVELS = (10.0, 20.0, 50.0)` в `intel_settings.py`.
- В элемент ленты добавляется `viral_level`: `1 | 2 | 3 | null` (по `VIRAL_LEVELS`).
  `Feed.jsx` берёт класс из него: 1 → `v3`, 2 → `v5`, 3 → `v10` (имена классов CSS не менять),
  собственные сравнения с 5 и 10 из `Feed.jsx` убрать.

### Интерфейс

- Полоса обновления: при `total_rub == 0` и `to_refresh > 0` — «бесплатно · N источников»;
  при смешанном — «≈ X ₽ за K аккаунтов через Apify · остальные бесплатно».
- «Источники»: у `provider === 'apify'` рядом с ником `<span className="rs-badge low">через
  Apify</span>` с title «личный аккаунт: официальный API его не отдаёт, обновление платное».
- Длительности у рилсов из API нет — плашка `.rs-dur` не рисуется (уже так).

## Контракт

Миграция `supabase/migrations/0012_radar_graph.sql` (идемпотентно):

```sql
alter table public.radar_competitors
  add column if not exists provider            text check (provider in ('graph','apify')),
  add column if not exists provider_checked_at timestamptz;
alter table public.radar_refreshes
  add column if not exists trigger text not null default 'manual' check (trigger in ('manual','auto'));
alter table public.radar_scrape_runs drop constraint if exists radar_scrape_runs_kind_check;
alter table public.radar_scrape_runs
  add constraint radar_scrape_runs_kind_check check (kind in ('reels','followers','graph'));
-- + два задания pg_cron по образцу 0009 (unschedule if exists → schedule)
```

Имя ограничения `radar_scrape_runs_kind_check` проверить запросом к `pg_constraint` до
применения. Применение — `supabase db query --linked --workdir <корень> -f <файл>`, затем
`notify pgrst, 'reload schema'` (`db push` в проекте не работает, см. итог ТЗ 10).

Новые поля ответов: `/feed` items — `viral_level`; `/sources` active — `provider`;
`/refresh/estimate` — `graph_sources`, `apify_sources`; `/refresh/current` — `trigger`.
`errors[]` формы не меняет.

Переменные окружения Vercel (production): `META_IG_TOKEN`, `META_IG_USER_ID` — добавляет
владелец одной командой, значение идёт из `backend/.env` мимо чата:

```
cd backend && for k in META_IG_TOKEN META_IG_USER_ID; do grep -m1 "^$k=" .env | cut -d= -f2- | tr -d '"\n' | vercel env add $k production --cwd ..; done
```

После добавления переменных нужен новый деплой (пуш в main).

## Затрагивается

- Новые: `backend/app/radar/graph_api.py`, `backend/tests/test_radar_graph.py`,
  `supabase/migrations/0012_radar_graph.sql`.
- Правки: `radar/settings.py`, `radar/repo.py`, `radar/refresh_service.py`,
  `api/radar_refresh.py`, `radar/intel_settings.py`, `radar/feed.py`, `radar/repo_intel.py`,
  `radar/downloader.py` (если нужно), тесты; `radarTabs/Feed.jsx`, `RefreshBar.jsx`,
  `Sources.jsx`.
- НЕ трогаются: `core/config.py`, `models/schemas.py`, `api/client.js`, `workers/*`,
  `pipeline/*` (кроме чтения `shared_state`), `api/radar.py`, `radar.css`, ветка `module/scout`.

## Вне рамок

- Экран разведки парсера из ТЗ 05 (`scout_*`) — отдельный модуль, не сливается.
- Автообновление платных (Apify) источников.
- Графики динамики просмотров; поиск новых источников.

## Реализация

- **Модель сессии:** Opus — шаг 0, ревью, применение миграции, приёмка, мёрж; код — Sonnet.
- **Команда запуска:** `cd Парсер/worktrees/radar && claude` → `git fetch && git merge origin/main`.
- **Этапы:**
  1. Opus: шаг 0 (сверка просмотров). Стоп, если расхождение > 10 %.
  2. Параллельно, по файлам:
     - A (Sonnet): `graph_api.py`, `settings.py`, `repo.py`, `refresh_service.py`,
       `api/radar_refresh.py`, миграция 0012, `tests/test_radar_graph.py`, правки
       `tests/test_radar_refresh.py`.
     - B (Sonnet): `intel_settings.py`, `feed.py`, `repo_intel.py`, `downloader.py`,
       `tests/test_radar_feed.py`, `tests/test_radar_sources.py`, тест добора видео.
     - C (Sonnet): `Feed.jsx`, `RefreshBar.jsx`, `Sources.jsx`.
     Контракт между ними — раздел «Контракт» и новые поля ответов выше.
  3. После уведомлений от всех троих: pytest, сборка, приёмка скриптом (взять за основу
     сохранённый сценарий приёмки ТЗ 10, если ещё лежит в scratchpad; иначе написать заново
     по его описанию в `10-radar-intel.md`), миграция, переменные Vercel, мёрж, пуш.

## Критерии приёмки

1. pytest: новые тесты зелёные, прежние радарные не сломаны; `npm run build` проходит.
2. При пустом `META_IG_TOKEN` обновление ведёт себя ровно как в ТЗ 10 (тест).
3. На проде: `GET /refresh/estimate` для трёх нынешних источников показывает `total_rub: 0`;
   «Обновить» проходит без единого вызова Apify (в `radar_scrape_runs` только `kind='graph'`),
   цифры просмотров в ленте сходятся с Instagram.
4. Личный/несуществующий аккаунт получает `provider='apify'` и понятный текст в ошибках.
5. Утром после выкатки в `radar_refreshes` есть строка `trigger='auto'`, в
   `radar_reel_snapshots` — вторые замеры; в ленте есть рилсы с `rising_mode='history'`.
6. «Разобрать» рилс, пришедший из официального API (без `video_url`), доходит до отчёта.
7. Доля залётных на живых данных при пороге 10× — около 10 % (было 36 % при 3×).
