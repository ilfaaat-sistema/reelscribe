# ReelScribe

## Что это

ReelScribe превращает сохранённые ссылки на Instagram Reels/посты в базу расшифровок с аналитикой.
Пользователь скидывает ссылки (или экспорт переписки Instagram `message_1.json`) — сервис скачивает
аудио, распознаёт речь, переводит иностранное на русский, подтягивает метрики (просмотры, лайки,
комментарии, автор, подписчики) и показывает всё в таблице с режимом аналитики, карточкой каждого
рилса и экспортом. Видео нигде не хранится — только текст и метрики.

Полное ТЗ: [`docs/ТЗ_ReelScribe.md`](docs/ТЗ_ReelScribe.md). Правила и конвенции проекта: [`CLAUDE.md`](CLAUDE.md).
Визуальный эталон UI/UX: [`reelscribe_v12.html`](reelscribe_v12.html).

## Стек

- **Backend:** Python 3.11 + FastAPI, фоновый воркер на очереди.
- **БД:** Supabase (Postgres).
- **Скачивание Instagram:** каскад источников (Apify-профиль → instaloader → StarAPI/RapidAPI →
  yt-dlp → платный Apify-фолбэк) — см. §4/§6 ТЗ.
- **ASR:** переключатель `asr_mode` (`auto | mlx | cloud | faster-whisper`).
- **Frontend:** React 19 + Vite, без тяжёлых UI-китов.
- **Деплой:** фронт и API — Vercel (один проект `reelscribe`, два сервиса), воркер — GitHub Actions.

## Требования

- Python 3.12 (версия задана в `backend/.python-version`, ей же пользуется Vercel и GitHub Actions)
- Node.js (для сборки фронтенда, любая актуальная LTS-версия)
- Отдельно ставить **ffmpeg не нужно** — бэкенд использует `imageio-ffmpeg`, который тянет
  статический бинарник ffmpeg сам как Python-зависимость (см. `backend/app/pipeline/media.py`,
  `backend/app/pipeline/download.py`).

## Настройка

1. Скопировать `.env.example` в `backend/.env`:
   ```bash
   cp .env.example backend/.env
   ```
2. Заполнить `backend/.env` вручную (в git не коммитится, см. `.gitignore`).

**Обязательно для минимального запуска:**
- `SUPABASE_URL`, `SUPABASE_ANON_KEY` — без них бэкенд не стартует (`app/core/config.py`
  требует их без дефолта).

**Опционально** (без них соответствующие функции либо отключены, либо работают на бесплатных
фолбэках):
- `SUPABASE_SERVICE_ROLE_KEY` — для операций, требующих обхода RLS.
- `APIFY_API_TOKEN` / `APIFY_API_TOKENS` (через запятую — ротация) — основной дешёвый источник
  скачивания и подписчиков через Apify. Без токена каскад скачивания уходит сразу к yt-dlp.
- `RAPIDAPI_KEY` / `RAPIDAPI_KEYS` (через запятую — ротация) — StarAPI, платный резервный источник.
- `OPENAI_API_KEY` — облачная транскрипция (`asr_mode=cloud`/`auto` вне Apple Silicon). В проде
  (воркер на GitHub Actions) ключ намеренно не передаётся — там `ASR_MODE` жёстко выставлен в
  `faster-whisper`, чтобы не платить за ASR; переменная нужна только для локального запуска
  в `cloud`/`auto`-режиме.
- `DEEPL_API_KEY` — перевод через DeepL; без ключа перевод идёт через бесплатный Google
  (`deep-translator`, без ключа не нужен).
- `INSTAGRAM_COOKIES_FILE` — cookies для yt-dlp-фолбэка.
- `KAGGLE_API_TOKEN`, `KAGGLE_NOTEBOOK_ID` — батч-транскрибация `large-v3` на Kaggle GPU (фаза 3).
- `ANTHROPIC_API_KEY`, `YANDEX_SPEECHKIT_KEY`, `DEEPGRAM_API_KEY` — объявлены в настройках,
  в коде пока нигде не используются (запланировано/не реализовано).
- `AUDIO_TMP_DIR` (дефолт `/tmp/reelscribe_audio`), `WORKER_CONCURRENCY` (дефолт `2`).

## Запуск локально

Зависимости бэкенда разделены на два файла: `backend/requirements.txt` — то, что нужно API
(и что реально ставится на Vercel), `backend/requirements-worker.txt` — зависимости воркера
(наследует первый файл через `-r requirements.txt`, сверху — yt-dlp, faster-whisper, instaloader
и т.д.). Если локально нужен только API — достаточно `requirements.txt`; чтобы запустить ещё и
воркер — ставить `requirements-worker.txt`.

```bash
# зависимости API
cd backend && pip install -r requirements.txt

# зависимости воркера (если нужен локальный запуск воркера)
cd backend && pip install -r requirements-worker.txt
```

Три процесса, порты закреплены за проектом (см. `CLAUDE.md`, «Порты дев-серверов» — блок 5240–5249):

```bash
# бэкенд (API)
cd backend && uvicorn app.main:app --reload --port 5245

# фоновый воркер (скачивание + ASR) — опционально, см. «Деплой» про GitHub Actions
cd backend && python -m app.workers.run

# фронтенд
cd frontend && VITE_API_URL=http://localhost:5245 npm run dev -- --port 5240
```

**Дефолтный порт Vite (5173) не использовать** — на машине он занят другими проектами. Порт всегда
задавать явно.

**Процессы гасить только по своему PID**, никаких `pkill`/`killall` — они заденут дев-серверы
других проектов:
```bash
lsof -nP -iTCP:<порт> -sTCP:LISTEN     # PID слушателя
lsof -a -p <PID> -d cwd -Fn            # из какой папки он запущен
```

## Тесты и линт

```bash
cd backend && pytest
cd backend && ruff check .
cd frontend && npm run lint
cd frontend && npm run build
```

## Деплой

| Что | Где | Конфиг | URL |
|---|---|---|---|
| Фронтенд + API | Vercel, проект `reelscribe` (один проект, два сервиса) | `vercel.json` (корень репозитория) | https://reelscribe-ai.vercel.app |
| Воркер | GitHub Actions | `.github/workflows/worker.yml`, `.github/workflows/keepalive.yml` | — (раннер, публичного адреса нет) |

Деплой фронтенда и API автоматический при пуше в `main` репозитория
https://github.com/ilfaaat-sistema/reelscribe.git. Render из проекта убран полностью — старая
конфигурация (`render.yaml`) осталась только в истории git.

### Vercel: один проект, два сервиса

Корневой `vercel.json` описывает два **Vercel Services** внутри одного проекта — `frontend`
(root `frontend/`) и `backend` (root `backend/`, entrypoint `app.main:app`). Верхнеуровневые
rewrites разводят трафик: `/api/(.*)` → сервис `backend`, `/(.*)` → сервис `frontend`
(SPA-rewrite на `index.html` живёт внутри самого фронтенд-сервиса).

- Root Directory проекта Vercel = **корень репозитория** (не `frontend`, как было при Render) —
  каждый сервис собирается из своей папки через `root` в `vercel.json`.
- Один домен на фронт и API → **CORS в проде не нужен**, `VITE_API_URL` в проде пустой (фронт
  ходит на относительный `/api`). Переменная нужна только для локальной разработки, когда
  бэкенд поднят отдельно на порту 5245.
- Vercel не засыпает (в отличие от бесплатного тарифа Render) — холодный старт занимает секунды.
- Python на Vercel — **3.12** (версии 3.11 там больше нет), задан в `backend/.python-version`.
- Переменные окружения API на Vercel (дашборд проекта): `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
  `GITHUB_DISPATCH_TOKEN` (им API дёргает `repository_dispatch` у воркера при импорте и ретрае).
- Есть `.vercelignore` в корне репозитория — без него CLI выгружал на хостинг лишнее (личные
  выгрузки Instagram, `docs/`, `.github/`, `supabase/` — набегало до 60 МБ).

**Тариф Hobby и его лимиты:**
- Hobby разрешён только для **некоммерческого личного использования** — донаты, реклама, платная
  подписка запрещены условиями тарифа; для коммерческого использования нужен Pro.
- 300 секунд на выполнение функции, 2 ГБ памяти.
- **Жёсткий лимит 4.5 МБ на тело ответа** — поэтому экспорт постраничный, с пределом на партию
  (замер на боевых данных: 522 КБ на страницу, укладывается с запасом).

### Воркер: GitHub Actions

Репозиторий публичный → минуты GitHub Actions бесплатны без лимита.

- Запуск: по расписанию `*/5 * * * *`, вручную (`workflow_dispatch` со страницы Actions) и
  мгновенно через `repository_dispatch` — API дёргает воркер сам при импорте и при ретрае, не
  дожидаясь ближайшего пятиминутного тика.
- Раннер запускает `python -m app.workers.run --drain`: разбирает всё, что есть в очереди, и
  выходит. Без флага (при локальном запуске на Mac) поведение прежнее — бесконечный цикл.
- Распознавание — faster-whisper прямо на раннере (`ASR_MODE=faster-whisper` жёстко задан в
  workflow), за ASR в проде не платим. `OPENAI_API_KEY` туда намеренно не передаётся.
- `TRANSCRIBE_CONCURRENCY=2` на раннере; по умолчанию (локально, на Mac) — 1.
- `.github/workflows/keepalive.yml` раз в месяц включает `worker.yml` через API — GitHub
  автоматически отключает workflow с `schedule`-триггером после 60 дней без активности в
  репозитории, keepalive не даёт расписанию заснуть.
- Секреты воркера — в GitHub Secrets репозитория: `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
  `APIFY_API_TOKEN`, `APIFY_API_TOKENS`, `RAPIDAPI_KEYS`. (`RAPIDAPI_KEY` и `DEEPL_API_KEY` не
  заведены — значений нет, перевод идёт через бесплатный Google Translate.)

**Три способа запустить воркер** (все ходят в одну и ту же базу Supabase, взаимозаменяемы):

1. **GitHub Actions** — основной способ в проде, руками ничего запускать не нужно.
2. **Вручную через `workflow_dispatch`** — со страницы Actions в GitHub, если нужно разобрать
   очередь прямо сейчас, не дожидаясь cron или `repository_dispatch`.
3. **Локально на Mac** — `cd backend && python -m app.workers.run` (без `--drain`, бесконечный
   цикл). Полезно для отладки пайплайна вживую, но **больше не обязателен для работы прода** —
   раньше, пока воркера в проде не было вообще, локальный Mac был единственным способом разобрать
   очередь.

⚠️ **Не держать локальный воркер в бесконечном режиме одновременно с воркером на Actions.**
Двойной обработки одного и того же задания не будет — claim задания в очереди атомарный на
уровне БД. Но cooldown API-ключей (Apify/RapidAPI после 429) живёт в памяти каждого процесса
отдельно: два независимых воркера словят 429 по одному и тому же ключу порознь, не зная о
cooldown друг друга, — это продлевает простой квоты вместо того, чтобы переждать его один раз.

### Rate-limit по IP

Защита кошелька (кап 5 импортов за 10 минут с одного IP) переехала из памяти процесса в таблицу
`rate_limits` (миграция `supabase/migrations/0002_rate_limits.sql`, уже применена). На serverless
Vercel память между вызовами функции не сохраняется — лимит в памяти процесса иначе не работал бы.

### Стоимость

**$0/мес**: Vercel Hobby бесплатен в рамках лимитов выше, GitHub Actions бесплатны на публичном
репозитории, ASR в проде — faster-whisper без платного API.

### Как проверить, что прод жив

```bash
# API отвечает
curl -s https://reelscribe-ai.vercel.app/api/health

# фронт отдаётся (ожидаем 200)
curl -s -o /dev/null -w '%{http_code}\n' https://reelscribe-ai.vercel.app/

# воркер: последние запуски workflow (нужен gh CLI и доступ к репозиторию)
gh run list --workflow=worker.yml --limit 5
```

## Структура репозитория

```
backend/           FastAPI: app/ (api, core, models, pipeline, services, workers), tests/
frontend/           React+Vite: src/ (pages, components, api, lib)
docs/               ТЗ_ReelScribe.md — полная спецификация; claude-code-toolkit.md — инструменты разработки
reelscribe_v12.html  кликабельный прототип (визуальный эталон UI/UX)
vercel.json         конфиг деплоя фронтенда + API на Vercel (два сервиса в одном проекте)
.github/workflows/  worker.yml (воркер на GitHub Actions), keepalive.yml (не даёт расписанию заснуть)
.env.example        имена переменных окружения (без значений)
.claude/            settings.json (хуки/права), skills/ (конвенции проекта)
```

## Куда смотреть дальше

- [`CLAUDE.md`](CLAUDE.md) — правила и конвенции проекта (что можно/нельзя менять, терминология метрик).
- [`docs/ТЗ_ReelScribe.md`](docs/ТЗ_ReelScribe.md) — полное техническое задание (модель данных, API,
  пайплайн, экраны, дорожная карта, критерии приёмки).
- [`docs/claude-code-toolkit.md`](docs/claude-code-toolkit.md) — инструменты разработки, применяемые в проекте.
