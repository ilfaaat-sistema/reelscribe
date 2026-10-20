import test from 'node:test'
import assert from 'node:assert/strict'
import { getCarousels, getCarousel } from './carousels.js'

test('список передаёт фильтр, сортировку и страницу только своему API', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push([url, options])
    return { ok: true, json: async () => ({ items: [], total: 0, collection: { status: 'running' } }) }
  }
  try {
    const signal = new AbortController().signal
    const data = await getCarousels({ author: 'автор & co', sort: 'likes', order: 'desc', limit: 100, offset: 200 }, signal)
    const url = new URL(calls[0][0], 'https://example.test')
    assert.equal(url.pathname, '/api/radar/carousels')
    assert.deepEqual(Object.fromEntries(url.searchParams), {
      sort: 'likes', order: 'desc', limit: '100', offset: '200', author: 'автор & co',
    })
    assert.equal(calls[0][1].signal, signal)
    assert.equal(data.collection.status, 'running')
    await getCarousel('179/ unsafe?', signal)
    assert.equal(calls[1][0], '/api/radar/carousels/179%2F%20unsafe%3F')
  } finally {
    globalThis.fetch = previous
  }
})

test('503 показывает сообщение API, а сетевые и отменённые запросы различаются', async () => {
  const previous = globalThis.fetch
  try {
    globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({ detail: 'Сбор ещё не завершён' }) })
    await assert.rejects(getCarousels(), /Сбор ещё не завершён/)
    globalThis.fetch = async () => { throw new Error('offline') }
    await assert.rejects(getCarousels(), /Нет соединения с сервером/)
    const abort = new Error('cancelled')
    abort.name = 'AbortError'
    globalThis.fetch = async () => { throw abort }
    await assert.rejects(getCarousels(), error => error === abort)
  } finally {
    globalThis.fetch = previous
  }
})

test('Скрытые авторы передаются в JSON для фильтрации до пагинации', async () => {
  const previous = globalThis.fetch
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/radar/carousels')
    assert.equal(options.method, 'POST')
    const body = JSON.parse(options.body)
    assert.deepEqual(body.exclude_authors, ['alice', 'bob'])
    assert.equal(body.offset, 100)
    return { ok: true, json: async () => ({ total: 10, items: [] }) }
  }
  try { assert.equal((await getCarousels({ exclude_authors: ['alice', 'bob'], offset: 100 })).total, 10) }
  finally { globalThis.fetch = previous }
})

test('избранные авторы отправляются POST даже при пустом списке и сочетаются со скрытыми', async () => {
  const previous = globalThis.fetch
  const bodies = []
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/radar/carousels')
    assert.equal(options.method, 'POST')
    bodies.push(JSON.parse(options.body))
    return { ok: true, json: async () => ({ items: [], total: 0 }) }
  }
  try {
    await getCarousels({ include_authors: [], exclude_authors: ['hidden'] })
    await getCarousels({ include_authors: ['alice', 'bob'] })
    assert.deepEqual(bodies[0].include_authors, [])
    assert.deepEqual(bodies[0].exclude_authors, ['hidden'])
    assert.deepEqual(bodies[1].include_authors, ['alice', 'bob'])
  } finally { globalThis.fetch = previous }
})

test('источник NEYRO_SET7 отправляется POST вместе с фильтрами избранных и скрытых', async () => {
  const previous = globalThis.fetch
  let body
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/radar/carousels')
    assert.equal(options.method, 'POST')
    body = JSON.parse(options.body)
    return { ok: true, json: async () => ({ items: [], total: 7 }) }
  }
  try {
    const result = await getCarousels({ source_account: 'neyro_set7', include_authors: ['alice'], exclude_authors: ['hidden'], offset: 50 })
    assert.equal(result.total, 7)
    assert.equal(body.source_account, 'neyro_set7')
    assert.deepEqual(body.include_authors, ['alice'])
    assert.deepEqual(body.exclude_authors, ['hidden'])
    assert.equal(body.offset, 50)
  } finally { globalThis.fetch = previous }
})

test('период передаётся списку GET, фильтрам POST и деталям; даты ответа сохраняются', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push([url, options])
    return { ok: true, json: async () => ({ start_date: '2026-10-06', end_date: '2026-10-19', items: [] }) }
  }
  try {
    const data = await getCarousels({ period: '14d', author: 'alice', sort: 'timestamp' })
    assert.equal(new URL(calls[0][0], 'https://example.test').searchParams.get('period'), '14d')
    assert.equal(data.start_date, '2026-10-06')
    assert.equal(data.end_date, '2026-10-19')
    await getCarousels({ period: '7d', include_authors: ['alice'], exclude_authors: ['bob'] })
    const body = JSON.parse(calls[1][1].body)
    assert.equal(body.period, '7d')
    assert.deepEqual(body.include_authors, ['alice'])
    assert.deepEqual(body.exclude_authors, ['bob'])
    await getCarousel('123', undefined, '14d')
    assert.equal(calls[2][0], '/api/radar/carousels/123?period=14d')
  } finally { globalThis.fetch = previous }
})
