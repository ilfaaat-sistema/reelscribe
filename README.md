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
- **Деплой:** фронт — Vercel, API и воркер — Render.

## Требования

- Python 3.11
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
- `OPENAI_API_KEY` — облачная транскрипция (`asr_mode=cloud`/`auto` на Linux/Render); без ключа
  `auto` на сервере уходит на faster-whisper CPU.
- `DEEPL_API_KEY` — перевод через DeepL; без ключа перевод идёт через бесплатный Google
  (`deep-translator`, без ключа не нужен).
- `INSTAGRAM_COOKIES_FILE` — cookies для yt-dlp-фолбэка.
- `KAGGLE_API_TOKEN`, `KAGGLE_NOTEBOOK_ID` — батч-транскрибация `large-v3` на Kaggle GPU (фаза 3).
- `ANTHROPIC_API_KEY`, `YANDEX_SPEECHKIT_KEY`, `DEEPGRAM_API_KEY` — объявлены в настройках,
  в коде пока нигде не используются (запланировано/не реализовано).
- `AUDIO_TMP_DIR` (дефолт `/tmp/reelscribe_audio`), `WORKER_CONCURRENCY` (дефолт `2`).

## Запуск локально

Три процесса, порты закреплены за проектом (см. `CLAUDE.md`, «Порты дев-серверов» — блок 5240–5249):

```bash
# бэкенд (API)
cd backend && uvicorn app.main:app --reload --port 5245

# фоновый воркер (скачивание + ASR)
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
| Фронтенд | Vercel, проект `reelscribe` | `frontend/vercel.json` (`VITE_API_URL` зашит в `build.env`) | https://reelscribe-app.vercel.app |
| API | Render, сервис `reelscribe-api` | `render.yaml` | https://reelscribe-api.onrender.com |
| Воркер | Render, сервис `reelscribe-worker` | `render.yaml` | — (фоновый процесс, без публичного URL) |

Оба Render-сервиса и Vercel-проект деплоятся автоматически при пуше в `main` репозитория
https://github.com/ilfaaat-sistema/reelscribe.git. Секреты с `sync: false` в `render.yaml`
(`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `APIFY_API_TOKEN`, `RAPIDAPI_KEY`, `RAPIDAPI_KEYS`,
`OPENAI_API_KEY`, `DEEPL_API_KEY`) задаются вручную в дашборде Render — они не приходят из `render.yaml`.

## Структура репозитория

```
backend/          FastAPI: app/ (api, core, models, pipeline, services, workers), tests/
frontend/          React+Vite: src/ (pages, components, api, lib)
docs/              ТЗ_ReelScribe.md — полная спецификация; claude-code-toolkit.md — инструменты разработки
reelscribe_v12.html  кликабельный прототип (визуальный эталон UI/UX)
render.yaml        конфиг деплоя API + воркера на Render
.env.example       имена переменных окружения (без значений)
.claude/           settings.json (хуки/права), skills/ (конвенции проекта)
```

## Куда смотреть дальше

- [`CLAUDE.md`](CLAUDE.md) — правила и конвенции проекта (что можно/нельзя менять, терминология метрик).
- [`docs/ТЗ_ReelScribe.md`](docs/ТЗ_ReelScribe.md) — полное техническое задание (модель данных, API,
  пайплайн, экраны, дорожная карта, критерии приёмки).
- [`docs/claude-code-toolkit.md`](docs/claude-code-toolkit.md) — инструменты разработки, применяемые в проекте.
