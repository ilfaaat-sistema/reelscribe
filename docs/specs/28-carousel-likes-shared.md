# Отбор каруселей на телефоне и ноутбуке

20.10.2026. Итоговое уточнение пользователя: кнопка ♥ на конкретной карусели и единый список отметок с телефона и ноутбука. Почтовый вход не нужен. Отметки хранятся в Supabase как общий список существующего приложения; любой посетитель с доступом к приложению имеет тот же доступ к этому списку. Отметка независима от звезды автора и статистики лайков Instagram.

## Контракт реализации

- GET `/api/radar/carousel-likes` → `{items:[{id,selected_at,snapshot,source_period}],total}`. Все активные отметки, независимо от отчёта/периода. `Cache-Control: no-store`.
- PUT `/api/radar/carousel-likes/{id}` body `{period:'7d'|'14d'}` → `{item}`. Идемпотентно отметить; сервер получает снимок из сохранённого отчёта, не принимает произвольный текст от клиента.
- DELETE `/api/radar/carousel-likes/{id}` → `{id,selected:false}`. Идемпотентно снять, сохранить неактивный снимок для восстановления.
- `public.carousel_likes`: `instagram_media_id text PK`, `selected_at timestamptz`, `active boolean`, `snapshot jsonb`, `source_period text`. ID строка 1–40 цифр. Поля снимка — whitelist существующего carousels API. Не использовать UUID reels или авторов как идентификатор.
- RPC `set_carousel_like(p_id text,p_selected boolean,p_snapshot jsonb default null,p_period text default null)` возвращает элемент при true, `{id,selected:false}` при false. Прямые записи таблицы закрыты; чтение и узкая RPC разрешены текущей anon-роли существующего приложения. Это общий, не приватный список. Ограничения типов/размера и URL в SQL и API.
- На карточке и в просмотре ♥; раздел «Отобранные карусели (N)» с сеткой, поиском автора/текста, сортировками и сохранённым caption/embed. Повторное нажатие снимает. Подтверждение только после успешной записи. При ошибке прежнее состояние сохраняется. Опрос 30 секунд в активной вкладке и при фокусе; устаревшие чтения не отменяют успешную запись.
- Новые auth-модули не входят в итоговую ветку. Старый draft PR #21 закрыть после готовности замены. Прежняя пустая личная таблица удаляется отдельной проверенной миграцией без CASCADE и только при отсутствии записей. Историю уже применённой миграции сохранить.

## Порядок и владение

1. Корень: контракт, новая миграция, `Carousels.jsx`, `App.jsx`, интеграция, визуальная приёмка и выпуск.
2. Backend (gpt-6.1-sol): только новые `backend/app/api/carousel_likes.py`, `backend/app/services/carousel_likes.py`, `backend/tests/test_carousel_likes.py`. Не менять main.py/SQL.
3. Frontend (gpt-6.1-sol): `frontend/src/api/selectedCarousels.js`, его тесты, новый `hooks/useSelectedCarousels.js`, `pages/SelectedCarousels.jsx`, `selected-carousels.css`. Hook `{items,loading,error,busy,refresh,setSelected(item,period,selected)}`. Не менять Carousels.jsx/App.jsx.
4. Корень ждёт исполнителей, интегрирует, проверяет сборку/lint/тесты, реальную запись/снятие с двух клиентов, старые звёзды/скрытие/7d/14d, desktop/mobile. Миграции только db push после сверки всей истории. Затем PR, merge, production и проверка.
