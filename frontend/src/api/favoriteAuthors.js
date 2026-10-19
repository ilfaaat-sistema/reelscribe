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
