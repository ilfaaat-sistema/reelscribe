# ТЗ 08. Радар — раздел разведки по конкурентам (`/radar`)

**Статус:** готово 24.09.2026

## Проблема / цель

Радар — отдельный локальный сервис (`../../Радар`, FastAPI + SQLite + React). Как он работает: собирает метаданные рилсов конкурентов через Apify, даёт выбрать лучшие, разбирает их (расшифровка + видеоанализ Gemini) и выдаёт отчёт. Живёт только на Маке: SQLite, поток-воркер, локальный Whisper, Vertex через gcloud ноутбука.

Цель — перенести Радар разделом в ReelScribe:
- тот же домен (`reelscribe-ai.vercel.app/radar`), общий бэкенд, общий Supabase;
- постоянный адрес, открывается с телефона;
- разбор за 30–60 с без очереди и холодного старта;
- бесплатно: Gemini по обычному ключу, потому что Vercel работает в США.

Данные из `radar.db` переносятся. Этот документ отменяет `Радар/docs/specs/deploy-timeweb-server.md` и `Радар/DEPLOY_PLAN.md`.

## Пользовательский сценарий

1. В шапке ReelScribe кнопка «Радар» ведёт на `/radar`.
2. Ввожу ники конкурентов (до 5), период (до 12 мес.), метрику. Жму «Собрать» — старт прогона Apify.
3. Страница опрашивает статус. Когда прогон готов, показываются сводка, корзины по просмотрам и лента рилсов с превью.
4. Отмечаю рилсы и режим (t — текст, v — видео, tv — оба). Жму «Разобрать»: рилсы разбираются по одному, статусы видны на карточках.
5. Клик по разобранному рилсу открывает отчёт: расшифровка с таймкодами, раскадровка, hook/structure/cta/идея формата. Есть скачивание `.md` по одному рилсу и пачкой.
6. Всё состояние живёт в адресе, `/radar?accounts=a,b&sort=likes&reel=XYZ`: ссылкой можно поделиться, после перезагрузки страница восстанавливается.

## Архитектура

- **Сбор:** API на Vercel запускает актор Apify асинхронно (`POST /v2/acts/{actor}/runs`). Каждый `GET /api/radar/scrape/{id}` опрашивает прогон. При `SUCCEEDED` датасет забирается и одной пачкой upsert-ится в `radar_reels`. Каждый вызов занимает 1–5 с. Подписчики собираются так же (`kind=followers`, актор `apify~instagram-profile-scraper`).
- **Превью:** существующий прокси `GET /api/thumb/{shortcode}` (`backend/app/api/thumb.py`), на фронте `thumbUrl(id)` из `frontend/src/api/client.js`. Файлы превью не скачиваются.
- **Разбор:**
  - `POST /api/radar/analyze` создаёт задания в `radar_jobs` и заранее одним вызовом Apify освежает протухшие ссылки на видео (срок жизни — параметр `oe` в ссылке: unix-время в hex);
  - фронт последовательно, по одному, вызывает `POST /api/radar/jobs/{reel_id}/run`;
  - функция захватывает задание условным update (`queued → in_progress`), качает mp4 в `tempfile.TemporaryDirectory()`, делает **один вызов Gemini** (расшифровка + видеоанализ), пишет `radar_analyses`. Временная папка удаляется всегда.
- **Страховка:** задание `pg_cron` раз в минуту делает `net.http_post` на `/api/radar/jobs/tick` (ставится отдельной миграцией после деплоя). `tick` берёт одно задание:
  - `queued`, если его никто не взял за 2 мин и `next_attempt_at <= now()`;
  - или `in_progress` дольше 6 мин: `attempts+1`, после 3 попыток ставится `failed`.
- **Gemini:** SDK `google-genai`, ключ `GEMINI_API_KEY` (только переменные Vercel), модель `gemini-2.5-flash`. Видео меньше 20 МБ идёт inline (`types.Part.from_bytes`), от 20 МБ — через Files API с ожиданием `ACTIVE` и удалением в `finally`. На 429/`RESOURCE_EXHAUSTED` задание возвращается в `queued`, `next_attempt_at = now + 60s`, статус анализа `rate_limited`. Vertex не используется.
- **Видео не хранится:** mp4 существует только во временной папке на время одного задания. Это единственное исключение из правила «качаем только аудио».
- **Лимиты** (auth нет, репозиторий публичный), считаются **по базе**, а не в памяти процесса:
  - сбор: не больше 5 ников, период 1–12 мес., не больше 6 прогонов в час по `radar_scrape_runs.created_at`;
  - разбор: не больше 100 новых заданий за сутки по `radar_jobs.created_at`.

## Контракт (фиксирован, агенты от него не отступают)

### Таблицы — `supabase/migrations/0008_radar.sql` (уже написана, применяет ведущая сессия)
- `radar_competitors`: `username` PK, `followers`, `followers_updated_at`, `added_at`.
- `radar_scrape_runs`:
  - `id` bigint identity, `kind` (`reels`|`followers`), `usernames text[]`, `period_months`;
  - `status` (`pending|running|done|error`);
  - `apify_run_id`, `apify_dataset_id`, `apify_token_ref`;
  - `results_count`, `cost_estimate_usd`, `error`, `created_at`, `finished_at`.
- `radar_reels`:
  - `id` (shortcode) PK, `username`, `url`, `caption`, `hashtags jsonb`, `music`;
  - `views bigint`, `likes`, `comments`, `duration_sec real`, `posted_at timestamptz`;
  - `video_url`, `scrape_run_id`, `updated_at`.
- `radar_analyses`:
  - `reel_id` PK, `mode` (`t|v|tv`), `status` (`queued|downloading|analyzing|rate_limited|done|error|cancelled`);
  - `transcript_segments jsonb [{start,end,text}]`, `transcript_engine`, `transcript_language`;
  - `visual_timeline jsonb [{t,text}]`, `hook`, `structure`, `cta`, `format_idea`, `error`, `updated_at`.
- `radar_jobs`:
  - `id uuid`, `reel_id`, `mode`, `state` (`queued|in_progress|done|failed|cancelled`);
  - `attempts`, `next_attempt_at`, `error`, `created_at`, `updated_at`.

### Настройки — `backend/app/radar/settings.py` (пишет агент A, B только импортирует)

`config.py` проекта **не трогать**: его параллельно меняет модуль scout.

```python
from app.core.config import settings as core  # supabase, apify-токены, github — оттуда
# собственные поля Радара читаются из окружения (os.getenv) с дефолтами:
GEMINI_API_KEY          = ""                     # env GEMINI_API_KEY
GEMINI_MODEL            = "gemini-2.5-flash"     # env RADAR_GEMINI_MODEL
MAX_USERNAMES           = 5
MAX_PERIOD_MONTHS       = 12
SCRAPES_PER_HOUR        = 6
ANALYSES_PER_DAY        = 100
REELS_PER_MONTH_ESTIMATE= 20
APIFY_COST_PER_REEL     = 0.0026
REEL_ACTOR              = "apify~instagram-reel-scraper"
PROFILE_ACTOR           = "apify~instagram-profile-scraper"
STALE_QUEUED_SEC        = 120
STALE_RUNNING_SEC       = 360
MAX_ATTEMPTS            = 3
INLINE_MAX_BYTES        = 20 * 1024 * 1024
```

### Эндпоинты `/api/radar/*` — `backend/app/api/radar.py` (агент A)

Формы ответов повторяют нынешний `Радар/backend/main.py`, чтобы фронт переносился почти без изменений.

| Метод и путь | Ответ |
|---|---|
| `GET /competitors` | `[{username, followers, followers_updated_at}]` |
| `GET /scrape/cost-estimate?usernames=a,b&period_months=3` | `{estimate_usd, n_reels}` |
| `POST /scrape {usernames:[], period_months}` | `{run_id}`. Ошибки: 400 — пусто или превышены лимиты (текст по-русски), 429 — часовой лимит, 502 — Apify |
| `POST /competitors/refresh-followers {usernames?:[]}` | `{run_id}` (kind=followers) |
| `GET /scrape/{run_id}` | `{id, kind, status, results_count, cost_estimate_usd, error, created_at}`. Побочный эффект: опрос Apify и приём датасета. Повторный вызов идемпотентен: переход `running→done` через условный update по status |
| `GET /summary?usernames=a,b` | `{total_reels, median_views, best_account, buckets:[{label,min,max,count}], accounts:[{username, followers, followers_updated_at, reels_count, median_views, median_likes, median_comments, reach, likes_rate, comments_rate}]}` |
| `GET /reels?usernames=&sort=views\|likes\|comments&order=desc&min_views=&max_views=&limit=200` | `[{…radar_reels, analysis_status, analysis_mode}]` |
| `POST /analyze {items:[{reel_id, mode}], force?:bool}` | `{queued:[reel_id], skipped:[reel_id]}`. skipped — уже `done` без force. 429 — суточный лимит |
| `POST /jobs/{reel_id}/run` | `{reel_id, status}` — синхронно выполняет `process_radar_job` (до ~90 с). Если задание уже захвачено или не `queued`, ответ сразу с текущим статусом |
| `POST /jobs/tick` | `{picked: reel_id \| null, status}` — одно зависшее задание (`claim_stale_job`) |
| `POST /analyze/cancel {reel_ids:[]}` | `{cancelled:[]}` |
| `GET /analyze/status?reel_ids=a,b` | `{reel_id: {reel_id, mode, status, error, updated_at}}`; для рилсов без разбора `status: "not_started"`; пустой список → 400 |
| `GET /reels/{id}/analysis` | `{reel, analysis}`, jsonb отдаётся массивами |
| `GET /reels/{id}/report.md`, `GET /export.md?reel_ids=` | `text/markdown`, `Content-Disposition: attachment` (генератор — из `Радар/backend/main.py:_build_report_md`) |

### Разбор — `backend/app/radar/pipeline.py` (агент B)
- `claim_job(reel_id) -> dict | None`: атомарно `queued → in_progress` (условный update по `state='queued'` и `next_attempt_at <= now()`), `attempts+1`.
- `claim_stale_job() -> dict | None`: правила страховки выше.
- `process_radar_job(job: dict) -> str`: возвращает итоговый статус анализа. Шаги:
  - статус `downloading` → скачивание (`downloader.download_to(tmpdir, reel)`) → статус `analyzing` → `gemini.analyze(video_path, mode)`;
  - запись результата → `radar_analyses.status='done'`, `radar_jobs.state='done'`;
  - отмена (`radar_jobs.state='cancelled'`) проверяется между шагами;
  - любая ошибка записывается человеческим текстом в `radar_analyses.error` и `radar_jobs.error`.
- `refresh_expired_links(reel_ids) -> None`: одним вызовом Apify reel-scraper (`username: [url, …]`) обновляет `video_url` рилсов с протухшим `oe`. Вызывает API в `/analyze`.
- `downloader.py`: `link_expired(url) -> bool` (параметр `oe`, запас 10 мин) → httpx по живой ссылке → при 403/протухшей освежить одну ссылку → повтор. yt-dlp не используется: на Vercel без cookies он бесполезен.
- `gemini.py`: `analyze(video_path, mode) -> dict` с ключами `transcript_segments, transcript_language, visual_timeline, hook, structure, cta, format_idea`. Сегменты проверяются на возрастание таймкодов и выход за длительность; при нарушении одна повторная попытка. `RateLimitedError` для 429.

### Фронт (агент C)
- `frontend/src/pages/Radar.jsx`: перенос `Радар/frontend/src/App.jsx`. Вся вёрстка под корневым `<div className="radar">`.
- `frontend/src/pages/radar.css`: стили Радара только под `.radar …`, цвета — токены ReelScribe из `index.css`. Без `:root`, `*` и `body`.
- `frontend/src/api/radar.js`: функции по эндпоинтам, база — как в `client.js` (`VITE_API_URL` + `/api/radar`).
- Состояние в адресе через `useSearchParams`: `accounts, period, metric, sort, view, buckets, reel`, дефолты в адрес не пишутся. Открытие отчёта — push, остальное — replace.
- Запуск разбора: после `POST /analyze` вызывать `/jobs/{id}/run` по одному, строго последовательно. Параллельно опрашивать `/analyze/status` раз в 2 с.
- После загрузки ленты статусы `done` восстанавливаются из `analysis_status` в ответе `/reels`.
- Убрать переключатель Whisper, `/api/config`, `PARSER_URL`, `/api/thumbs`.
- `frontend/src/App.jsx`: маршрут `/radar`; в шапке `RADAR_URL` и внешняя ссылка заменяются кнопкой `navigate('/radar')`; на `/radar` степпер не показывается.

## Затрагивается
- **Новые файлы:**
  - `backend/app/radar/*`, `backend/app/api/radar.py`, `backend/app/models/radar_schemas.py`;
  - `frontend/src/pages/Radar.jsx`, `radar.css`, `frontend/src/api/radar.js`;
  - `scripts/migrate_radar_sqlite.py`, тесты `backend/tests/test_radar_*.py`, миграции `0008_radar.sql` и `0009_radar_cron.sql`.
- **Правки в 2–3 строки:** `backend/app/main.py` (`include_router`), `frontend/src/App.jsx`, `backend/requirements.txt` (`google-genai`), `vercel.json` (`maxDuration: 300` для backend).
- **Не трогаем:** `config.py`, `schemas.py`, `index.css`, `client.js`, `worker.yml`, `backend/app/workers/*`, `pipeline/*`.

## Вне рамок
- Анализ комментариев (`comment_analyses`: фича в Радаре не реализована, таблица пустая).
- Фильтр ленты по периоду: в Радаре его нет, переносим поведение как есть.
- Авторизация или пароль — отдельной задачей, если начнут выбирать лимиты.
- Объединение с разведкой scout (ТЗ 05).

## Реализация

- **Модель:** ведущая сессия Opus (оркестрация, DDL, ревью, мёрж), код пишут 4 Sonnet-агента.
- **Этапы:**
  0. Opus: миграция `0008` через MCP после «да» владельца, проверка `list_tables`/`get_advisors`.
  1. Параллельно, файлы не пересекаются:
     - **A — API:** `radar/{settings,apify_runs,repo,scrape_service,analytics,report}.py`, `api/radar.py`, `models/radar_schemas.py`, `main.py`, `vercel.json`, `tests/test_radar_api.py`;
     - **B — разбор:** `radar/{pipeline,downloader,gemini}.py`, `requirements.txt`, `tests/test_radar_pipeline.py`;
     - **C — фронт:** `pages/Radar.jsx`, `pages/radar.css`, `api/radar.js`, `App.jsx`;
     - **D — перенос:** `scripts/migrate_radar_sqlite.py`.
  2. Opus, после отчёта всех четверых: ревью, `pytest`, `npm run build`, перенос данных (сначала dry-run), preview-деплой, переменные Vercel, миграция `0009_radar_cron.sql`.
  3. Приёмка скриптом Playwright, `/code-review high`, мёрж в main.

## Критерии приёмки
- Сбор по 1 нику за 1 месяц → строки в `radar_reels`. Повторный опрос не дублирует строки и не пересчитывает стоимость. Превышение лимитов даёт 400/429 с русским текстом.
- Разбор 1 рилса в режиме tv на проде укладывается в 90 с: сегменты с таймкодами + раскадровка. mp4 нигде не остаётся.
- Два одновременных `/run` одного рилса не разбирают его дважды. Брошенное задание добирает `tick`.
- Эталон: у `DYZou94oo9g` есть расшифровка Whisper. Сравнить её с Gemini и показать владельцу до мёржа.
- `/radar?accounts=…&reel=…` восстанавливает страницу. `/`, `/results`, `/history` ReelScribe не изменились.
- Перенесены 58 рилсов, 1 конкурент, 4 прогона, 1 разбор; тестовые `TEST_%` не переносились.

## Итог приёмки

- **Скорость:** разбор рилса в режиме tv на проде — 26 с на сбор данных, 24 с Gemini, всего ~50 с без учёта холодного старта. Мерили на Vercel с логом запроса `POST /api/radar/jobs/{reel_id}/run`.
- **Точность Gemini vs Whisper:** на эталоне `DYZou94oo9g` (реклама дупиков) Gemini точнее по мелочам («по пустякам» vs Whisper «по пустикам»), сегменты мельче, язык определён точно.
- **Боевой сбор:** фактический импорт аккаунта `directoreels` за 1 месяц — 20 рилсов, $0.052 на Apify.
- **Перенос данных:** из локального `radar.db` в Supabase успешно перенесены 1 конкурент, 4 прогона, 58 рилсов, 1 разбор. Структура не изменилась, таблицы взяли те же имена.
- **Код-ревью:** 10 замечаний при `code-review high`, все исправлены до мёржа.

## Итерация 2 (24.09.2026): оригинальный вид и механика локального Радара

Решение владельца: вернуть светлый дизайн и механику локального Радара внутри `/radar`.

**Контракт:**
- **Дизайн** (`frontend/src/pages/radar.css`): оригинальные стили `../../Радар/frontend/src/index.css` + `App.css` 1:1, каждое правило под `.radar`.
  - переменные из `:root` переходят в `.radar { … }`, правила `body` — тоже в `.radar`;
  - шрифты Unbounded, Manrope и JetBrains Mono подключаются `@import` Google Fonts в начале файла;
  - визуальный эталон — `../../Радар/reels-radar-prototype-v2.html`;
  - остальные страницы ReelScribe не меняются.
- **Своя шапка** (`Radar.jsx`): сразу под общей шапкой ReelScribe, разметка как в оригинале. `<div className="topbar"><div className="logo">Reels <b>Радар</b></div><div className="meta">…</div></div>`: в `meta` показывается «Рилсов в БД: N» из `summary.total_reels`, до загрузки — «Локальный аналитик рилсов конкурентов» (текст из оригинала, адаптировать: «Аналитик рилсов конкурентов»).
- **Период до 24 месяцев:** `MAX_PERIOD_MONTHS = 24` в `settings.py`, слайдер 1..24.
- **Выбор расшифровки** Gemini / Whisper — переключатель как в оригинале (сегменты «Gemini» | «Whisper (OpenAI)»):
  - `GET /api/radar/config` → `{whisper_available: bool}` (есть ли `OPENAI_API_KEY`). Без ключа Whisper недоступен и подписан «нет ключа OpenAI»;
  - `POST /api/radar/analyze` принимает `transcriber: 'gemini'|'whisper'` (по умолчанию gemini) и сохраняет его в `radar_jobs.transcriber` (миграция `0010_radar_transcriber.sql`, уже применена). `whisper` без ключа — 400;
  - в разборе при `transcriber='whisper'` для режимов t/tv расшифровку делает OpenAI `whisper-1` (`POST https://api.openai.com/v1/audio/transcriptions`, `response_format=verbose_json`, mp4 отправляется как есть, лимит 25 МБ — иначе понятная ошибка) через новый `backend/app/radar/whisper.py`: `transcribe(video_path) -> (segments, language)`;
  - для tv видеоанализ по-прежнему делает Gemini, но в режиме `v` (без расшифровки), для t Gemini не вызывается вовсе;
  - `transcript_engine = 'openai-whisper'`; 429 OpenAI — тот же путь отложенного повтора, что у Gemini.
