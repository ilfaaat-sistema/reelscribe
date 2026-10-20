// Отдельные данные сайта: обычная перезагрузка и очистка сетевого кэша их не удаляют.
export const HIDDEN_AUTHORS_KEY = 'rs:carousels:hidden-authors:v1'
const USERNAME = /^[a-z0-9._]{1,30}$/

export function normalizeAuthor(value) {
  const username = String(value || '').trim().replace(/^@/, '').toLowerCase()
  if (!USERNAME.test(username)) throw new Error('Некорректное имя автора')
  return username
}

export function readHiddenAuthors(storage) {
  try {
    const raw = (storage || window.localStorage).getItem(HIDDEN_AUTHORS_KEY)
    if (!raw) return { authors: [], error: '' }
    const data = JSON.parse(raw)
    if (data.version !== 1 || !Array.isArray(data.authors) || data.authors.length > 1600) throw new Error()
    const authors = []
    const seen = new Set()
    for (const item of data.authors) {
      const username = normalizeAuthor(item.username)
      if (seen.has(username) || !Number.isFinite(Date.parse(item.hidden_at))) throw new Error()
      seen.add(username)
      authors.push({ username, hidden_at: item.hidden_at })
    }
    return { authors, error: '' }
  } catch {
    return { authors: [], error: 'Не удалось прочитать скрытых авторов. Данные сайта недоступны или повреждены.' }
  }
}

export function writeHiddenAuthors(authors, storage) {
  try {
    if (authors.length > 1600) throw new Error()
    ;(storage || window.localStorage).setItem(HIDDEN_AUTHORS_KEY, JSON.stringify({ version: 1, authors }))
  } catch {
    throw new Error('Не удалось сохранить список в браузере. Список не изменён: проверьте доступность данных сайта.')
  }
}

export function hideAuthor(authors, username, hiddenAt = new Date().toISOString()) {
  username = normalizeAuthor(username)
  return authors.some(item => item.username === username) ? authors : [{ username, hidden_at: hiddenAt }, ...authors]
}

export function restoreAuthor(authors, username) {
  username = normalizeAuthor(username)
  return authors.filter(item => item.username !== username)
}

const BASE = (import.meta.env?.VITE_API_URL || '') + '/api/radar/hidden-authors'

function normalizeAuthors(authors) {
  if (!Array.isArray(authors) || authors.length > 1600) throw new Error('Некорректный список авторов')
  const seen = new Set()
  return authors.map(item => {
    const username = normalizeAuthor(item?.username)
    if (seen.has(username) || typeof item?.hidden_at !== 'string' || !Number.isFinite(Date.parse(item.hidden_at))) throw new Error('Некорректный список авторов')
    seen.add(username)
    return { username, hidden_at: item.hidden_at }
  })
}

async function request(path, options, fetcher = globalThis.fetch) {
  let response
  try { response = await fetcher(BASE + path, { ...options, cache: 'no-store' }) }
  catch (failure) {
    if (failure?.name === 'AbortError') throw failure
    throw new Error('Нет соединения с сервером. Список скрытых авторов не изменён.')
  }
  if (!response.ok) throw new Error(`Не удалось ${options.method ? 'сохранить' : 'загрузить'} скрытых авторов (${response.status}). Попробуйте ещё раз.`)
  try { return await response.json() }
  catch { throw new Error('Сервер вернул некорректные данные скрытых авторов.') }
}

function normalizeList(data) {
  try {
    const authors = normalizeAuthors(data.authors)
    if (data.total !== authors.length) throw new Error()
    return { authors, total: authors.length }
  } catch { throw new Error('Сервер вернул некорректный список скрытых авторов.') }
}

export async function fetchHiddenAuthors(signal, fetcher) {
  return normalizeList(await request('', { signal }, fetcher))
}

export async function importHiddenAuthors(authors, signal, fetcher) {
  const clean = normalizeAuthors(authors)
  return normalizeList(await request('/import', {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ authors: clean }),
  }, fetcher))
}

export async function setAuthorHidden(username, hidden, fetcher) {
  username = normalizeAuthor(username)
  if (typeof hidden !== 'boolean') throw new Error('Не указано состояние скрытия')
  const data = await request(`/${encodeURIComponent(username)}`, { method: hidden ? 'PUT' : 'DELETE' }, fetcher)
  try {
    if (hidden) {
      const [item] = normalizeAuthors([data.item])
      if (item.username !== username) throw new Error()
      return item
    }
    if (data.username !== username || data.hidden !== false) throw new Error()
    return null
  } catch { throw new Error('Сервер не подтвердил изменение скрытого автора.') }
}
