// Клиент API раздела «Радар». База — как в client.js: на проде фронт и бэк на
// одном домене (VITE_API_URL не задаётся, запросы идут на относительный
// /api/radar), локально бэкенд поднят отдельно и VITE_API_URL указывается явно
// (см. CLAUDE.md проекта). Разбор ошибок — тоже как в client.js: detail из тела
// ответа, иначе человеческий текст по коду статуса.
const BASE = (import.meta.env.VITE_API_URL || '') + '/api/radar'

const STATUS_MESSAGES = {
  400: 'Некорректный запрос (400)',
  401: 'Требуется авторизация (401)',
  403: 'Доступ запрещён (403)',
  404: 'Не найдено (404)',
  408: 'Истекло время ожидания (408)',
  409: 'Конфликт данных (409)',
  422: 'Некорректные данные (422)',
  429: 'Слишком много запросов, попробуйте позже (429)',
  500: 'Ошибка сервера (500)',
  502: 'Сервер недоступен (502)',
  503: 'Сервис временно недоступен (503)',
  504: 'Сервер не отвечает (504)',
}

function statusMessage(status) {
  return STATUS_MESSAGES[status] || `Ошибка запроса (${status})`
}

async function req(path, opts = {}) {
  let r
  try {
    r = await fetch(BASE + path, opts)
  } catch (e) {
    if (e?.name === 'AbortError') throw e
    throw new Error('Нет соединения с сервером')
  }
  if (!r.ok) {
    let message = statusMessage(r.status)
    try {
      const txt = await r.text()
      if (txt) {
        try {
          const data = JSON.parse(txt)
          if (data && typeof data.detail === 'string' && data.detail.trim()) {
            message = data.detail
          } else if (data && data.detail != null) {
            message = JSON.stringify(data.detail)
          }
        } catch (_) {
          // тело не JSON — оставляем сообщение по статусу
        }
      }
    } catch (_) {
      // не удалось прочитать тело — оставляем сообщение по статусу
    }
    const err = new Error(message)
    err.status = r.status
    throw err
  }
  const ct = r.headers.get('content-type') || ''
  return ct.includes('json') ? r.json() : r.blob()
}

const postJson = body => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

// GET /competitors — список ранее собранных конкурентов с подписчиками
export const getCompetitors = () => req('/competitors')

// GET /config — доступность Whisper (настроен ли на сервере OPENAI_API_KEY)
export const getConfig = () => req('/config')

// GET /scrape/cost-estimate — оценка стоимости сбора на Apify до запуска
export const getCostEstimate = (usernames, periodMonths) =>
  req(`/scrape/cost-estimate?usernames=${encodeURIComponent(usernames.join(','))}&period_months=${periodMonths}`)

// POST /scrape — запуск платного сбора метаданных рилсов
export const startScrape = (usernames, periodMonths) =>
  req('/scrape', postJson({ usernames, period_months: periodMonths }))

// POST /competitors/refresh-followers — обновить подписчиков (без usernames — для всех)
export const refreshFollowers = usernames =>
  req('/competitors/refresh-followers', postJson(usernames?.length ? { usernames } : {}))

// GET /scrape/{run_id} — статус прогона сбора; побочный эффект на бэке — приём датасета
export const getScrapeStatus = runId => req(`/scrape/${runId}`)

// GET /summary — сводка и корзины по просмотрам для набора аккаунтов
export const getSummary = usernames =>
  req(`/summary${usernames?.length ? `?usernames=${encodeURIComponent(usernames.join(','))}` : ''}`)

// GET /reels — лента рилсов с полями analysis_status/analysis_mode
export const getReels = (usernames, sort = 'views', opts = {}) => {
  const qs = new URLSearchParams()
  if (usernames?.length) qs.set('usernames', usernames.join(','))
  qs.set('sort', sort)
  qs.set('order', opts.order || 'desc')
  qs.set('limit', opts.limit ?? 200)
  if (opts.minViews != null) qs.set('min_views', opts.minViews)
  if (opts.maxViews != null) qs.set('max_views', opts.maxViews)
  return req(`/reels?${qs}`)
}

// POST /analyze — постановка рилсов в очередь на разбор (t|v|tv);
// transcriber — 'gemini' (по умолчанию) | 'whisper' — движок расшифровки для режимов t/tv
export const startAnalyze = (items, transcriber = 'gemini', force = false) =>
  req('/analyze', postJson(force ? { items, transcriber, force } : { items, transcriber }))

// POST /jobs/{reel_id}/run — синхронный прогон одного задания (до ~90 с);
// таймаут fetch намеренно не ставим короче 120 с — тут его просто нет.
export const runJob = reelId => req(`/jobs/${reelId}/run`, { method: 'POST' })

// POST /jobs/tick — страховка для зависших заданий; вызывается pg_cron,
// фронтом не используется, но входит в контракт эндпоинтов ТЗ.
export const tickJob = () => req('/jobs/tick', { method: 'POST' })

// POST /analyze/cancel — отмена ещё не завершённых заданий разбора
export const cancelAnalyze = reelIds => req('/analyze/cancel', postJson({ reel_ids: reelIds }))

// GET /analyze/status — статусы пачки заданий для поллинга
export const getAnalyzeStatus = reelIds =>
  req(`/analyze/status?reel_ids=${encodeURIComponent(reelIds.join(','))}`)

// GET /reels/{id}/analysis — рилс + результат разбора для отчёта
export const getReelAnalysis = id => req(`/reels/${id}/analysis`)

// GET /reels/{id}/report.md — прямая ссылка на скачивание (используется как href)
export const reportUrl = id => `${BASE}/reels/${id}/report.md`

// GET /export.md?reel_ids= — сводный отчёт по пачке рилсов (используется как href)
export const exportUrl = reelIds => `${BASE}/export.md?reel_ids=${encodeURIComponent(reelIds.join(','))}`

// ── Разведка (ТЗ 10): источники, лента, обновление кнопкой ──────────────

// GET /feed — лента рилсов источников с залётностью; usernames — массив
export const getFeed = ({ period, usernames, onlyViral, sort, order, limit, offset } = {}) => {
  const qs = new URLSearchParams()
  if (period != null) qs.set('period', period)
  if (usernames?.length) qs.set('usernames', usernames.join(','))
  if (onlyViral) qs.set('only_viral', 1)
  if (sort) qs.set('sort', sort)
  if (order) qs.set('order', order)
  if (limit != null) qs.set('limit', limit)
  if (offset != null) qs.set('offset', offset)
  return req(`/feed?${qs}`)
}

// GET /sources — активные и исключённые источники
export const getSources = () => req('/sources')

// POST /sources — добавить источники одной строкой («@ник, ссылка на профиль»)
export const addSources = input => req('/sources', postJson({ input }))

// DELETE /sources/{username} — убрать источник в исключённые
export const removeSource = username =>
  req(`/sources/${encodeURIComponent(username)}`, { method: 'DELETE' })

// POST /sources/{username}/restore — вернуть исключённый источник
export const restoreSource = username =>
  req(`/sources/${encodeURIComponent(username)}/restore`, { method: 'POST' })

// GET /refresh/estimate — цена и состав обновления до запуска
export const getRefreshEstimate = () => req('/refresh/estimate')

// POST /refresh — запуск обновления источников (платный Apify)
export const startRefresh = (force = false) => req('/refresh', postJson({ force }))

// GET /refresh/current — состояние обновления; каждый вызов двигает конвейер на бэке
export const getRefreshCurrent = () => req('/refresh/current')
