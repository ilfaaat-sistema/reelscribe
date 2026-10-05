# ТЗ 10. Разведка Радара: источники, лента залётных, обновление кнопкой

**Статус:** в работе

## Проблема / цель

Радар умеет только разовый сбор по введённым никам: список аккаунтов между сеансами не виден,
подписчики у всех «н/д», дата рилса не показана, понять «что залетело» и «что взлетает сейчас»
нельзя. Нужна постоянная разведка по образцу YouTube Радара: отслеживаемые источники отдельной
вкладкой и лента их рилсов с залётностью относительно нормы самого аккаунта.

## Пользовательский сценарий

1. Открывает `/radar` → вкладка «Лента». Вкладки: «Лента», «Источники», «Разовый сбор».
2. В «Источниках» вставляет `@ник` или ссылку на профиль → «Добавить». Аккаунт появляется с
   пометкой «ещё не обновлялся».
3. Полоса «Обновить» показывает цену в рублях. Нажимает → прогресс «рилсы 2/4 пачки»,
   «подписчики» → итог: сколько рилсов сохранено, сколько стоило, какие аккаунты не обновились.
4. В «Ленте» — рилсы всех источников, по умолчанию сверху самые залётные. Фильтрует период,
   аккаунт, «только залётные», сортирует кликом по заголовку.
5. «Разобрать» у рилса открывает «Разовый сбор» с этим рилсом, выбранным для разбора.
6. «Убрать» в «Источниках» переносит аккаунт в «Исключённые», оттуда — «Вернуть».

## Функциональные требования

### Расчёт (`backend/app/radar/feed.py`, чистые функции без обращения к БД)

Константы — в `backend/app/radar/intel_settings.py`:
`VIRAL_RATIO_THRESHOLD=3.0`, `VIRAL_LEVELS=(3.0, 5.0, 10.0)`, `MATURE_DAYS=3`,
`NORM_WINDOW_DAYS=90`, `NORM_MAX_REELS=60`, `NORM_MIN_N=5`, `NORM_RELIABLE_N=10`,
`RISING_MIN_GAP_HOURS=20`, `RISING_RATIO=1.2`, `RISING_MAX_AGE_DAYS=14`,
`RISING_NOHISTORY_MAX_AGE_DAYS=7`, `RISING_PERCENTILE=0.7`, `MAX_SOURCES=60`.

- **Норма аккаунта** `norm_views` — медиана `views` по рилсам аккаунта с возрастом
  ≥ `MATURE_DAYS` и ≤ `NORM_WINDOW_DAYS`, не более `NORM_MAX_REELS` самых свежих из них.
  `norm_n` — сколько рилсов вошло. Сам рилс из медианы не исключается. Рилсы без `views` или
  без `posted_at` в норму не входят.
- **Залётность** `viral_ratio = views / norm_views` (округление до 2 знаков).
  `norm_n < NORM_MIN_N` или `norm_views` = 0 → `viral_ratio = null`.
  `norm_reliable = norm_n >= NORM_RELIABLE_N`.
  `is_viral = norm_reliable and viral_ratio >= VIRAL_RATIO_THRESHOLD`.
- **Скорость** `speed = views / max(1, age_days)`; **охват** `reach = views / followers`
  (`null`, если подписчиков нет или 0). `age_days` — дробное, от `posted_at` до «сейчас».
- **«Набирает»** `rising` / `rising_mode`:
  - есть пара снимков (последний `v2` и ближайший более ранний `v1` с разрывом
    ≥ `RISING_MIN_GAP_HOURS`) и `v2 >= v1`: `recent = (v2 − v1) / дней_между`;
    `rising = recent >= RISING_RATIO × speed and age_days <= RISING_MAX_AGE_DAYS`;
    `rising_mode = "history"` (если rising), иначе `null`;
  - пары нет или дельта отрицательная: `rising = age_days <= RISING_NOHISTORY_MAX_AGE_DAYS
    and speed >= P70`, где P70 — элемент с индексом `int(len × 0.7)` в отсортированных по
    возрастанию скоростях ВСЕХ рилсов активных источников (без учёта фильтров ленты);
    `rising_mode = "age"` (если rising), иначе `null`.
- Отрицательные `likes`/`comments` (скрытые, `-1`) отдаются как `0`.
- Сортировка: значения `null` всегда в конце, независимо от направления.

### Обновление (`backend/app/radar/refresh_service.py`)

Константы — в `backend/app/radar/settings.py`: `DEEP_LIMIT=60`, `DEEP_DAYS=90`,
`INCR_LIMIT=15`, `INCR_DAYS=14`, `DEEP_STALE_DAYS=14`, `BATCH_DEEP=5`, `BATCH_INCR=10`,
`BATCH_FOLLOWERS=25`, `FOLLOWERS_TTL_DAYS=3`, `MIN_REFRESH_INTERVAL_H=6`,
`REFRESHES_PER_HOUR=2`, `REFRESH_STALE_MIN=180`, `START_CLAIM_STALE_SEC=90`, `USD_RUB=80`,
`APIFY_COST_PER_PROFILE=0.0023` (непроверенная оценка, уточняется платным прогоном).

- Источник идёт **глубоким** проходом, если `last_scraped_at` пуст или старше
  `DEEP_STALE_DAYS`; иначе **лёгким**. Источник с `last_scraped_at` моложе
  `MIN_REFRESH_INTERVAL_H` пропускается (если не `force`).
- Пачки: сначала глубокие (по `BATCH_DEEP` ников, `resultsLimit=DEEP_LIMIT`,
  `onlyPostsNewerThan = сегодня − DEEP_DAYS`), потом лёгкие (по `BATCH_INCR`,
  `INCR_LIMIT`, `INCR_DAYS`), потом подписчики (по `BATCH_FOLLOWERS`) — только тем обновляемым
  источникам, у кого `followers_updated_at` пуст или старше `FOLLOWERS_TTL_DAYS`.
- `POST /refresh` создаёт строку `radar_refreshes` и все дочерние `radar_scrape_runs`
  (`status='pending'`, `refresh_id`, `batch_idx`, `params`, `period_months=null`). Cron нет —
  конвейер двигает **каждый** `GET /refresh/current`:
  1. первая нетерминальная пачка по `batch_idx`;
  2. `running` → `scrape_service.poll_scrape_run(run_id)`;
  3. `pending` → захват старта условным update `start_claimed_at=now() WHERE
     start_claimed_at IS NULL AND status='pending'`; только захвативший вызывает Apify и
     `set_run_started`. Захват старше `START_CLAIM_STALE_SEC` без перехода в running → пачка
     в `error` (повторного платного запуска нет);
  4. пачка стала терминальной → в том же вызове стартует следующая;
  5. все терминальны → финал: `reels_saved`, `cost_usd`, `errors`, статус `done` (хотя бы
     одна пачка успешна) или `error`.
- После успешной реел-пачки её никам ставится `last_scraped_at=now()` (и при 0 рилсов).
  Метка ставится сразу по закрытии пачки, не дожидаясь финала обновления.
- Замок: уникальный частичный индекс на `status='running'`. Перед созданием зависшие
  `running` старше `REFRESH_STALE_MIN` закрываются как `error`. Повтор при идущем → 409.
  `GET /refresh/current` зависшие не закрывает — он всегда продолжает идущее обновление.
- Лимит `REFRESHES_PER_HOUR` считается по `radar_refreshes`. Дочерние прогоны (с
  `refresh_id`) НЕ входят в `SCRAPES_PER_HOUR` разового сбора.
- Каждая принятая реел-пачка (и разовый сбор тоже) пишет снимки в `radar_reel_snapshots`.
  Запись снимков не фатальна: ошибка логируется, приём датасета не откатывается.
- Фактическая цена прогона берётся из Apify (`usageTotalUsd`), если отдаётся; иначе оценка.

### Источники (`backend/app/radar/sources.py`)

- Вход — строка с одним или несколькими значениями через запятую/пробел/перенос:
  `@ник`, голый ник, ссылка на профиль `instagram.com/<ник>/`. Ссылки на `/reel/ /p/ /tv/
  /stories/ /explore/` отклоняются. Ник: `^[A-Za-z0-9._]{1,30}$`, в нижний регистр.
- Добавление: upsert `{username, is_source: true, source_added_at: now, excluded_at: null}` —
  `followers` не затирается, Apify не вызывается. Сверх `MAX_SOURCES` → отказ.
- Убрать: `is_source=false, excluded_at=now()`; рилсы остаются. Вернуть: обратно.
- Разовый сбор (`ensure_competitors`) источников НЕ создаёт: `is_source` по умолчанию false.

## Контракт (обязателен для всех исполнителей)

### Миграция `supabase/migrations/0011_radar_intel.sql`

```sql
-- ТЗ 10: Разведка Радара. Идемпотентно, как 0008.
alter table public.radar_competitors
  add column if not exists is_source        boolean     not null default false,
  add column if not exists source_added_at  timestamptz,
  add column if not exists excluded_at      timestamptz,
  add column if not exists last_scraped_at  timestamptz,
  add column if not exists full_name        text;
create index if not exists idx_radar_competitors_source
  on public.radar_competitors (is_source) where is_source;

create table if not exists public.radar_refreshes (
  id            bigint generated by default as identity primary key,
  status        text not null default 'running' check (status in ('running','done','error')),
  total_batches integer not null default 0,
  estimate_usd  numeric(10,4),
  cost_usd      numeric(10,4),
  reels_saved   integer not null default 0,
  errors        jsonb not null default '[]'::jsonb,
  created_at    timestamptz not null default now(),
  finished_at   timestamptz
);
create unique index if not exists uq_radar_refresh_one_running
  on public.radar_refreshes ((true)) where status = 'running';

alter table public.radar_scrape_runs
  add column if not exists refresh_id        bigint references public.radar_refreshes(id) on delete set null,
  add column if not exists batch_idx         integer,
  add column if not exists params            jsonb,
  add column if not exists start_claimed_at  timestamptz;
create index if not exists idx_radar_runs_refresh
  on public.radar_scrape_runs (refresh_id, batch_idx);

create table if not exists public.radar_reel_snapshots (
  id            bigint generated by default as identity primary key,
  reel_id       text not null references public.radar_reels(id) on delete cascade,
  views         bigint,
  likes         integer,
  comments      integer,
  taken_at      timestamptz not null default now(),
  scrape_run_id bigint references public.radar_scrape_runs(id) on delete set null
);
create index if not exists idx_radar_snap_reel
  on public.radar_reel_snapshots (reel_id, taken_at desc);
create unique index if not exists uq_radar_snap_run
  on public.radar_reel_snapshots (reel_id, scrape_run_id) where scrape_run_id is not null;
-- + RLS и политики anon_full_access_* для двух новых таблиц — дословно по образцу 0008.
```

`params` у пачки: `{"results_limit": int, "newer_than": "YYYY-MM-DD", "deep": bool}`
(у пачки подписчиков — `{}`).

Состояния строки `radar_competitors`: активный источник — `is_source`; исключённый —
`not is_source and excluded_at is not null`; прочие — разово проверенные, в разведке не видны.

### API (все под `/api/radar`)

`GET /feed?period=30&usernames=a,b&only_viral=0&sort=viral&order=desc&limit=60&offset=0`
— `period`: `7|30|90|0` (0 = всё, дефолт 30), по `posted_at`; `sort`:
`viral|views|speed|reach|posted_at|followers|likes|comments` (неизвестное → `viral`);
`order`: `asc|desc`; `limit` ≤ 200.

```json
{
  "total": 312, "limit": 60, "offset": 0,
  "viral_threshold": 3.0,
  "last_refresh_at": "2026-10-05T10:00:00+00:00",
  "accounts": [{"username": "x", "count": 41}],
  "items": [{
    "id": "C1abc", "url": "https://…", "username": "x", "caption": "…",
    "posted_at": "2026-10-01T09:00:00+00:00", "age_days": 4.2, "duration_sec": 31.0,
    "views": 120000, "likes": 5400, "comments": 210, "followers": 50000,
    "norm_views": 30000, "norm_n": 24, "norm_reliable": true,
    "viral_ratio": 4.0, "is_viral": true,
    "speed": 28571.4, "reach": 2.4,
    "rising": true, "rising_mode": "history",
    "analysis_status": null, "analysis_mode": null
  }]
}
```

`accounts` — активные источники со счётчиком рилсов в выбранном периоде (до фильтров
`usernames`/`only_viral`), для выпадающего списка. `total` — после всех фильтров.

`GET /sources`

```json
{
  "active": [{
    "username": "x", "full_name": "Имя", "followers": 50000,
    "followers_updated_at": "…", "median_views": 30000, "norm_n": 24, "norm_reliable": true,
    "last_reel_at": "…", "reels_count": 58, "viral_count": 4,
    "last_scraped_at": "…", "added_at": "…", "needs_deep": false
  }],
  "excluded": [{"username": "y", "excluded_at": "…", "followers": 1200}],
  "max_sources": 60
}
```

`median_views` — та же `norm_views`. `added_at` — `source_added_at`.

`POST /sources` тело `{"input": "@nick, https://instagram.com/other/"}` →
`{"added": ["nick"], "restored": [], "already": [], "invalid": [{"input": "…", "reason": "…"}]}`.
Превышение `MAX_SOURCES` → 400 с `detail`.
`DELETE /sources/{username}` → `{"removed": true}` (404, если не источник).
`POST /sources/{username}/restore` → `{"restored": true}` (404, если не исключённый).

`GET /refresh/estimate`

```json
{"sources_total": 20, "to_refresh": 20, "deep": 3, "incremental": 17, "batches": 4,
 "reels_est": 435, "reels_usd": 1.13, "followers_profiles": 20, "followers_usd": 0.05,
 "total_usd": 1.18, "total_rub": 94.0, "rate": 80, "skipped_fresh": 0}
```

`POST /refresh` тело `{"force": false}` → `{"refresh_id": N}`; 400 — нет источников или все
свежие; 409 — уже идёт; 429 — лимит в час. У всех ошибок `detail` — русский текст для показа.

`GET /refresh/current` (он же шаг конвейера)

```json
{"id": 7, "status": "running", "stage": "reels", "stage_label": "рилсы 2/4 пачки",
 "batch_done": 1, "batch_total": 5, "reels_saved": 120,
 "cost_usd": 0.31, "cost_rub": 25.0,
 "errors": [{"batch": 3, "usernames": ["a"], "error": "…"}],
 "started_at": "…", "finished_at": null}
```

`status`: `idle|running|done|error`; `stage`: `reels|followers|finishing|null`. Обновлений не
было ни разу → `{"status": "idle"}`. После завершения отдаётся последнее обновление
(`done`/`error`) — без повторного продвижения.

### Клиент `frontend/src/api/radar.js` (дописать; существующее не менять)

```
getFeed({period, usernames, onlyViral, sort, order, limit, offset})   // usernames — массив
getSources()
addSources(input)            // строка
removeSource(username)
restoreSource(username)
getRefreshEstimate()
startRefresh(force = false)
getRefreshCurrent()
```

Ошибка HTTP → `throw` объекта `Error` с `message = detail` ответа и полем `status`
(по образцу уже существующих функций файла).

### `frontend/src/pages/radarTabs/format.js`

`fmtNum(n)` → «12,3к» / «1,2 млн» / «—» для null; `fmtX(n)` → «×4,2» / «—»;
`fmtDate(iso)` → «05.10.2026»; `fmtAgo(iso)` → «3 ч назад» / «2 дн назад» / «только что» / «—»;
`fmtRub(n)` → «94 ₽»; `fmtPerDay(n)` → «28,6к/день».

### Адрес страницы

`tab` = `feed|sources|oneoff`; дефолт `feed` в адрес не пишется. Нет `tab`, но есть любой из
старых ключей (`accounts, period, metric, sort, view, buckets, reel`) → `oneoff`. Смена вкладки
— новый набор параметров с одним `tab`. Ключи ленты: `fperiod` (7|30|90|0, дефолт 30),
`facc` (ник), `fsort`, `fdir` (asc|desc), `fviral` (1); дефолты не пишутся.
Из ленты в разбор: `?tab=oneoff&accounts=<ник>&select=<id>`; если
`analysis_status === 'done'` — `?tab=oneoff&accounts=<ник>&reel=<id>`.

### Компоненты

- `pages/Radar.jsx` — оболочка: `export default function Radar()`.
- `pages/radarTabs/OneOff.jsx` — прежний экран; `export default function OneOff()`, плюс
  именованные экспорты `ReelCard`, `ReportPanel`, `ErrorBanner`.
- `pages/radarTabs/Feed.jsx` — `export default function Feed({ refreshKey })`.
- `pages/radarTabs/Sources.jsx` — `export default function Sources({ refreshKey, onChanged })`.
- `pages/radarTabs/RefreshBar.jsx` — `export default function RefreshBar({ sourcesKey, onDone })`.
  `onDone()` зовётся при переходе `running → done|error`; оболочка увеличивает `refreshKey`,
  по которому Feed и Sources перезагружают данные. `Sources.onChanged()` увеличивает
  `sourcesKey`, по которому RefreshBar перечитывает оценку.

### Разметка и классы (CSS — `pages/radar-scout.css`, всё под `.radar`, префикс `rs-`)

Существующие классы `.card .btn .btn-primary .btn-ghost .seg .on .num .wrap .p-track .p-fill
.p-fill-running .p-text .topbar` берутся из `radar.css` как есть и не переопределяются.

Оболочка:

```
<div className="radar">
  <div className="topbar">…как сейчас…</div>
  <nav className="rs-tabs" role="tablist">
    <button role="tab" className="rs-tab on" aria-selected="true">Лента</button>
    <button role="tab" className="rs-tab">Источники</button>
    <button role="tab" className="rs-tab">Разовый сбор</button>
  </nav>
  {tab !== 'oneoff' && <RefreshBar/>}
  {вкладка}
</div>
```

RefreshBar:

```
<div className="wrap rs-wide">
<section className="rs-refresh" data-state="idle|running|done|error">
  <div className="rs-refresh-main">
    <button className="btn btn-primary rs-refresh-btn">Обновить</button>
    <div className="rs-refresh-info">
      <span className="rs-refresh-est">≈ <b className="num">94 ₽</b> · 20 источников (3 новых)</span>
      <span className="rs-refresh-last">Обновлено 3 ч назад</span>
    </div>
  </div>
  <div className="rs-refresh-progress">            {/* только running */}
    <div className="p-track"><div className="p-fill p-fill-running" style={{width:'40%'}}/></div>
    <div className="p-text rs-refresh-stage">рилсы 2/4 пачки</div>
  </div>
  <div className="rs-refresh-result">               {/* только done|error */}
    <span className="rs-refresh-summary">Сохранено 120 рилсов · 25 ₽</span>
    <ul className="rs-refresh-errors"><li>@a — текст ошибки</li></ul>
  </div>
</section>
</div>
```

Лента:

```
<div className="wrap rs-wide"><section className="card rs-feed">
  <div className="rs-filters">
    <div className="seg rs-period"><button className="on">30 дней</button>…</div>
    <select className="rs-select">…</select>
    <label className="rs-check"><input type="checkbox"/> Только залётные</label>
    <span className="rs-count">Рилсов: <b className="num">312</b></span>
  </div>
  <div className="rs-table-wrap"><table className="rs-table">
    <thead><tr>
      <th className="rs-th"/>                                      {/* превью */}
      <th className="rs-th rs-th-left">Аккаунт</th>
      <th className="rs-th rs-th-sort desc" data-key="viral">Залётность</th>
      …data-key: views, speed, reach, posted_at, followers, likes, comments…
      <th className="rs-th"/> <th className="rs-th"/>               {/* 🔥, действие */}
    </tr></thead>
    <tbody><tr className="rs-row" data-id="C1abc">
      <td className="rs-td-thumb"><a className="rs-thumb"><img/><span className="rs-dur">0:31</span></a></td>
      <td className="rs-td-acc"><a className="rs-acc">@x</a><div className="rs-caption">…</div></td>
      <td className="rs-td-num">
        <span className="rs-badge viral v3|v5|v10">×4,0</span>     {/* is_viral */}
        <span className="rs-viral-plain">×1,2</span>               {/* надёжно, но ниже порога */}
        <span className="rs-badge low" title="мало данных">×2,1</span> {/* !norm_reliable */}
        <span className="rs-viral-none">—</span>                   {/* viral_ratio null */}
      </td>
      <td className="rs-td-num">…</td>  {/* просмотры, скорость, охват, дата, подписчики, лайки, комментарии */}
      <td className="rs-td-rise"><span className="rs-badge rising">🔥 набирает</span></td>
      <td className="rs-td-act"><button className="btn btn-ghost rs-analyze-btn">Разобрать</button></td>
    </tr></tbody>
  </table></div>
  <div className="rs-more"><button className="btn btn-ghost">Показать ещё</button></div>
  <div className="rs-empty">…</div>
  <div className="rs-skeleton">…</div>
</section></div>
```

Порядок колонок: превью, аккаунт, залётность, просмотры, скорость, охват, дата, подписчики,
лайки, комментарии, 🔥, действие. Заголовок сортировки: `.rs-th-sort` всегда у сортируемых,
`.asc`/`.desc` — только у активного. В ячейке залётности ровно один из четырёх вариантов.
Кнопка действия: «Разобрать», а при `analysis_status === 'done'` — «Отчёт».

Источники:

```
<div className="wrap rs-wide"><section className="card rs-sources">
  <div className="rs-add">
    <input className="rs-add-input" placeholder="@ник или ссылка на профиль"/>
    <button className="btn btn-primary rs-add-btn">Добавить</button>
  </div>
  <div className="rs-add-msg">…итог добавления / ошибки…</div>
  <div className="rs-table-wrap"><table className="rs-table rs-src-table">
    <thead>…Аккаунт, Подписчики, Медиана, Последний рилс, Рилсов, Залётных, Обновлён, ''…</thead>
    <tbody><tr className="rs-row" data-username="x">
      <td className="rs-td-acc"><a className="rs-acc">@x</a><span className="rs-fullname">Имя</span></td>
      <td className="rs-td-num">…</td>…
      <td className="rs-td-num"><span className="rs-badge new">ещё не обновлялся</span></td>
      <td className="rs-td-act"><button className="btn btn-ghost rs-remove">Убрать</button></td>
    </tr></tbody>
  </table></div>
  <details className="rs-excluded"><summary>Исключённые (2)</summary>
    <ul className="rs-excluded-list">
      <li className="rs-excluded-item"><span className="rs-acc">@y</span>
        <button className="btn btn-ghost rs-restore">Вернуть</button></li>
    </ul>
  </details>
  <div className="rs-empty">…</div>
</section></div>
```

Модификаторы: `.rs-wide` (max-width 1400px), `.rs-badge.viral.v3|v5|v10`, `.rs-badge.rising`,
`.rs-badge.low`, `.rs-badge.new`, `.rs-th-sort.asc|.desc`, `.rs-th-left` (заголовок колонки
«Аккаунт» в обеих таблицах — влево; остальные заголовки вправо). Таблица скроллится горизонтально
внутри `.rs-table-wrap`; никаких `display:flex` на `td`/`tr`. Отступы и ширины — явными
значениями, без `justify-content: space-between`.

## Затрагивается

- Новые файлы бэкенда: `radar/intel_settings.py`, `radar/repo_intel.py`, `radar/feed.py`,
  `radar/sources.py`, `radar/refresh_service.py`, `api/radar_intel.py`, `api/radar_refresh.py`,
  `models/radar_intel_schemas.py`, тесты.
- Правки бэкенда: `radar/repo.py`, `radar/settings.py`, `radar/scrape_service.py`,
  `radar/apify_runs.py`, `main.py`, `tests/test_radar_api.py`.
- Фронт: `pages/Radar.jsx` (оболочка), `pages/radarTabs/*`, `pages/radar-scout.css`,
  `api/radar.js`.
- БД: миграция 0011 (`supabase db push`).
- НЕ трогаются: `core/config.py`, `models/schemas.py`, `api/client.js`, `workers/*`,
  `pipeline/*`, `api/radar.py`, `pages/radar.css`, `chat/*`.

## Вне рамок

- Автообновление по расписанию; поиск новых источников (рекомендации похожих аккаунтов).
- Аватары (ссылки Instagram протухают).
- Залётность в ИИ-чате; чистка старых снимков.
- Пересчёт цифр рилсов старше 14 дней при лёгком обновлении.

## Реализация

- **Модель сессии:** Opus — ТЗ, контракт, ревью, приёмка, мёрж; код пишут Sonnet-субагенты.
- **Волна 1 (параллельно, файлы не пересекаются):**
  - B1 «Обновление»: миграция 0011; правки `radar/repo.py`, `radar/settings.py`,
    `radar/scrape_service.py`, `radar/apify_runs.py`, `main.py`; новые
    `radar/refresh_service.py`, `api/radar_refresh.py`, `tests/test_radar_refresh.py`; мок
    снимков в `tests/test_radar_api.py`. `main.py` подключает ОБА новых роутера
    (`app.api.radar_intel.router` и `app.api.radar_refresh.router`).
  - B2 «Лента и источники»: `radar/intel_settings.py`, `radar/repo_intel.py`, `radar/feed.py`,
    `radar/sources.py`, `api/radar_intel.py`, `models/radar_intel_schemas.py`,
    `tests/test_radar_feed.py`, `tests/test_radar_sources.py`. В `repo.py` не пишет и из него
    не импортирует.
  - C «Стили»: только `pages/radar-scout.css`.
- **Волна 2 (после отчётов всех трёх):**
  - F1: `pages/Radar.jsx`, `radarTabs/OneOff.jsx` (перенос `git mv`), `Sources.jsx`,
    `RefreshBar.jsx`, `format.js`, `api/radar.js`.
  - F2: только `radarTabs/Feed.jsx`.
- **Приёмка** — после отчётов F1 и F2.

## Критерии приёмки

1. `pytest backend/tests` зелёный целиком, включая прежние `test_radar_api.py` и
   `test_radar_pipeline.py`; `ruff check backend` без новых замечаний; `npm run build` проходит.
2. Старая ссылка `/radar?accounts=a&reel=X` открывает «Разовый сбор» с отчётом; сценарий
   «Шаг 1 → 4» работает как раньше; разовый сбор не делает аккаунт источником.
3. Скрипт Playwright на подменённых ответах: три вкладки; сортировка по клику меняет
   `fsort`/`fdir` и порядок строк; фильтры пишутся в адрес; бейджи залётности и «набирает»
   на месте; «Разобрать» ведёт на `tab=oneoff&…&select=…`; полоса обновления проходит
   `idle → running → done` и `error`; «Убрать» → «Исключённые» → «Вернуть».
4. Один платный прогон на двух источниках: цифры ленты сходятся с Instagram, записана
   фактическая цена запроса подписчиков, проверено, что лимит рилсов считается на ник.
