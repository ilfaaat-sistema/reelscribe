# Карусели за неделю — утверждённая реализация

19.10.2026 пользователь утвердил план: все1600 авторов, период13–19октября2026 Europe/Moscow (19-е неполное), отдельный облачный сбор безMac/платныхAPI; сохранение прежнего трёхдневного результата. Счётчик автора открывает отдельную сетку, общий просмотр, сортировки дата/лайки/комментарии, 6колонок широкогоэкрана; просмотр через уже используемый Instagram embed. Русский caption/transcript поумолчанию; при отсутствии переводакнопка, переводтольконажатием через существующий сервис, поля БД прежние. Собственный слайдер/OCR/генерация/расширенные периоды вневерсии.

## Контракт

Worktree worktrees/carousels-week, feature/carousels-week, база226d45c. Чужие правки не переносить/переписывать. Новыйdataset branch data/authors-carousels-20261013-20261019 с собственным marker, прежниеdata/authors-activity не меняются. Формат пятифайлов store сохраняется; activity-report.json содержит start_date,end_date,timezone,collected_at,provider=meta_graph,paid_runs=0,collection(status,target,checked,available,unavailable,pending,requests,cooldown_until),totals(carousels),profiles[{username,carousels}],items[]. Item: id(IG),username,permalink,shortcode,timestamp,caption,media_type=CAROUSEL_ALBUM,media_url,thumbnail_url,likes(nullable),comments(nullable),views(nullable),followers(nullable),collected_at. Только whitelist publicполя, никакихтокенов/заметок. mediaURLs HTTPSInstagramCDN, отсутствие допустимо; fallbackthumbпоshortcode. Дедуп(username,id), сводкаuniqueid. Непроверенныепрофили не0.

GET /api/radar/carousels?author=&sort=likes|comments|timestamp&order=desc|asc&limit=50|100&offset=0 -> {items,total,start_date,end_date,timezone,collected_at,collection,totals,profiles}. Стабильнаясортировка,nullвконце, tiesid. GET /api/radar/carousels/{id} -> item. datasetвыбираетсятолькосервером; никакихclientURLs. Доотчёта503срусскимсообщением. Сетка /radar/carousels?author=&sort=&order=&limit=&offset=, detailпоdrawer, safeReturnToвlocation.state+scroll. Не обращаться getReel(IGid).

POST /api/reels/{id}/translate body {field:caption|transcript} -> соответствующиеполяcaption_ru/caption_lang илиtranscript_text_ru. UUIDвалидация, нетпроизвольноготекста, толькохранимыйисточник, ужеимеющийсявалидныйпереводвозвращаетсябезвызова; лимиты/квотысуществующегопереводчикасохраняются, массовыйпереводневызывается.

## Владение и порядок

- Root: контракт, workflow/store/daemon, read-onlycarouselsAPI, маршрутыApp/main, интеграцияклика существующегоAuthorsвотдельномстаромworktree, приёмка/публикация/живойпилот/1600.
- Solcollector: ТОЛЬКО backend/app/radar/carousel_job.py и backend/tests/test_carousel_job.py; run_batch(checkpoint_dir,output_report,token,account_id,api_version,client,clock,max_seconds,max_requests) keywordсовместимсactivity_job; самостоятельныйрезюмируемыйbatch, reuseнеизменяемыхhelpersactivity_job; fieldsзапрашивать минимальноid/type/dateсначала, детальтолькокаруселям. Хранитьdetailsphasecheckpoint, countидеттолькопослеуникальнойидентификациивокне. Тестыдат/паузы/рестарт/пагинации/ошибок/fields.
- Solgrid: ТОЛЬКО frontend/src/pages/Carousels.jsx, frontend/src/pages/carousels.css, frontend/src/api/carousels.js, frontend/src/api/carousels.test.js. Полностью готоваястраница APIконтрактвыше, своиgrid/drawerstylesсиспользованиемtokens. Маршрутыправитroot.
- SolRussian: ТОЛЬКО frontend/src/components/ReelDrawer.jsx, frontend/src/api/translation.js, backend/app/api/translation.py, backend/tests/test_translation_api.py. routerс /reels/{id}/translate, rootподключаетmain. Сначала Supabase skill+read-onlyschema/sourcecode; миграцийнет. Не менятьclient.js/App/Results/main. Сохранитьiframe/заметки/закрытие/повторноечтение.
- Astraпослеисполнителей: критическаяприёмка наузкомdiffбезсекретов; rootждётявногозавершенияперединтеграцией.

## Приёмка

Пилотнесколькихреальныхкаруселейдополногосбора. Подсчёт=карточкамсучётомpartial; московскиеграницы,идемпотентность,нетнулейвместонеизвестного,старыеdataset/jobнеизменны,автопродолжение. Desktop/narrowскриншоты,backrestores,русскийс/безперевода,старыеReels/notes/iframe. Поддержать7карточексуществующейглавнойсетки(новаяпараллельнаяправкапользователя); новаясетка6. Build/lint/тестыпослеинтеграции. Опубликоватьстраницунезависимооткаталогамиграции; клиентлокальногокаталогапатчитroot. Платныйfallbackнеподключать.
