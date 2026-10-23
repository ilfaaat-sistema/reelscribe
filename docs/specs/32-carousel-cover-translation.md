# Перевод крупного текста обложек каруселей

Утверждено пользователем 20.10.2026. Реализация в feature/carousel-cover-translation; production и основной merge после отдельной приёмки. Перевод caption из ТЗ 31 — другая задача.

## Поведение

Только крупный заголовок/подзаголовки первого изображения, русский перевод без пересказа и домыслов. Русский исходник сохраняет формулировку. Мелкие подписи/водяные знаки/CTA пропускаются. Плашка между обложкой и автором, четыре строки с заранее выделенным местом; полный текст в Drawer. Никаких новых блоков в SelectedCarousels в этом этапе.

Запуск после 500 мс реальной видимости. До запуска пакетно прочитать кэш. Скрытая вкладка не начинает запросов. Максимум два запроса клиентом и два глобально в БД. Одна обработка ID/version; срок CDN URL не инвалидирует кэш. Никаких автоматических платных повторов, в том числе SDK. Таймаут/неопределённый исход потребляет зарезервированную попытку. Пилот: общий, не суточный предел 20 фактических попыток; до 20 уникальных обложек, не обещание 20 успешных переводов.

## Контракт v1

База /api/radar/carousel-cover-translations.
GET ?ids=<через запятую> (до 100 ID): {items: [{id, status, source_text, text_ru}], enabled: boolean}. Чтение без запуска модели. Неизвестный ID возвращает missing.
POST /{media_id}, JSON {period: '7d'|'14d'|'30d'|'archive14d'}: {item: {id, status, source_text, text_ru}}.
Статусы: missing, running, ready, no_text, unreadable, image_unavailable, error, busy, limit_reached, disabled. busy означает занятость общих слотов без платного вызова; допускает отложенный повтор запуска. running — только опрос чтением. Терминальные ошибки автоматически не запускаются повторно. Ошибки инфраструктуры HTTP 503, валидации 422; сообщение без секретов.
Фронтенд использует source_text/text_ru только как plain text. Только POST запускает обработку. Сервер разрешает ID по существующим архивам/живой ленте, произвольные URL от клиента запрещены.

## Backend и безопасность

Отдельный сервис, Vertex AI ONLY: vertexai=True, project/location/model через CAROUSEL_COVER_VERTEX_PROJECT, CAROUSEL_COVER_VERTEX_LOCATION (global), CAROUSEL_COVER_VERTEX_MODEL. ADC; никаких API-key fallback. CAROUSEL_COVER_TRANSLATION_ENABLED по умолчанию false; отдельное включение после проверки кредита. Обычные функции Gemini не менять.
Хранилище Supabase, отдельный service-role клиент с проверкой наличия; никогда не заменять им общий anon-клиент. Таблицы/RPC закрыты для anon/authenticated, RLS. SQL атомарно резервирует слот и бюджет; фиксация результата защищена идентификатором захвата. Общий предел 2 и 20 установлен БД. Лимит не обнуляется при смене версии. Зависшие попытки не освобождать для нового платного запуска вслепую.
Сначала скачать разрешённое изображение, проверить HTTPS/хост на каждом redirect, MIME, ограничение размера, таймаут. Не использовать платный сбор и не обновлять Meta-данные. После загрузки атомарно зарезервировать модельный вызов. Распознавать полное изображение, не обрезать нижний заголовок. Структурированный ответ оригинал/перевод/статус; исходное изображение — недоверенные данные. Сохранять hash, model, prompt_version, usage, длительность. Не логировать signed URL/credential/provider raw error.
Синхронный ожидаемый запрос, без FastAPI BackgroundTasks. Таймауты конечные; транспортные повторы SDK отключены.

## Владение и исполнители

- Backend GPT-6.1 Sol: backend/app/services/carousel_cover_*.py, backend/app/api/carousel_cover_translations.py, backend/app/main.py, backend/tests/test_carousel_cover*.py, собственная миграция через supabase migration new. requirements только при необходимости.
- Frontend GPT-6.1 Sol: frontend/src/api/carouselCoverTranslations.js, frontend/src/hooks/useCarouselCoverTranslations.js, frontend/src/components/CarouselCoverTranslation.jsx, frontend/src/pages/Carousels.jsx, frontend/src/pages/carousels.css; собственные unit tests рядом с модулями.
- Проверки GPT-5.6 Sol: backend/tests/test_cover_translation_acceptance.py, frontend/src/api/carouselCoverAcceptance.test.js и отчёт docs/specs/32-carousel-cover-review.md. Иные файлы не править.
- Корень: ТЗ/состояние, безопасная диагностика Vertex/billing/БД, baseline и визуальная приёмка, интеграция. Ключевая проверка архитектуры/расхода на Astra. Контракт менять только через корень.

## Приёмка

Тесты: идемпотентность/конкуренция, предел 2 и запрет 21-го вызова, таймаут без повтора, русский/смешанный/отсутствующий/нечитаемый текст, неисправный URL/MIME/redirect, чужие ID/URL, недоступная БД, права anon/authenticated. Кэш между периодами/устройствами. UI 500 мс, hidden tab, сохранность фильтров/отбора/Drawer, большой текст/мобильный вид. Build/lint + затронутые прежние тесты. SQL проверять реальным совместимым локальным Postgres/PGlite при наличии.
Baseline до UI-правки, финальный скриншот. Pilot только после подтверждения проекта, авторизации, действующего кредита и общего счётчика. Реальные обращения делает только корень, агенты используют mocks. Сначала сохранять отчёт и статус на диск. Проверка всех ожидающих миграций перед разрешённым db push. Никакого production push/merge автоматически.
