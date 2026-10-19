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
