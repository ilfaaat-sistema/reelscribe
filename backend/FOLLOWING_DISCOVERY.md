# Поиск авторов по подпискам

`GET /api/radar/discovery/{username}` читает UTF-8 снимок `<username>.json` из каталога `FOLLOWING_DISCOVERY_DIR`. `POST /api/radar/discovery` с телом `{"input":"@username"}` также отдаёт готовый снимок. Ник и ссылки профилей приводятся к канонической форме. Храните каталог со снимками за пределами репозитория.

## Новый сбор на локальной машине

- Укажите `FOLLOWING_DISCOVERY_DIR`, `FOLLOWING_DISCOVERY_LOCAL_START=1` и `FOLLOWING_DISCOVERY_MAX_CHARGE_USD`. Последнее значение задаёт серверный верхний предел в единицах параметра Apify `maxTotalChargeUsd`. `FOLLOWING_DISCOVERY_MAX_PROFILES` задаёт предел строк на запуск, по умолчанию 1500, допустимо 1–3000.
- Привяжите backend и Vite к loopback и запускайте локальный backend с `--no-proxy-headers`. Откройте интерфейс на `http://localhost:5177` или `http://127.0.0.1:5177`. Для другого порта задайте точный адрес через `FOLLOWING_DISCOVERY_LOCAL_ORIGIN`. Маршрут принимает новый запуск с loopback-соединения к loopback-сокету, с совпадающими `Host` и браузерным `Origin` без forwarded-заголовков.
- Список запрашивается только у Coregent в базовом режиме. Перед стартом сервер сверяет предел запуска с актуальным использованием и доступным бесплатным кредитом Apify. Новые сборы используют основной настроенный `APIFY_API_TOKEN`. Подтверждённый результат можно опрашивать через GET на том же локальном адресе. [Формат и покрытие списка](https://apify.com/coregent/instagram-followers-following-scraper), [предел запуска](https://docs.apify.com/api/v2/actors-runs-post).
- Сервер сохраняет идентификатор запущенного сбора в `.discovery-active.json`. При неоднозначном ответе на запуск следующая попытка блокируется для защиты от повторного списания. Возобновление новых запусков требует проверки предыдущего запуска в Apify и разрешения оператора на очистку служебного файла.

## Дозаполнение карточек через Meta

API использует `META_IG_TOKEN`, `META_IG_USER_ID` и `META_IG_API_VERSION`; версия по умолчанию совпадает с локальными работниками Радара. Карточки Business и Creator запрашиваются через официальный Business Discovery, в том числе когда список подписок уже собран. Отсутствие доступной карточки не удаляет строку списка. `profile_status` хранит доступность, `profile_source` и `profile_checked_at` указывают происхождение метаданных. [Meta Business Discovery](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/business-discovery).

Для пустых карточек импортированного снимка или продолжения после технического сбоя запустите из `backend`:

```bash
python -m app.workers.following_discovery_meta alex_magnier
```

Команда сохраняет прогресс после каждой порции запросов и сообщает срок продолжения при лимите Meta. `--batches` задаёт количество порций за запуск. Счётчик донора, список подписок и дата списка сохраняются. Команда обращается только к Meta.

При временной ошибке DNS на macOS клиент получает публичный IPv4-адрес `graph.facebook.com` через штатный `/usr/bin/dig` и текущий DNS. Соединение по IP сохраняет `Host`, SNI и проверку сертификата для `graph.facebook.com`. При потере IP-адреса есть одна попытка обновить DNS. Токен передаётся в HTTPS-заголовке, а параметры DNS системы остаются прежними. [HTTPX и проверка имени сервера](https://www.python-httpx.org/advanced/extensions/#sni_hostname).

При лимите Meta учитываются `Retry-After` и `X-Business-Use-Case-Usage` для Instagram. Поле `estimated_time_to_regain_access` задано в минутах. Временные сетевые и серверные ошибки оставляют карточки в очереди на продолжение. [Лимиты Meta Graph API](https://developers.facebook.com/docs/graph-api/overview/rate-limiting/).

## Публикация и автоматическое обновление

На Vercel, если локального файла нет, `GET` и `POST` читают закрытый bucket `reelscribe-discovery` в настроенном проекте Supabase. Путь объекта `snapshots/<username>.json`. Используются штатные серверные `SUPABASE_URL` и `SUPABASE_SERVICE_ROLE_KEY`. Снимок читается с обходом CDN-кэша, ответ сайта отдаётся с `Cache-Control: no-store`. Запуски нового сбора по публичному API выключены. [Режим доступа Supabase Storage](https://supabase.com/docs/guides/storage/security/access-control) · [Обход кэша Supabase](https://supabase.com/docs/guides/storage/cdn/smart-cdn#bypassing-cache).

В `backend` можно один раз сверить и опубликовать снимок штатным Supabase CLI связанного проекта:

```bash
python -m app.workers.following_discovery_sync '/абсолютный/путь/alex_magnier.json' \
  --workdir '/абсолютный/путь/Парсер'
```

Для постоянной синхронизации передайте `--watch --poll-seconds 5`. Команду можно запустить службой пользователя macOS с абсолютным путём к Python и `WorkingDirectory` равным `backend`. Если служба не находит CLI в `PATH`, передайте его абсолютный путь через `--supabase-cli`. `SUPABASE_URL` задаётся штатной конфигурацией backend или окружением службы, CLI использует уже существующую авторизацию и проверяет привязку проекта.

Служба читает облако только при старте, изменении файла или повторе после сбоя. Перед загрузкой она валидирует снимок, сверяет SHA-256 и свежесть. После загрузки сверяет байты в закрытом bucket. История отпечатков и блокировка для одного процесса на донор хранятся в `supabase/.temp` связанного проекта. В bucket отправляется только результат в формате снимка.
