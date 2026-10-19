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
