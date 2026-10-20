import test from 'node:test'
import assert from 'node:assert/strict'
import { DEFAULT_FAVORITE_AUTHORS, FAVORITE_AUTHORS_KEY, readFavoriteAuthors, writeFavoriteAuthors, toggleFavoriteAuthor } from './favoriteAuthors.js'

function memoryStorage(initial = null) {
  let value = initial
  return {
    getItem(key) { assert.equal(key, FAVORITE_AUTHORS_KEY); return value },
    setItem(key, next) { assert.equal(key, FAVORITE_AUTHORS_KEY); value = next },
  }
}

test('дефолты используются только до первой записи, в том числе пустой', () => {
  const storage = memoryStorage()
  assert.deepEqual(readFavoriteAuthors(storage).authors, DEFAULT_FAVORITE_AUTHORS)
  writeFavoriteAuthors([], storage)
  assert.deepEqual(readFavoriteAuthors(storage), { authors: [], error: '' })
})

test('нормализует, удаляет дубли и добавляет/удаляет автора', () => {
  const storage = memoryStorage()
  writeFavoriteAuthors(['@Alice', 'alice', 'Bob'], storage)
  const authors = readFavoriteAuthors(storage).authors
  assert.deepEqual(authors, ['alice', 'bob'])
  assert.deepEqual(toggleFavoriteAuthor(authors, 'BOB'), ['alice'])
  assert.deepEqual(toggleFavoriteAuthor(authors, 'Carol'), ['alice', 'bob', 'carol'])
})

test('ошибки чтения и записи видимы вызывающему коду', () => {
  const broken = { getItem() { throw new Error('denied') }, setItem() { throw new Error('denied') } }
  assert.match(readFavoriteAuthors(broken).error, /Не удалось прочитать/)
  assert.throws(() => writeFavoriteAuthors([], broken), /Не удалось сохранить/)
})

test('профиль принимает URL и @username, отклоняет публикации и чужие домены', async () => {
  const { parseFavoriteProfile } = await import('./favoriteAuthors.js')
  assert.equal(parseFavoriteProfile(' https://www.instagram.com/Alice/?igsh=abc '), 'alice')
  assert.equal(parseFavoriteProfile('@Bob'), 'bob')
  for (const value of ['https://evil.test/alice/', 'https://instagram.com/p/abc/', 'http://instagram.com/alice/', 'https://instagram.com@evil.test/alice/', 'alice', 'https://instagram.com/reels/']) {
    assert.throws(() => parseFavoriteProfile(value), /Вставьте ссылку/)
  }
})

test('серверное избранное читает статусы, записывает нормализованное имя и удаляет без JSON', async () => {
  const { getFavoriteAuthors, addFavoriteAuthor, removeFavoriteAuthor } = await import('./favoriteAuthors.js')
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push([url, options])
    return options.method === 'DELETE' ? { ok: true, status: 204 } : { ok: true, json: async () => options.method === 'POST' ? { username: 'alice', status: 'pending' } : { authors: [{ username: 'alice', status: 'updated' }] } }
  }
  try {
    assert.equal((await getFavoriteAuthors()).authors[0].status, 'updated')
    assert.equal((await addFavoriteAuthor('https://instagram.com/Alice/')).status, 'pending')
    assert.deepEqual(JSON.parse(calls[1][1].body), { profile: '@alice' })
    assert.equal(await removeFavoriteAuthor('Alice'), null)
    assert.equal(calls[2][0], '/api/radar/carousels/favorites/alice')
    globalThis.fetch = async () => ({ ok: false, status: 429, json: async () => ({ detail: 'Список переполнен' }) })
    await assert.rejects(addFavoriteAuthor('@alice'), /Список переполнен/)
  } finally { globalThis.fetch = previous }
})
