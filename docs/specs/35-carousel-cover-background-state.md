# Состояние реализации облачной очереди обложек

23.10.2026. Рабочая ветка `feature/carousel-cover-background`; база фактически `fb6665e`.

UI отдельным commit `a2f3e64`: только готовый текст уменьшен до8.6667px/12.6667px, плашка101px, шесть строк. Portal отображает полный текст при hover/focus карточки, сохраняет переносы и прокрутку, закрывается поEscape/scroll/resize/смене карточки. Touch сохраняет Drawer.

Worker — отдельные файлы `carousel_cover_background.py` и `carousel_cover_background_store.py`. Фиксированный Production API; пакетные GET и два одновременных POST, без транспортных повторов. Перед пачкой≤10 сохранение inflight через CAS. После неизвестного исхода новые POST текущего запуска прекращаются; незавершённые намерения после crash восстанавливаются только GET и не повторяются автоматически. Busy откладывается минимум на15минут. Серверный лимит/слоты остаются источником истины.

Данные: ветка `data/carousel-cover-background`, только state.json/marker/vercel.json, создание orphan без кода, non-force CAS. Runtime выключен без явно установленной переменной. Пользователь выбрал continuous; existing/archive также реализованы. Пользователь отдельно разрешил2500 попыток только23.10.2026 МСК с автоматическим возвратом500; соответствующий SQL и frontend budget validation реализует соседний модуль. Worker не кодирует дневной лимит и оставляет ограничение500 POST на один запуск.

Проверено локально без сети/модели:

- Новые worker/store fakeHTTP и CAS tests:28passed; checkpoint,crash,unknown,auth,three_errors,cache,deadline,два одновременно,period fallback,scope,pagination,marker,size.
- Cover+caption backend регрессии, включая существующие SQL/PGlite и серверные два слота:184passed до добавления последних7 сценариев (повторять только изменённый участок).
- Frontend cover/visibility/caption tests:33passed.
- Frontend build прошёл; lint без ошибок,8 существовавших unused warnings в других файлах.

Визуальная desktop/mobile приёмка с заблокированными платными POST выполняется корнем через отдельный preview43157. Финальные screenshots и замеры корень сохраняет в исключённой `.baseline/cover-background-20261023/`. Публикация, БД, runtime variables, dispatch и фактический cloud run принадлежат корню и этим модулем не выполнялись.

Корневая приёмка кода завершена: UI desktop/mobile PASS (font8.6667, line12.6667, высота101, шесть строк, portal/full-text, переход указателя внутрь, Escape/keyboard/Drawer). Гонка pointerleave исправлена b5cfda2. Общий deadline перебора периодов исправлен b7f9894; независимое ревью worker/store29tests PASS. Budget210289c:75backend и22frontend, независимая SQL/validator-проверка PASS. Лимит2500 действует только23.10.2026МСК, после00:00автоматически500. Новых открытых code blocker нет. Реальный rollout/Actions проверяется отдельно; тесты не выдаются за уже запущенную облачную обработку.
