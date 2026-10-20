const ID = /^[0-9]{1,40}$/
const PERIODS = new Set(['7d', '14d', 'archive14d'])
const FIELDS = ['id', 'username', 'permalink', 'shortcode', 'timestamp', 'caption', 'media_type',
  'media_url', 'thumbnail_url', 'likes', 'comments', 'views', 'followers', 'collected_at']

function safeUrl(value, media = false) {
  if (value == null) return null
  if (typeof value !== 'string') throw new Error()
  const url = new URL(value)
  const host = url.hostname.toLowerCase()
  const allowed = media ? ['instagram.com', 'cdninstagram.com', 'fbcdn.net'] : ['instagram.com']
  if (url.protocol !== 'https:' || url.username || url.password || !allowed.some(name => host === name || host.endsWith(`.${name}`))) throw new Error()
  return value
}

function snapshotOf(source, id) {
  if (!source || typeof source !== 'object' || Array.isArray(source)) throw new Error()
  const snapshot = Object.fromEntries(FIELDS.map(field => [field, source[field] ?? null]))
  if (String(snapshot.id) !== id || snapshot.media_type !== 'CAROUSEL_ALBUM') throw new Error()
  for (const field of ['username', 'shortcode', 'timestamp', 'caption', 'collected_at']) {
    if (snapshot[field] != null && typeof snapshot[field] !== 'string') throw new Error()
  }
  if (snapshot.shortcode != null && !/^[\w-]+$/.test(snapshot.shortcode)) throw new Error()
  for (const field of ['likes', 'comments', 'views', 'followers']) {
    if (snapshot[field] != null && (typeof snapshot[field] !== 'number' || !Number.isFinite(snapshot[field]) || snapshot[field] < 0)) throw new Error()
  }
  snapshot.id = id
  snapshot.permalink = safeUrl(snapshot.permalink)
  snapshot.media_url = safeUrl(snapshot.media_url, true)
  snapshot.thumbnail_url = safeUrl(snapshot.thumbnail_url, true)
  return snapshot
}

function normalizeItem(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error()
  const id = String(item.id)
  if (!ID.test(id) || !PERIODS.has(item.source_period) || !Number.isFinite(Date.parse(item.selected_at))) throw new Error()
  return { id, selected_at: item.selected_at, source_period: item.source_period,
    snapshot: snapshotOf(item.snapshot, id) }
}

function normalizeItems(items) {
  if (!Array.isArray(items) || items.length > 10000) throw new Error()
  const seen = new Set()
  const clean = []
  for (const value of items) {
    const item = normalizeItem(value)
    if (!seen.has(item.id)) clean.push(item)
    seen.add(item.id)
  }
  return clean
}

const BASE = (import.meta.env?.VITE_API_URL || '') + '/api/radar/carousel-likes'

async function request(path, options = {}, fetcher = globalThis.fetch) {
  let response
  try { response = await fetcher(BASE + path, { ...options, cache: 'no-store' }) }
  catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new Error('Нет соединения с сервером. Список не изменён.')
  }
  if (!response.ok) throw new Error(`Не удалось ${options.method ? 'сохранить отметку' : 'загрузить подборку'} (${response.status}). Попробуйте ещё раз.`)
  try { return await response.json() }
  catch { throw new Error('Сервер вернул некорректные данные.') }
}

export async function fetchSelectedCarousels(signal, fetcher) {
  const data = await request('', { signal }, fetcher)
  try { return { items: normalizeItems(data.items), total: data.total } }
  catch { throw new Error('Сервер вернул некорректную подборку.') }
}

export async function setCarouselSelected(id, period, selected, fetcher) {
  id = String(id ?? '')
  if (!ID.test(id)) throw new Error('Некорректный ID карусели')
  if (typeof selected !== 'boolean') throw new Error('Не указано состояние отметки')
  if (selected && !PERIODS.has(period)) throw new Error('Некорректный период каруселей')
  const data = await request(`/${id}`, selected ? {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ period }),
  } : { method: 'DELETE' }, fetcher)
  try {
    if (selected) {
      const item = normalizeItem(data.item)
      if (item.id !== id) throw new Error()
      return item
    }
    if (String(data.id) !== id || data.selected !== false) throw new Error()
    return null
  } catch { throw new Error('Сервер не подтвердил изменение отметки.') }
}
