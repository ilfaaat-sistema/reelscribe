# Контракт перевода подписи карусели

23.10.2026. Caption backend переведён на утверждённый Gemini через Vertex. API, хранилище, хэш, 15-минутная аренда, fencing и невозвратный учёт стартов сохранены; новых миграций нет. Управление внешними env/IAM/БД и публикацией остаётся у корня.

## Источники и границы

- Основное окно — `frontend/src/pages/Carousels.jsx`. Переводится только сохранённый `caption`; OCR и новое получение Instagram не входят в задачу.
- Точный исходник берётся тем же серверным resolver, что существующий GET `backend/app/api/carousels.py:detail`: живые `14d`/`30d` — сохранённый `state.json` ветки `data/carousels-favorites-live`, затем существующий архивный fallback внутри выбранного окна; `7d`/`archive14d` — опубликованные архивы; пустой период — существующий автоматический выбор и совместимость старых ссылок. Порядок и семантика выбора источника не меняются.
- `backend/app/api/carousel_favorites.py:live_detail` читает сохранённую ветку GitHub, не Meta. `_rolling_detail` затем проверяет архивы в границах живого окна. `_report` кэшируется на 60 секунд; версия подписи соответствует доступному сохранённому ответу этих функций, а не независимо проверенному состоянию Instagram.
- Отбор хранится в `public.carousel_likes.snapshot`. Он имеет отдельный `SelectedCarousels.jsx:Drawer`, который не переиспользует основное окно. Поэтому UI перевода в отборе не добавляется. Нельзя незаметно подменять недоступный источник основного окна старым snapshot: возможна другая подпись; кроме того, публичная существующая RPC принимает снимок клиента. Такой snapshot не разрешён как произвольный источник нового платного перевода. Расширение второго окна требует отдельного контекста источника и контракта.
- Запрос никогда не принимает `caption`, `text`, URL, snapshot или имя переводчика. Pydantic `extra='forbid'`; ID — строка `[0-9]{1,40}`.
- `source_hash` = lowercase SHA-256 точных UTF-8 байтов сохранённой подписи: без `strip`, NFC, изменения пробелов и переносов. Для отсутствующей/пустой подписи хэш вычисляется от `caption or ''`. Переводчик вправе обрезать края при переводе, ключ кэша — точный оригинал. ID вместе с hash разделяет версии, период в ключ не входит. Одинаковая версия публикации между периодами переиспользует результат; старая архивная версия не получает перевод новой подписи.

## GET деталей — совместимое расширение

`GET /api/radar/carousels/{media_id}?period=14d`

Все прежние поля сохраняются. Добавляется одно поле:

```json
{
  "caption_translation": {
    "source_hash": "64 lowercase hex",
    "state": "available",
    "translation": null,
    "source_language": null
  }
}
```

| state | Значение и UI |
|---|---|
| `ready` | Есть прошедший проверку перевод для точных ID/hash. `translation` — непустая строка; показывать русский с переключателем на оригинал. |
| `available` | `needs_translation(caption)` истинно, длина допустима, хранилище и конфигурация Vertex доступны, готового перевода нет. Показать кнопку «Перевести». |
| `not_needed` | Пустой, русский или короткий исходник по существующему `needs_translation`; кнопки нет. |
| `unavailable` | Иностранная подпись есть, но кэш/серверный ключ недоступен либо превышен предел длины. Оригинал доступен; показать ненавязчивую недоступность перевода. |

Язык — lowercase код из ответа Vertex либо `null`, отдельное определение языка не запускается. При отсутствии результата `translation` и `source_language` равны `null`. GET никогда не вызывает переводчик, не резервирует квоту, не создаёт строки БД. Ошибка **только новой** таблицы/ключа/кэша не превращает существующие детали в 503: вернуть оригинал и `state=unavailable`. Ошибка самого существующего источника сохраняет прежний HTTP-ответ. Новые ответы деталей и POST — `Cache-Control: no-store`.

## POST по явному нажатию

`POST /api/radar/carousels/{media_id}/translate`

```json
{"period":"14d","source_hash":"64 lowercase hex"}
```

`period`: `7d | 14d | 30d | archive14d | null`, при отсутствии трактуется как `null`; `source_hash` обязателен. GET и POST должны использовать один resolver исходника без рекурсивного чтения кэша. Сервер сам читает сохранённый источник и сравнивает его hash с ожидаемым до резервирования и вызова переводчика.

Успех HTTP 200:

```json
{
  "id":"17889656370622037",
  "caption_translation":{
    "source_hash":"64 lowercase hex",
    "state":"ready",
    "translation":"Русский перевод",
    "source_language":"en"
  },
  "cached":false
}
```

`cached=true` означает чтение готового результата без переводчика и без нового списания лимита. POST не принимает переключение языка: цель всегда русский. Совпадение hash проверяется и при чтении кэша; результат проходит существующий `translation_failure_reason`. Непригодный кэш не выдаётся как `ready`.

Ошибки содержат `detail` — понятную русскую строку и верхнеуровневый `code` — стабильный идентификатор. Новый service не возвращает и не логирует сырые исключения, исходный текст, URL с токеном и ответы провайдера: в его логах только безопасная причина, provider/model, длительность, finish enum и whitelisted числовые usage metadata. Старый pipeline рилсов не меняется; из него переиспользуются только чистые проверки needs_translation/translation_failure_reason.

| HTTP | code | Значение |
|---|---|---|
| 404 | `carousel_not_found` | Публикации нет в выбранном сохранённом источнике. |
| 409 | `source_changed` | Ожидаемый hash не совпадает или источник изменился во время работы. Нужно перечитать детали; автоматически заново не переводить. |
| 409 | `source_missing` | Сохранённая подпись отсутствует/пуста. |
| 409 | `translation_not_needed` | Существующее правило считает исходник русским/коротким. |
| 409 | `translation_in_progress` | Для ID/hash уже идёт перевод; `Retry-After: 5`. |
| 413 | `source_too_long` | Больше 12 000 символов. |
| 422 | `invalid_request` | Неверный ID/период/hash, лишние поля; стандартную детализацию валидации можно сохранить, клиент имеет запасное русское сообщение. |
| 429 | `translation_rate_limited` | Общий лимит новых вызовов/одновременных работ; `Retry-After` из резерва БД. |
| 429 | `translation_quota_exceeded` | Явный HTTP429 Vertex; попытка закрывается с outcome=quota, без fallback. |
| 502 | `translation_failed` | Явный отказ провайдера/схемы/качества либо неизвестный transport-исход; без повтора. |
| 503 | `translation_unavailable` | Недоступны конфигурация/OIDC/STS/IAM или БД; до reserve без попытки. Явные Vertex401/403 после reserve также 503 с закрытием попытки. |
| 503 | `carousel_source_unavailable` | Недоступен существующий источник; не вызывать переводчик. |

На 409/429/502/503 исходник остаётся видимым, ошибка рядом с действием, повтор только вручную. Клиент не должен программно повторять платный POST. Закрытие/смена ID, периода или hash инвалидирует ответ старого запроса, даже если AbortController не остановил сервер. Клиент принимает успешный ответ только при совпадении ID и исходного hash. Список используется как временный контент до GET деталей; кнопка появляется только после ответа деталей. Если GET старой версии API не содержит `caption_translation`, оригинал продолжает отображаться без кнопки.

## Постоянное хранение и полномочия

Предлагаемые объекты только в `public`; существующие reels/transcripts, rate_limits, carousel_likes и настройки Auth не изменяются:

1. `carousel_caption_translations`: `instagram_media_id text`, `source_hash text`, `translation text not null`, `source_language text null`, `created_at timestamptz`, `updated_at timestamptz`; PK `(instagram_media_id,source_hash)`. CHECK ID/hash, непустой перевод; готовая запись — только после проверки переводчика. Полный оригинал дополнительно не хранится.
2. `carousel_caption_translation_attempts`: `id uuid` (случайный token владельца), `instagram_media_id text`, `source_hash text`, `started_at timestamptz`, `lease_until timestamptz`, `finished_at timestamptz null`, `outcome text null`. Индексы по времени и незавершённым ID/hash. Счётчик ведётся отдельными попытками, чтобы повтор и смена hash не стирали расход последнего часа.
3. Узкие RPC резервирования и завершения: `reserve_carousel_caption_translation(p_id,p_source_hash,p_attempt_id)` и `finish_carousel_caption_translation(p_attempt_id,p_translation,p_source_language,p_outcome)`. Параметр source length допустимо добавить для защиты инварианта, но текст запроса не передаётся. Бизнес-проверки источника находятся на сервере; эти RPC не публичный вход.

RLS включён; все права `PUBLIC`, `anon`, `authenticated` на новые таблицы/RPC отозваны; только `service_role` получает необходимые SELECT/INSERT/UPDATE/DELETE таблиц и EXECUTE функций. RPC — `SECURITY INVOKER`, `search_path=''`, полные имена объектов. Функции по умолчанию не должны остаться исполняемыми PUBLIC. Новый отдельный серверный клиент использует существующее поле `settings.supabase_service_role_key`; `get_db()` и его anon-права остальных функций не меняются. Ключ отсутствует — GET fail-open, POST fail-closed. Секрет не передаётся браузеру, в URL или логи.

## Расход, конкуренция и неопределённый исход

`reserve` одной транзакцией под общим `pg_advisory_xact_lock`:

1. Проверяет готовый кэш ID/hash и возвращает его без новой попытки.
2. При незавершённом владельце того же ID/hash возвращает `in_progress`.
3. Проверяет максимум **2** действующих аренды глобально для этой фичи и максимум **12** новых попыток за скользящий час по времени БД. После успешного auth и reserve выполняется ровно один generate_content; SDK attempts=1, chunk/retry/fallback нет. Это отдельный лимит caption: старый процессный лимит рилсов сохраняется. Длина источника ограничена **12 000** символами до reserve. Повторное чтение и неподлежащий переводу текст расход не увеличивают.
4. Создаёт попытку до вызова провайдера. Ошибки и квота считаются попыткой; возврата счётчика нет. Завершённые/истёкшие записи старше суток очищаются внутри reserve, без удаления окна часа и действующих аренд.

`finish` под тем же lock проверяет token владельца; только его результат может создать готовый кэш и закрыть попытку. Ошибка закрывает попытку без перевода. Сбой записи после оплаченного перевода — 503, без выдачи несохранённого успеха и без автоматического повтора. Перед сохранением исходник перечитывается тем же resolver. Если hash изменился, попытка закрывается без записи результата и возвращается `source_changed`; новый текст автоматически не переводится.

Аренда действует **15 минут**. После истечения новый reserve может выдать новый token, но прежняя попытка остаётся в часовом журнале. Finish проверяет ID, hash, token и неистёкшую аренду; старый владелец не может изменить новый кэш или попытку. Автоматического повтора POST нет.

**Неоднозначный transport-исход:** внешний async deadline65s, HTTP timeout60s. Timeout/network/cancel после отправки сохраняет попытку и lease до штатных15минут; finish failed не вызывается. Лимит2 относится к действующим арендам; прекращение удалённой обработки после timeout не доказано. Явные provider/schema/quality-ошибки закрывают аренду, исходник не кэшируется.

## Vertex и request-scoped OIDC

Отдельные `carousel_caption_settings/auth/vertex.py`, env namespace `CAROUSEL_CAPTION_`, flag `TRANSLATION_ENABLED=false` по умолчанию. Утверждены `gemini-3.1-flash-lite`, location `global`. Готовый валидный кэш возвращается до проверки конфигурации и auth; GET никогда не вызывает STS/модель. Рилсы продолжают прежний DeepL/Google.

POST передаёт только `Request.headers.get('x-vercel-oidc-token')` во внутренний auth. Точный runtime `VERCEL_ENV` должен быть preview/production и совпадать с JWT environment/sub; также проверяются RS256+kid, issuer/audience, owner/project IDs и имена, exp/iat/nbf. Проверка payload предварительная; Google STS проверяет подпись при обязательном refresh до reserve. Credentials, supplier и токен принадлежат одному HTTP-запросу; ADC/API key/Developer API fallback нет. STS и IAM endpoints фиксированы, impersonation email ограничен настроенным Google project. Transport10s на hop, trust_env=false, без transport retry.

Вызов `genai.Client(vertexai=True, credentials=..., project=..., location=...)`: SDK attempts1, один candidate, max_output_tokens8192, JSON schema `{text_ru: string<=60000, source_language: lowercase code|null}`, extra forbidden. Caption передаётся JSON-данными отдельно от system instruction; commands внутри него переводятся, не исполняются. Tools отключены. Thinking явно не задаётся. Принимается только STOP, без blocked feedback, затем прежний quality gate с caption-only усилением короткого текста и fenced finish после перечитывания точного source/hash. Новых миграций и сброса старого пригодного кэша нет.

## Исторические факты подготовки 20.10.2026

Это исходная диагностика, не текущий статус внешней среды; актуальное состояние и выкаты ведёт корень в документе состояния.

- Worktree: `worktrees/carousel-caption-translation`; ветка `feature/carousel-caption-translation`, база свежего `origin/main` — `2533afe`. Fetch выполнен; чужие файлы main не менялись. Копия ТЗ 31 перенесена.
- Supabase CLI **2.109.0**, штатная авторизация работает. Linked проект **kcfkhebtlyqgbzaajgiv**, ACTIVE_HEALTHY, Postgres 17. Read-only SQL подтвердил `public.carousel_likes`: RLS включён, SELECT anon разрешён, INSERT anon/authenticated запрещён. Колонки: `instagram_media_id`, `selected_at`, `active`, `snapshot`, `source_period`. Сейчас 2 сохранённых строки с caption, активных 0. Содержимое подписей из БД не выводилось.
- `public.carousel_caption_translations` отсутствует. В истории БД **83** записи; локальные `0008`–`0011` не совпадают по версиям с общей историей. В БД есть миграции других приложений. Из текущего каталога нельзя выполнять обычный db push без сверки версий истории и изоляции единственной новой ожидающей миграции. Для review подготовлен отдельный staging с83 применёнными версиями и новым файлом; byte-аудит SQL всех83 записей не заявляется. Существующую историю не исправляли; repair/fetch всей истории/прямой SQL вместо db push не выполнялись.
- Метаданные Vercel production проекта `reelscribe` проверены через `vercel env ls production`: `SUPABASE_URL`/`SUPABASE_ANON_KEY` существуют, **SUPABASE_SERVICE_ROLE_KEY отсутствует**. Значения секретов не запрашивались; `.env` не открывались. Добавление серверного ключа штатным скрытым интерфейсом — отдельное условие публикации, не локальной реализации.
- Read-only production API уже сохранённых данных подтверждает: `14d` — 41 карточка, `30d` — 76; архивы `7d` — total 1404, `archive14d` — total 1212. Выборки по 100 архивных карточек содержат caption у 99. Все ID — строки. Ответы и сводка сохранены в `.baseline/carousel-caption-translation/source-check/`. Meta и переводчик не вызывались.
- Реальный пример со скриншота сохранён корнем: `.baseline/carousel-caption-translation/reference-item.json`, ID `17889656370622037`, caption 935 символов. Использовать локальный fixture, повторно не скачивать.
- Исходное подключение интеграций не менялось: Supabase доступен через уже авторизованный CLI, Supabase MCP-инструментов в этой сессии нет; локальные devtools/meta-ads выключены, node_repl включён. Новые MCP/аккаунты не включались. Браузер и визуальная приёмка — владение корня; он сохранил baseline отдельно.

Навык Supabase прочитан. Markdown-версии changelog/docs веб-инструмент не принял по content-type; HTML [changelog](https://supabase.com/changelog), [RLS](https://supabase.com/docs/guides/database/postgres/row-level-security) и [functions](https://supabase.com/docs/guides/database/functions) доступны. Октябрьское уведомление о TLS касается HTTP API, не меняет выбранную схему прав. До создания SQL перечитать применимые гарантии и использовать штатный `supabase migration new`; применение — отдельное разрешение и db push после всей сверки.

## Владение и проверки

Backend: новый сервис/маршрут, серверный cache client, подключение main, расширение details, новая миграция, backend-тесты и API-зависимости переводчика. Для прежнего reels в `backend/requirements.txt` сохраняются worker-минимумы `deep-translator==1.11.4`, `deepl==1.32.0`: lazy import HTTP-маршрута требует их на Vercel; прежде они были только в requirements-worker.txt. Frontend: только утверждённые api/carousels.js, Carousels.jsx, carousels.css и их тесты. Общие API-изменения — через корень. SelectedCarousels.jsx не меняется.

Проверки backend: только серверный источник, запрет extra text; готовый кэш без вызова; точный hash и смена caption; повтор после открытия; русское/пустое/короткое/длинное; непригодный перевод и квота; недоступный ключ/cache fail-closed POST и fail-open GET; конкуренция двух инстансов; токен владельца/таймаут/ошибка записи; границы 12/час и 2 активных; все периоды и старые прямые ссылки; существующие translation API/translate tests. Реальные права и миграция проверяются только на разрешённом этапе выката.

Локальная проверка Vertex 23.10.2026 использует существующий изолированный venv без изменения его пакетов: google-genai2.31.0, google-auth2.62.0, requests2.34.2. Минимумы SDK/auth подняты до фактически проверенных API (HttpRetryOptions, async transport, SubjectTokenSupplier), requests объявлен прямой зависимостью. Реальные provider calls не выполнялись; MockTransport проверяет фактические Vertex endpoint, timeout60s и единственный запрос приHTTP500.

Согласованное корнем усиление качества: для needs_translation=true и исходника короче80символов после прежнего gate требуется минимум ceil(0.2×число букв исходника) букв в переводе. Emoji/пунктуация не увеличивают порог. Единый caption_failure_reason используется для ответа модели, ready-кэша и удаления непригодного кэша; старый reels helper не меняется. Это структурная защита от почти пустого результата, без обещания семантической полноты.
