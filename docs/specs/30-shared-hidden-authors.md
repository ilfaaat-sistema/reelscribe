# Общие скрытые авторы

Запрос пользователя 20.10.2026: скрытие автора и возврат в сетку должны сохраняться на сервере и работать на телефоне/ноутбуке, как общий отбор каруселей. Без аккаунтов. Скрытие не удаляет избранного автора и не снимает лайки каруселей.

## Контракт и исполнители

Корневая Astra: контракт, интеграция, миграция, публикация и итоговая приёмка. Backend Sol: только app/api/hidden_authors.py, app/services/hidden_authors.py, подключение main.py, новый SQL и backend/tests/test_hidden_authors.py. Frontend Sol: только api/hiddenAuthors.js и его тесты, новый hooks/useHiddenAuthors.js, pages/Carousels.jsx. Остальные файлы корня/чужие правки сохранять.

GET /api/radar/hidden-authors → {authors:[{username,hidden_at}],total}. PUT /{username} → {item:{username,hidden_at}}. DELETE /{username} → {username,hidden:false}. POST /import body {authors:[{username,hidden_at}]} → полный список как GET. Строгие тела, username lowercase 1–30 [a-z0-9._], даты ISO, до1600 импортируемых записей. Cache-Control no-store. Ошибка БД 503 без внутренних деталей.

Supabase public.carousel_hidden_authors: username PK, hidden_at timestamptz, active bool. RLS и SELECT anon/authenticated; прямые записи закрыты. Узкие RPC set_carousel_author_hidden(p_username,p_hidden) и import_carousel_hidden_authors(p_authors jsonb). Общий advisory lock, защитный лимит1600 активных. Повторный PUT сохраняет дату; DELETE создаёт/оставляет inactive tombstone даже при отсутствии записи. Импорт добавляет только отсутствующие username, никогда не меняет существующие строки: возвращённые авторы не воскресают из старого браузера. Даты импорта сохраняются. Транзакционная валидация всего пакета.

Клиент: сохраняет legacy read/write helpers для резервной копии и тестов. Новый hook useHiddenAuthors() → {authors,loading,error,busy,refresh,setHidden(username,hidden)}. При старте читается локальный старый список и импортируется сервером до загрузки общего списка. Локальная копия не удаляется; повторный импорт безопасен благодаря tombstone. Сервер подтверждает каждую операцию перед изменением UI. Защита от устаревших GET и параллельных записей; обновление по focus/visibility и каждые30с. Нельзя показывать успех при сетевой ошибке. Ошибка чтения старого localStorage не должна лишать доступа к серверному списку; показать отдельное предупреждение о неперенесённой копии.

## Приёмка

Проверить hide/read из независимого клиента/restore, идемпотентность, импорт дат/дубликатов/некорректных данных и отсутствие повторного скрытия после возврата. Сохраняется фильтрация до пагинации, возврат/отмена/scroll, лайки и звёзды. Build/lint и затронутые тесты; БД только штатным db push после сверки ВСЕЙ истории; финальный домен и UI. Ветка feature/hidden-authors-shared, постоянный worktree worktrees/hidden-authors-shared.
