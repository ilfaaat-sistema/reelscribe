# Независимая приёмка перевода обложек каруселей

Контракты: `docs/specs/32-carousel-cover-translation.md` от 20.10.2026 и `docs/specs/32-carousel-cover-live-contract.md` от 23.10.2026. Проверки выполнены без сети, платных API, секретов и облачных изменений.

## Матрица live 500/day от 23.10.2026

| Область | Проверка | Критерий | Уровень | Статус |
|---|---|---|---|---|
| Pilot → live | Исчерпать pilot 20, переключить закрытой RPC | Lifetime-счётчик и строки не обнуляются; live допускает ещё 480 попыток в те же московские сутки | PGlite + SQL review | Пройдено |
| Live 500 | Попытка №501 текущих московских суток | `limit_reached`, `retry_after` равен ближайшей 00:00 Europe/Moscow в ISO UTC; бюджет не тратится | PGlite | Пройдено |
| Полночь | Ранее зарезервированные строки перенести до 00:00 MSK | Новые сутки дают новый daily-бюджет; lifetime-счётчик и кэш сохраняются | PGlite | Пройдено |
| Слоты | Два `running`/unknown outcome удерживают слоты при переключении режима и полуночи | Третья резервация `busy`; ни режим, ни сутки не освобождают unknown-слот | PGlite + service | Пройдено |
| Права | anon/authenticated вызывают reserve/finish/unavailable/switch-mode и читают таблицы | Все операции отклонены; только service_role может переключить live | PGlite roles/RLS/grants | Пройдено |
| Бесплатные исходы | `image_unavailable` без claim token | Не входит в pilot/lifetime/daily-счётчики | PGlite | Пройдено |
| GET budget | Прочитать missing при pilot/live limit | Pilot: `retry_after=null`; live: ближайшая московская полночь. Готовые/терминальные строки не заменяются лимитом | API/service | Пройдено |
| Frontend retry | `limit_reached` с будущим `retry_after` | До срока нет POST; после срока сначала GET, затем POST только для `missing` + visible + enabled | `node:test`, real store seams | Пройдено |
| Hidden/focus | Срок наступает в hidden; focus/visibilitychange после долгой паузы | Hidden не делает GET/POST; wake сверяет текущее время; timeout не превышает int32 | `node:test`, fake clock | Пройдено |
| Frontend cache | Готовый/терминальный item и pilot `retry_after=null` | Кэш не перезапускается; pilot-limit не опрашивается бесконечно | `node:test`, HTTP normalization + store | Пройдено |
| OIDC | Wrong issuer/audience/subject, expired token, refresh failure | До модели безопасная 503; нет ADC/API-key fallback; token не попадает в ответ/лог/БД | `pytest`, fake credentials/Request | Пройдено локально |

## Матрица

| Область | Проверка | Критерий | Уровень | Статус |
|---|---|---|---|---|
| Атомарность 2/20 | Одновременно зарезервировать три разные обложки | Ровно два захвата, третий `busy`; бюджет не тратится дважды | PGlite 0.5.10, реальный PostgreSQL-совместимый SQL | Пройдено |
| Атомарность 2/20 | 20 успешных резерваций и 21-я | 21-я возвращает `limit_reached`; смена `prompt_version`/модели не сбрасывает счётчик | PGlite 0.5.10, реальный PostgreSQL-совместимый SQL | Пройдено |
| Идемпотентность | Два захвата одних `media_id`/version | Одна фактическая попытка; повтор получает текущий статус | SQL + API | Пройдено |
| Multi-tab dedupe | Два независимых клиента одновременно POST-ят один ID/version | Два POST допустимы; атомарная БД выдаёт claim только одному, второй получает `running`/cache; модель вызвана ровно один раз | SQL + два параллельных service-вызова | Пройдено |
| SDK no retries | Модельный транспорт сразу выдаёт timeout/503 | Один вызов; ошибка терминальна; резерв попытки не возвращается | `pytest`, инъекция fake client и проверка конфига SDK | Пройдено |
| URL / SSRF | `http`, loopback/private IP, неразрешённый host, HTTPS redirect на private host | Отказ до загрузки запрещённого адреса и до резервации модели | `pytest` + `httpx.MockTransport` | Пройдено |
| URL / payload | Неверный MIME, oversized body, долгая загрузка | `image_unavailable`/безопасная 503 по контракту; модель не вызвается | `pytest` + streaming mock | Пройдено в авторских тестах |
| API trust boundary | POST с клиентским URL/чужим ID, GET >100 ID | 422/404; URL не принимается; модель и загрузчик не вызваются | FastAPI `TestClient` | Пройдено |
| RLS / grants | anon и authenticated пытаются SELECT/INSERT/UPDATE таблиц и EXECUTE RPC | Все операции запрещены; service-role путь отделён от anon клиента | Реальный PostgreSQL с ролями; дополнительно static SQL review | Пройдено в PGlite |
| 500 ms / hidden tab | Mount hidden; visible 499 ms; hide before deadline; repeat visibility | В hidden нет GET/POST; на 499 ms нет POST; hide сбрасывает отсчёт; POST возможен только после новых 500 ms непрерывной видимости | `node:test`, управляемые timers/visibility | Пройдено |
| Кэш / scheduling | Несколько ID и периодов, `ready`/`running`/терминальный статус | GET пакетный; `ready` и терминальные не POST-ятся; `running` только опрашивается GET; смена CDN URL/периода не запускает новую попытку | API + hook | Пройдено |

## Доступные тестовые средства

- Backend: установлен `pytest`; в проекте уже используются FastAPI `TestClient`, `monkeypatch`, in-memory fake Supabase и `httpx.MockTransport`.
- Frontend: Node.js v24.16.0 и нативный `node:test`; существующие hook-тесты исполняют минимальную React-fixture без новых зависимостей.
- `frontend/node_modules` и backend `.venv` в этом worktree отсутствуют; нативные frontend-тесты от них не зависят.
- Доступен PGlite 0.5.10 в локальном временном каталоге; в нём исполнены фактическая миграция, роли, RLS-отказы и проверки атомарного лимита. `psql`, `postgres`, `initdb`, `psycopg` и `testing.postgresql` не обнаружены.

## План файлов приёмки

- `backend/tests/test_cover_translation_acceptance.py`: API/OIDC trust boundary, redirect SSRF, no-retry/timeout, Gemini payload, multi-tab claim, pilot/live SQL-лимиты, московская полночь и RLS.
- `frontend/src/api/carouselCoverAcceptance.test.js`: HTTP normalization, store и visibility gate; batch GET, 500 ms, `retry_after`, hidden/focus, int32 timer cap, кэш и read-only polling.

## Результат

Проверен integration commit `7ee2806`, включающий OIDC `d627918`, frontend retry `e8e17b9` и live-бюджет `7ee2806`.

- `backend/tests/test_cover_translation_acceptance.py`: 13 passed. Дополнительно к базовой защите проверены pilot20 → live500 с учётом пилота, 501-я попытка, московская полночь, unknown-слот, закрытые RPC, OIDC claims/expiry/refresh и payload `gemini-3.8-flash` с `thinkingLevel=LOW` без `temperature`.
- `frontend/src/api/carouselCoverAcceptance.test.js`: 7 passed. Проверены 500 ms/hidden, `retry_after` с порядком GET → POST, focus/visibility, int32 cap, pilot terminal, кэш, UTC normalization и лимит двух клиентских запросов.
- Точечные регрессии затронутых файлов: 59 backend-тестов и 24 frontend-теста passed. Frontend build прошёл; lint завершился с кодом 0 и восемью существовавшими `no-unused-vars` warnings в незатронутых файлах.

Внешние Vertex/STS/Supabase вызовы и production migration не выполнялись. PGlite проверяет SQL, роли и RLS-отказы локально, но не доказывает реальную многосоединительную нагрузку. Её нужно проверить после применения миграции без модели. Реальные WIF/IAM, billing, pilot20, desktop/mobile и production остаются за корневой приёмкой по контракту.
