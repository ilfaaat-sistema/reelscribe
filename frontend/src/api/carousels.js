import { isLiveCarouselPeriod } from './carouselFilters.js'

const BASE = (import.meta.env?.VITE_API_URL || '') + '/api/radar/carousels'

async function readJson(url, signal, options = {}) {
  let response
  try {
    response = await fetch(url, { ...options, signal })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new Error('Нет соединения с сервером')
  }

  if (!response.ok) {
    let message = response.status === 503
      ? 'Отчёт о каруселях пока не готов'
      : `Не удалось загрузить карусели (${response.status})`
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string' && body.detail.trim()) message = body.detail
    } catch { /* Ошибка сервера может прийти без JSON. */ }
    throw new Error(message)
  }
  return response.json()
}

const TRANSLATION_ERROR_MESSAGES = {
  source_changed: 'Текст публикации изменился. Повторите перевод после обновления данных.',
  translation_in_progress: 'Перевод уже выполняется. Попробуйте ещё раз чуть позже.',
  translation_not_needed: 'Этот текст не требует перевода.',
}

const TRANSLATION_STATUS_MESSAGES = {
  413: 'Текст слишком длинный для перевода.',
  422: 'Не удалось обработать текст для перевода.',
  429: 'Лимит переводов исчерпан. Попробуйте позже.',
  502: 'Сервис перевода ответил с ошибкой.',
  503: 'Перевод сейчас недоступен.',
}

function translationError(response, body) {
  const detail = body?.detail
  const code = body?.code || (detail && typeof detail === 'object' ? detail.code : null)
  const serverMessage = typeof detail === 'string'
    ? detail
    : (typeof detail?.message === 'string' ? detail.message : (typeof body?.message === 'string' ? body.message : ''))
  const message = TRANSLATION_ERROR_MESSAGES[code]
    || TRANSLATION_STATUS_MESSAGES[response.status]
    || serverMessage
    || `Не удалось перевести текст (${response.status})`
  const error = new Error(message)
  error.code = code || null
  error.status = response.status
  return error
}

export function getCarousels({ period = null, author = '', sort = 'likes', order = 'desc', limit = 50, offset = 0, exclude_authors = [], include_authors = null, source_account = null, favorites_only = true, date_from = null, date_to = null, min_likes = null, max_likes = null, min_comments = null, max_comments = null } = {}, signal) {
  const ranges = { date_from, date_to, min_likes, max_likes, min_comments, max_comments }
  const hasRanges = Object.values(ranges).some(value => value != null)
  if (hasRanges || isLiveCarouselPeriod(period) || include_authors != null || exclude_authors.length || source_account === 'neyro_set7') {
    return readJson(BASE, signal, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...(hasRanges ? ranges : {}), ...(period ? { period } : {}), ...(isLiveCarouselPeriod(period) ? { favorites_only } : {}), author, sort, order, limit, offset, exclude_authors, ...(include_authors == null ? {} : { include_authors }), ...(source_account === 'neyro_set7' ? { source_account } : {}) }),
    })
  }
  const query = new URLSearchParams({ sort, order, limit: String(limit), offset: String(offset) })
  if (period) query.set('period', period)
  if (author) query.set('author', author)
  return readJson(`${BASE}?${query}`, signal)
}

export function getCarousel(id, signal, period = null) {
  return readJson(`${BASE}/${encodeURIComponent(id)}${period ? `?${new URLSearchParams({ period })}` : ''}`, signal)
}

export async function translateCarousel(id, { period = null, sourceHash } = {}, signal) {
  let response
  try {
    response = await fetch(`${BASE}/${encodeURIComponent(id)}/translate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ period, source_hash: sourceHash }),
      signal,
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new Error('Нет соединения с сервером')
  }

  if (!response.ok) {
    let body = null
    try { body = await response.json() } catch { /* Ошибка сервера может прийти без JSON. */ }
    throw translationError(response, body)
  }
  return response.json()
}
