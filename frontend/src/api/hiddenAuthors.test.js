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

const api = await import('./hiddenAuthors.js')
const item = { username: 'alice', hidden_at: '2026-10-19T10:00:00Z' }
const response = data => ({ ok: true, json: async () => data })

test('Общий список и импорт используют no-store, нормализуют имя и сохраняют дату', async () => {
  const calls = []
  const fetcher = async (url, options) => { calls.push({ url, options }); return response({ authors: [item], total: 1 }) }
  const controller = new AbortController()
  assert.deepEqual(await api.fetchHiddenAuthors(controller.signal, fetcher), { authors: [item], total: 1 })
  await api.importHiddenAuthors([{ ...item, username: '@ALICE' }], controller.signal, fetcher)
  assert.equal(calls[0].options.cache, 'no-store')
  assert.equal(calls[0].options.signal, controller.signal)
  assert.match(calls[1].url, /\/hidden-authors\/import$/)
  assert.equal(calls[1].options.method, 'POST')
  assert.deepEqual(JSON.parse(calls[1].options.body), { authors: [item] })
  assert.equal(calls[1].options.cache, 'no-store')
})

test('PUT и DELETE требуют подтверждение именно запрошенного автора', async () => {
  let call
  assert.deepEqual(await api.setAuthorHidden('@ALICE', true, async (url, options) => {
    call = { url, options }; return response({ item })
  }), item)
  assert.match(call.url, /\/alice$/)
  assert.equal(call.options.method, 'PUT')
  assert.equal(call.options.cache, 'no-store')
  assert.equal(await api.setAuthorHidden('alice', false, async (_url, options) => {
    assert.equal(options.method, 'DELETE'); return response({ username: 'alice', hidden: false })
  }), null)
  await assert.rejects(api.setAuthorHidden('alice', true, async () => response({ item: { ...item, username: 'bob' } })), /не подтвердил/)
  await assert.rejects(api.setAuthorHidden('alice', false, async () => response({ username: 'bob', hidden: false })), /не подтвердил/)
})

test('Некорректный импорт отклоняется до запроса, ошибки сети и сервера видимы', async () => {
  const forbidden = () => { throw new Error('Запрос не должен выполняться') }
  for (const authors of [[{ ...item, username: '../alice' }], [{ ...item, hidden_at: 'bad' }], [item, item], Array(1601).fill(item)]) {
    await assert.rejects(api.importHiddenAuthors(authors, undefined, forbidden), /Некоррект/)
  }
  await assert.rejects(api.fetchHiddenAuthors(undefined, async () => { throw new Error('secret') }), /Нет соединения/)
  await assert.rejects(api.fetchHiddenAuthors(undefined, async () => ({ ok: false, status: 503 })), /503/)
  await assert.rejects(api.fetchHiddenAuthors(undefined, async () => response({ authors: [item], total: 2 })), /некорректный список/)
  const abort = new DOMException('Отменено', 'AbortError')
  await assert.rejects(api.fetchHiddenAuthors(undefined, async () => { throw abort }), error => error === abort)
})

// Минимальный исполнитель hooks проверяет сетевые гонки без добавления тестовых зависимостей.
async function hookFixture(legacy) {
  const { readFile } = await import('node:fs/promises')
  const states = [], refs = [], effects = []
  let stateIndex = 0, refIndex = 0
  const calls = []
  const deferred = () => {
    let resolve, reject
    const promise = new Promise((yes, no) => { resolve = yes; reject = no })
    return { promise, resolve, reject }
  }
  const pending = []
  const network = (kind, args) => { const task = deferred(); calls.push({ kind, args }); pending.push(task); return task.promise }
  const fixture = {
    useState: initial => { const index = stateIndex++; states[index] = typeof initial === 'function' ? initial() : initial; return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value }] },
    useRef: value => { const index = refIndex++; refs[index] ||= { current: value }; return refs[index] },
    useEffect: effect => effects.push(effect), useCallback: callback => callback,
    readHiddenAuthors: () => legacy, normalizeAuthor: api.normalizeAuthor,
    fetchHiddenAuthors: (...args) => network('get', args), importHiddenAuthors: (...args) => network('import', args), setAuthorHidden: (...args) => network('set', args),
  }
  globalThis.__hiddenHookFixture = fixture
  const originalWindow = globalThis.window, originalDocument = globalThis.document
  const events = new Map()
  globalThis.window = { setInterval: callback => { events.set('timer', callback); return 1 }, clearInterval() {}, addEventListener: (key, callback) => events.set(key, callback), removeEventListener() {} }
  globalThis.document = { visibilityState: 'visible', addEventListener: (key, callback) => events.set(key, callback), removeEventListener() {} }
  const source = (await readFile(new URL('../hooks/useHiddenAuthors.js', import.meta.url), 'utf8'))
    .replace(/^import .*\n/gm, '')
  const injected = 'const {useState,useRef,useEffect,useCallback,readHiddenAuthors,normalizeAuthor,fetchHiddenAuthors,importHiddenAuthors,setAuthorHidden}=globalThis.__hiddenHookFixture;\n' + source
  const { default: runHook } = await import('data:text/javascript;base64,' + Buffer.from(injected).toString('base64') + '#' + Math.random())
  const hook = runHook()
  const cleanup = effects[0]()
  return { hook, states, calls, pending, events, cleanup: () => { cleanup(); globalThis.window = originalWindow; globalThis.document = originalDocument; delete globalThis.__hiddenHookFixture } }
}
const settle = () => new Promise(resolve => setImmediate(resolve))

test('Hook: импорт до чтения, устаревший GET не заменяет PUT, повторный PUT заблокирован', async () => {
  const fixture = await hookFixture({ authors: [item], error: '' })
  try {
    assert.equal(fixture.calls[0].kind, 'import')
    fixture.pending[0].resolve({ authors: [item] })
    await settle()
    assert.deepEqual(fixture.states[0], [item])
    const refresh = fixture.hook.refresh()
    assert.equal(fixture.calls[1].kind, 'get')
    const mutation = fixture.hook.setHidden('bob', true)
    assert.equal(await fixture.hook.setHidden('@BOB', false), false)
    assert.equal(fixture.calls.length, 3)
    assert.equal(fixture.calls[1].args[0].aborted, true)
    const bob = { ...item, username: 'bob' }
    fixture.pending[2].resolve(bob)
    assert.equal(await mutation, true)
    fixture.pending[1].resolve({ authors: [] })
    await refresh
    assert.deepEqual(fixture.states[0], [bob, item])
    assert.equal(fixture.states[4].size, 0)
  } finally { fixture.cleanup() }
})

test('Hook: недоступный localStorage не блокирует сервер; failed mutation не меняет список', async () => {
  const fixture = await hookFixture({ authors: [], error: 'Недоступно' })
  try {
    assert.equal(fixture.calls[0].kind, 'get')
    assert.match(fixture.states[3], /резервную копию/)
    fixture.pending[0].resolve({ authors: [item] })
    await settle()
    const mutation = fixture.hook.setHidden('alice', false)
    fixture.pending[1].reject(new Error('Сбой сервера'))
    assert.equal(await mutation, false)
    assert.deepEqual(fixture.states[0], [item])
    assert.equal(fixture.states[2], 'Сбой сервера')
    fixture.events.get('visibilitychange')()
    assert.equal(fixture.calls[2].kind, 'get')
    fixture.pending[2].resolve({ authors: [] })
    await settle()
    assert.deepEqual(fixture.states[0], [])
  } finally { fixture.cleanup() }
})

test('Hook: legacy остаётся до подтверждения; неудачный импорт повторяется и не открывает записи', async () => {
  const fixture = await hookFixture({ authors: [item], error: '' })
  try {
    assert.deepEqual(fixture.states[0], [item])
    assert.equal(await fixture.hook.setHidden('bob', true), false)
    fixture.pending[0].reject(new Error('Импорт недоступен'))
    await settle()
    assert.deepEqual(fixture.states[0], [item])
    assert.equal(fixture.states[2], 'Импорт недоступен')
    const retry = fixture.hook.refresh()
    assert.equal(fixture.calls[1].kind, 'import')
    fixture.pending[1].resolve({ authors: [] })
    await retry
    assert.deepEqual(fixture.states[0], [])
    assert.equal(fixture.states[2], '')
  } finally { fixture.cleanup() }
})
