export const FAVORITE_AUTHORS_KEY = 'rs:carousels:favorite-authors:v1'
export const DEFAULT_FAVORITE_AUTHORS = ['alex_magnier', 'artemiy.miller', 'ilia.paliy', 'romarayt']

const USERNAME = /^[a-z0-9._]{1,30}$/

export function normalizeFavoriteAuthor(value) {
  const username = String(value || '').trim().replace(/^@/, '').toLowerCase()
  if (!USERNAME.test(username)) throw new Error('Некорректное имя автора')
  return username
}

export function readFavoriteAuthors(storage) {
  try {
    const raw = (storage || window.localStorage).getItem(FAVORITE_AUTHORS_KEY)
    if (raw == null) return { authors: [...DEFAULT_FAVORITE_AUTHORS], error: '' }
    const data = JSON.parse(raw)
    if (data.version !== 1 || !Array.isArray(data.authors) || data.authors.length > 1600) throw new Error()
    const authors = []
    const seen = new Set()
    for (const value of data.authors) {
      const username = normalizeFavoriteAuthor(value)
      if (seen.has(username)) throw new Error()
      seen.add(username)
      authors.push(username)
    }
    return { authors, error: '' }
  } catch {
    return { authors: [...DEFAULT_FAVORITE_AUTHORS], error: 'Не удалось прочитать избранных авторов. Данные сайта недоступны или повреждены.' }
  }
}

export function writeFavoriteAuthors(authors, storage) {
  try {
    const clean = []
    const seen = new Set()
    if (!Array.isArray(authors) || authors.length > 1600) throw new Error()
    for (const value of authors) {
      const username = normalizeFavoriteAuthor(value)
      if (!seen.has(username)) clean.push(username)
      seen.add(username)
    }
    ;(storage || window.localStorage).setItem(FAVORITE_AUTHORS_KEY, JSON.stringify({ version: 1, authors: clean }))
  } catch {
    throw new Error('Не удалось сохранить избранных авторов. Список не изменён: проверьте доступность данных сайта.')
  }
}

export function toggleFavoriteAuthor(authors, username) {
  username = normalizeFavoriteAuthor(username)
  return authors.includes(username) ? authors.filter(author => author !== username) : [...authors, username]
}

const FAVORITES_BASE = (import.meta.env?.VITE_API_URL || '') + '/api/radar/carousels/favorites'
export const FAVORITES_IMPORT_KEY = 'rs:carousels:favorite-authors:imported:v1'

export function parseFavoriteProfile(value) {
  const input = String(value || '').trim()
  if (/^@[a-z0-9._]{1,30}$/i.test(input)) return normalizeFavoriteAuthor(input)
  try {
    const url = new URL(input)
    if (url.protocol !== 'https:' || !['instagram.com', 'www.instagram.com'].includes(url.hostname) || url.port || url.username || url.password) throw new Error()
    const match = url.pathname.match(/^\/([a-z0-9._]{1,30})\/?$/i)
    if (!match || ['p', 'reel', 'reels', 'stories', 'explore', 'accounts', 'direct'].includes(match[1].toLowerCase())) throw new Error()
    return normalizeFavoriteAuthor(match[1])
  } catch { throw new Error('Вставьте ссылку на профиль Instagram или @username, а не ссылку на публикацию.') }
}

async function favoritesRequest(path = '', options = {}, signal) {
  let response
  try { response = await fetch(FAVORITES_BASE + path, { ...options, signal }) }
  catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new Error('Нет соединения с сервером. Избранное не изменено.')
  }
  if (!response.ok) {
    let message = `Не удалось обновить избранное (${response.status})`
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') message = body.detail
    } catch { /* Ответ может не содержать JSON. */ }
    throw new Error(message)
  }
  return response.status === 204 ? null : response.json()
}

export const getFavoriteAuthors = signal => favoritesRequest('', {}, signal)
export const addFavoriteAuthor = profile => {
  const username = parseFavoriteProfile(profile)
  return favoritesRequest('', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile: '@' + username }) })
}
export const removeFavoriteAuthor = username => favoritesRequest('/' + encodeURIComponent(normalizeFavoriteAuthor(username)), { method: 'DELETE' })
