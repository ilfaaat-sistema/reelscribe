const BASE = (import.meta.env.VITE_API_URL || '') + '/api'

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
          // тело не JSON (например, HTML страница ошибки) — оставляем сообщение по статусу
        }
      }
    } catch (_) {
      // не удалось прочитать тело — оставляем сообщение по статусу
    }
    throw new Error(message)
  }
  const ct = r.headers.get('content-type') || ''
  return ct.includes('json') ? r.json() : r.blob()
}

export const importLinks = (body) =>
  req('/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const previewImport = (links_text) =>
  req('/import/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ links_text }),
  })

export const getSessions = () => req('/sessions')

export const getSession = (id) => req(`/sessions/${id}`)

export const updateSessionComment = (id, comment) =>
  req(`/sessions/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ comment }),
  })

export const getReels = (params = {}, signal) => {
  const qs = new URLSearchParams()
  const mapped = { ...params }
  // map frontend desc:bool -> backend dir:asc|desc
  if ('desc' in mapped) {
    mapped.dir = mapped.desc === 'true' || mapped.desc === true ? 'desc' : 'asc'
    delete mapped.desc
  }
  Object.entries(mapped).forEach(([k, v]) => v != null && v !== '' && qs.set(k, v))
  return req(`/reels?${qs}`, signal ? { signal } : {}).then(data =>
    Array.isArray(data) ? { items: data, total: data.length } : { items: data.items || [], total: data.total ?? 0 }
  )
}

export const getReel = (id) => req(`/reels/${id}`)

export const updateNote = (id, note) =>
  req(`/reels/${id}/note`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note }),
  })

export const getProgress = (sessionId) =>
  req(`/progress?session=${sessionId}`)

export const retryJobs = (sessionId) =>
  req(`/retry${sessionId ? `?session=${sessionId}` : ''}`, { method: 'POST' })

export const exportUrl = (params = {}) =>
  `${BASE}/export?${new URLSearchParams(params)}`

export const getErrors = (session) =>
  req(`/errors${session ? `?session=${session}` : ''}`)

export const errorsExportUrl = (params = {}) =>
  `${BASE}/errors/export?${new URLSearchParams(params)}`
