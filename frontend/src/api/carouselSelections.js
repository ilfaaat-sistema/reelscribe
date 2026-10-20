const BASE = `${import.meta.env?.VITE_API_URL || ''}/api/radar/carousel-selections`

const ERROR_MESSAGES = {
  401: 'Сессия истекла. Войдите снова.',
  409: 'Подборка изменилась в другом запросе. Обновите состояние и повторите действие.',
}

export class CarouselSelectionApiError extends Error {
  constructor(status, body = null) {
    const detail = typeof body?.detail === 'string' && body.detail.trim() ? body.detail : null
    super(ERROR_MESSAGES[status] || detail || `Ошибка запроса (${status})`)
    this.name = 'CarouselSelectionApiError'
    this.status = status
    this.code = status === 401 ? 'AUTH_REQUIRED' : status === 409 ? 'REVISION_CONFLICT' : 'HTTP_ERROR'
    this.detail = detail
    this.body = body
    // Удобно для разрешения конфликта без повторного запроса.
    if (status === 409 && body && typeof body === 'object') this.current = body.current ?? body.state ?? null
  }
}

async function request(path, token, signal, options = {}) {
  const headers = new Headers(options.headers || {})
  if (token) headers.set('Authorization', `Bearer ${token}`)
  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...options,
      headers,
      signal,
      cache: 'no-store',
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new Error('Нет соединения с сервером')
  }

  let body = null
  try {
    body = await response.json()
  } catch { /* Пустой или не-JSON ответ. */ }
  if (!response.ok) throw new CarouselSelectionApiError(response.status, body)
  return body
}

function jsonOptions(method, body) {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

export function getCarouselSelections({ q = '', sort = 'selected_at', order = 'desc', limit = 50, offset = 0 } = {}, token, signal) {
  const query = new URLSearchParams({ sort, order, limit: String(limit), offset: String(offset) })
  if (q) query.set('q', q)
  return request(`/?${query}`, token, signal)
}

export function getCarouselSelectionStatus(ids, token, signal) {
  return request('/status', token, signal, jsonOptions('POST', { ids }))
}

export function getCarouselAvailability(ids, period, token, signal) {
  return request('/availability', token, signal, jsonOptions('POST', { ids, period }))
}

export function getCarouselSelection(id, token, signal) {
  return request(`/${encodeURIComponent(id)}`, token, signal)
}

export function putCarouselSelection(id, { source_period, expected_revision }, token, signal) {
  return request(`/${encodeURIComponent(id)}`, token, signal, jsonOptions('PUT', { source_period, expected_revision }))
}

export function deleteCarouselSelection(id, expected_revision, token, signal) {
  const query = new URLSearchParams({ expected_revision: String(expected_revision) })
  return request(`/${encodeURIComponent(id)}?${query}`, token, signal, { method: 'DELETE' })
}
