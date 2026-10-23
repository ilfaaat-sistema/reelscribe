# Массовый и автоматический перевод подписей каруселей

23.10.2026. Пользователь утвердил: все существующие и новые, до500 новых переводов в сутки. Исходная функция31 опубликована на deeba0b, API и Vertex/WIF уже работают.

## Результат и ограничения

Обработать все сохранённые архивы7d/archive14d и живые14d/30d, затем подхватывать новые данные. Русский уже показывается по умолчанию для готового кэша. Изображения/обложки/Meta-сбор и старый reels-перевод не меняются. Инвентаризация:2038 ID+hash,420 по прежнему foreign-правилу,2готовых; убрать1толькохэштеги и добавить7коротких lexical => финальная классификация423 новых. Финальный classify может уточнить счётчик, все категории учитывать явно.

Бюджет:500 стартов Vertex в календарные сутки Europe/Moscow,2 действующие аренды глобально. Фоновый worker выполняет1 POST одновременно, оставляет второй слот ручному переводу. Ошибки/неопределённый исход не возвращают суточную попытку. Новые вызовы только по существующему защищённому API, без передачи новых секретов Actions. Модель gemini-3.1-flash-lite/global, ограничения модели/хэша/fencing сохраняются.

## Порядок и владение

После фиксации контракта отдельные branch/worktree feature/carousel-caption-bulk / worktrees/carousel-caption-bulk. GPT6.1 владеет backend classifier/budget/attempt status/миграцией/своими тестами. GPT5.6 параллельно владеет worker, GitHub store, workflow и своими тестами. Корень — координация, конфиг, миграция/выкат/dispatch, finalstate; Astra — только ключевой review и финальная приёмка. Максимум2 исполнителя+корень; общие файлы между исполнителями не пересекать.

## Контракт backend

Новый чистый модуль app/services/carousel_caption_rules.py экспортирует needs_caption_translation(text), source_hash(text), classify_caption(text). Никаких SDK/config/secrets imports. Из текста для языкового решения удалить URL/@mentions/#hashtags; emoji/digits/punctuation без букв и одиночные handle-like токены с._ не требуют перевода. Для обычного body сохранить порог кириллицы30%, минимум1буква вместо12; короткие foreign тоже переводить. classify:empty/not_needed/too_long/eligible, max12000. Хэш всегда точные UTF8 исходника, без нормализации. Подключить в caption service и caption-only quality; старый app.pipeline.translate не менять.

GET деталей сохраняет прежние поля и state. Для uncached eligible добавить caption_translation.attempt: {status:none|running|expired_unknown|failed|quota|source_changed|unavailable, started_at:ISO|null, lease_until:ISO|null}. Приватный attemptUUID/token не выдавать. ready до config/auth. Это read-only статус последней попытки ID+hash, никаких STS/model вGET. Ошибка чтения статуса не должна превращаться вnone:status unavailable, worker при нём не POST. Для готового кэша attempt можно не включать.

Миграция штатным migration new заменяет только reserve_carousel_caption_translation:500 стартов с00:00МСК, RetryAfter до следующей полуночи;2leases и singleflight/fencing неизменны. Историю attempts больше не удалять: она нужна для запрета автоматических повторов. Добавить индекс ID/hash/started_at. Service retry_after принимает до86400. Новых публичных прав/таблиц не добавлять. POST получает необязательный strict bool automatic=false; старый ручной body совместим. Worker всегда передаёт automatic=true.

## Облачная очередь

Новые app/workers/carousel_caption_bulk.py и app/services/carousel_caption_bulk_store.py. Сеть только уже сохранённые данные своего GitHub repo и фиксированный HTTPS Production API; никаких Meta/providerSDK/новых keys. Чистый classifier импортировать без тяжёлых deps. Использовать pinned httpx и стандартный ephemeral GITHUB_TOKEN.

Sources: activity-report.json архивных data/authors-carousels-20261013-20261019 и data/authors-carousels-20261006-20261019; state.json data/carousels-favorites-live, live30дней поМСК (14подмножество). Читать3готовыхJSON поContents/blob сmax10MiB, без redirects/секретов вURL. Одинаковый ID+exacthash дедуп; сохранить подходящие periods, предпочтение30d,14d,archive14d,7d. При смене SHA перечитывать; не принимать наличие имени за валидность содержимого.

Постоянное state в новой data/carousel-caption-bulk: только state.json,vercel.json с git.deploymentEnabled=false и marker .carousel-caption-bulk-data. Load проверяет marker/запретдеплоя/schema; CAS git ref, forcefalse. Ошибка/конфликт сохранения останавливает новые POST. В state нет caption/translation/token, только ID/hash/periods/status/timestamps/коды/счётчики/refs. MAX10MiB. Существующие ветки не менять.

Перед пачкой до10 записей сохранить inflight checkpoint (до отправки POST); после обработки сохранить исходы. Всегда сначалаGET свежих деталей: ready->готово без POST; not_needed->готово без модели; wronghash/source_changed->пересобрать запись, не повторять POST автоматически;404->gone. POST только matchinghash+available+attempt.none. Known running=>ждать, expired_unknown/failed/quota=>review/error, не повторять модель автоматически. Неопределённый HTTP/transport POST=>unknown и остановить новые вызовы; следующая сессия может только сверить cache/attempt. После crash unsentinflight можно вернуть в pending только если прошло>=360сек(вышеmaxVercel300), statusnone и age<24h; иначе unknown/review. Ready всегда можно восстановить поGET.

Server429 translation_rate_limited без модели => pause_until поRetryAfter и продолжить после срока; providerquota/auth/503/неразобранный502 => сохранитьpause_reason и остановить. При app502 translation_failed сделатьGET-reconcile: ready=>готово; явно failed=>review этой записи без новогоPOST и продолжить;3 последовательных таких ошибок=>pause всего job. Running/expired/unavailable=>unknown+pause. Никаких циклических платных retry. Для unknown не освобождать lease вручную. API status unavailable=>неPOST. GET может повторяться ограниченно; POST transport retries0. Не очищать реальные счётчики/переводы.

Workflow carousel-caption-translations.yml: dispatch, cronкаждые15минут и workflow_run успешного main-workflow «Живые избранные карусели» только same-repo/main; код checkoutmain, permissionscontentswrite,concurrencycarousel-caption-bulk cancel-in-progressfalse,timeout30min. Worker max20min/run,1POST за раз, max500/run; сохранять progress и продолжатьследующимзапуском. Только start/paused/completed counts в логах, не data/secrets. Включение/выключение repository variable CAROUSEL_CAPTION_BULK_ENABLED=true/false; disabled не делаетPOST. Dispatch initial после выпуска; cron не считать доказанным до фактического run.

## Проверки и сдача

Classifier на snapshots/коротких/handles/body+hashtags, старые188 backend/44frontendрегрессии по затронутому объёму. SQL500/day/полночьМСК/errorsnoRefund/2slots/fencing/RetryAfter; не вызывать500 реальных моделей. Worker fakeHTTP ready/missing/new/sourcechanged/429/auth/unknown/crash/CAS/rateGithub/duplicateperiods, запрет modelretries/новыхсекретов. Проверить настоящую запущенную Actionsjob и сохранённый checkpoint; контролируемый первый batch под темжеbudget, затем продолжить до очереди0 или явно зафиксированных ошибок. Не объявлять все переведёнными до pending=0 и сверки всех классифицированных записей. Сохранить manifest/state/runURL, фактический расход поusage если доступен; не выдавать оценку за invoice.

Перед remote dbpush сверить target и все pending через isolatedhistory, применить только своюмиграцию безrepair/apply. Затем выкат кода/работа queue и фактическая приёмка; сохранять чужие docs/ветки. Новые IAM/Googleключи не нужны. Визуал не требуется: UI не меняется, режим Russian ready уже проверен31.

## Уточнение key-review перед реализацией

Astra approve: отдельная закрытая RPC reserve_carousel_caption_translation_auto(p_id,p_source_hash,p_attempt_id),толькоservice_role. Подтемжеглобальнымadvisorytxlock:готовыйкэшвозвращаетсябезрасхода;anyпредыдущаяattemptID/hash=>activein_progressилиprevious_attempt;толькоприnoneвызватьсовместимуюmanual3argRPCподтемжереентрантнымlock. Всеисторииattemptsсохраняются,GETnoneпередPOSTнеявляетсягарантией,гарантиянасервере.

API automatic=true выбираетautoRPC;previous_attempt=>409 translation_review_required иworkerreviewбезновоймодели. Ручнойповторautomatic=falseсохраняетсяпод500/day. Неизменяемыйinflight_started_atвcheckpointнеобновлятьприreconcile;ageсчитаетсяотпервойзаписи. Marker/data-branch/CASиостальныеограниченияне меняются.

## Предрелизная приёмка

Backend:211tests+new/legacySQLPGlitepass. WorkerпослеAstraисправлений:256combinedtests,ruff/YAML/diffcheckpass. ИсправленыGitHubmultilineBase64,matchingperiodfallbackвключая404,пересчётоконпоновомуднюМСК,POST401/403pause. FinalkeyreviewAstraapprove;визуалне нужен,UIне менялся. Финальнаяинвентаризация2038=74empty+1539not_needed+2ready+423pending. Запуск/DDL/домен/Actionsфактысохраняютсяотдельнов33-stateпослеисполнения.

## Приёмка реального прохода и уточнение v2

Первый рабочийcloudrun39934250083успешен(336ready/84pending/5review),второй39936750271успешен(420ready/5review/0pending). StartupfalseEAAвSHAисправленfb6665e:валидациявсех2038реальныхзаписейдоPOST,секретыдальшезапрещены.

V2чиститслужебныеbarehandles/camera-creditизязыковогоанализа,venueтолькотиповойsuffixвRussianконтекстесbanadwords;source/hashсохраняет. DistinctnonRussianCyrillicвsemanticbodyраспознаётKK;quotedtitleвRussianконтекстенеискажаетязык. Невнятныеименаостаютсяreview,неfalseNoNeed. classification_versionсовместимсстарымstate,одинразпересчитываетsnapshotбезсменыrefs. review/error/source_changedвосстанавливаютсяТОЛЬКОGETдоready/not_needed,платныхавторетраевнет.

ПосколькуcronGitHubможетзадерживаться,workflowсампродолжаетstatusstarted+pendingпослереальногопрогресса;zero-progressdeadline=>paused15min/outputfalse. permissionscontentswrite+actionswriteтолькособственномурепозиториюдляfixedselfdispatch/main,GITHUB_TOKENбезновыхсекретов. Workflowinputmax_postsnumberdefault500/CLIstrict1..500,0сохраняетсячерезtoJSONиотклоняется. Самопродолжениебезinputsвозвращается500. Этонеизменяетserver500/day/одинworkerPOST. Контролируемыйrun5проверяетживуюцепочку,втестахpaused/unknown/review-onlyнетdispatch.

ПредрелизныйAstrareviewзакрыл3края:BuyNow/Beautifulнепропускаются,maxposts0невключает500,zero-progressнезацикливается.362combinedtestspass,дополнительно2workflowstatic. Runtime/API/SDK/SQLлимитыизпредыдущеговыпускасохранены. Редакторскоеисправлениеодногословабапкері→ТренерготовитсяRootчереззащищённыйcacheпривязанныйID+exactsourceHash,послеv2gate,безмодели/перезаписиистории.
