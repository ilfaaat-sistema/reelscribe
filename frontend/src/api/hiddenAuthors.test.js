import test from 'node:test'
import assert from 'node:assert/strict'
import { HIDDEN_AUTHORS_KEY, readHiddenAuthors, writeHiddenAuthors, hideAuthor, restoreAuthor } from './hiddenAuthors.js'

function storage() {
  const values = new Map()
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
}

test('Скрытие сохраняется после повторного чтения и возвращается без дублей', () => {
  const store = storage()
  const authors = hideAuthor([], '@Alice', '2026-10-19T10:00:00Z')
  writeHiddenAuthors(authors, store)
  assert.deepEqual(readHiddenAuthors(store), { authors, error: '' })
  assert.equal(hideAuthor(authors, 'alice').length, 1)
  writeHiddenAuthors(restoreAuthor(authors, 'ALICE'), store)
  assert.deepEqual(readHiddenAuthors(store).authors, [])
})

test('Повреждённое и недоступное хранилище показывают ошибку', () => {
  const store = storage()
  store.setItem(HIDDEN_AUTHORS_KEY, '{broken')
  assert.match(readHiddenAuthors(store).error, /Не удалось прочитать/)
  const denied = { getItem() { throw new Error() }, setItem() { throw new Error() } }
  assert.ok(readHiddenAuthors(denied).error)
  assert.throws(() => writeHiddenAuthors([], denied), /Не удалось сохранить/)
})
