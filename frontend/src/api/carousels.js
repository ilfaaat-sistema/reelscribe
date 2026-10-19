const BASE = (import.meta.env?.VITE_API_URL || '') + '/api/radar/carousels'

async function readJson(url, signal) {
  let response
  try {
    response = await fetch(url, { signal })
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

export function getCarousels({ author = '', sort = 'likes', order = 'desc', limit = 50, offset = 0 } = {}, signal) {
  const query = new URLSearchParams({ sort, order, limit: String(limit), offset: String(offset) })
  if (author) query.set('author', author)
  return readJson(`${BASE}?${query}`, signal)
}

export function getCarousel(id, signal) {
  return readJson(`${BASE}/${encodeURIComponent(id)}`, signal)
}
