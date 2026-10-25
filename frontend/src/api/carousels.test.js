import test from 'node:test'
import assert from 'node:assert/strict'
import { getCarousels, getCarousel, translateCarousel } from './carousels.js'

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
    assert.equal(calls[0][1].method, 'POST')
    assert.equal(JSON.parse(calls[0][1].body).period, '14d')
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


test('архив 14 дней имеет отдельный URL и не меняет исторический 7d', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async url => { calls.push(url); return { ok: true, json: async () => ({ items: [] }) } }
  try {
    await getCarousels({ period: 'archive14d' })
    await getCarousel('123', undefined, 'archive14d')
    await getCarousels({ period: '7d' })
    assert.equal(new URL(calls[0], 'https://test.local').searchParams.get('period'), 'archive14d')
    assert.equal(calls[1], '/api/radar/carousels/123?period=archive14d')
    assert.equal(new URL(calls[2], 'https://test.local').searchParams.get('period'), '7d')
  } finally { globalThis.fetch = previous }
})

test('выключенное избранное передаёт favorites_only=false в тот же 14d API, деталь сохраняет period', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => { calls.push([url, options]); return { ok: true, json: async () => ({ items: [] }) } }
  try {
    await getCarousels({ period: '14d', favorites_only: false, sort: 'timestamp', exclude_authors: ['hidden'] })
    const body = JSON.parse(calls[0][1].body)
    assert.equal(body.period, '14d')
    assert.equal(body.favorites_only, false)
    assert.deepEqual(body.exclude_authors, ['hidden'])
    await getCarousel('123', undefined, '14d')
    assert.equal(calls[1][0], '/api/radar/carousels/123?period=14d')
  } finally { globalThis.fetch = previous }
})

test('30d сохраняется в API, деталях и выключении ограничения', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => { calls.push([url, options]); return { ok: true, json: async () => ({ items: [] }) } }
  try {
    await getCarousels({ period: '30d', favorites_only: false })
    assert.equal(JSON.parse(calls[0][1].body).period, '30d')
    assert.equal(JSON.parse(calls[0][1].body).favorites_only, false)
    await getCarousel('123', undefined, '30d')
    assert.equal(calls[1][0], '/api/radar/carousels/123?period=30d')
  } finally { globalThis.fetch = previous }
})

test('перевод отправляет period и source_hash, не передавая текст', async () => {
  const previous = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push([url, options])
    return { ok: true, json: async () => ({ id: '123', caption_translation: { source_hash: 'abc', state: 'ready', translation: 'Перевод', source_language: 'en' }, cached: false }) }
  }
  try {
    const signal = new AbortController().signal
    const result = await translateCarousel('123 /?', { period: 'archive14d', sourceHash: 'abc' }, signal)
    assert.equal(calls[0][0], '/api/radar/carousels/123%20%2F%3F/translate')
    assert.equal(calls[0][1].method, 'POST')
    assert.equal(calls[0][1].signal, signal)
    assert.deepEqual(JSON.parse(calls[0][1].body), { period: 'archive14d', source_hash: 'abc' })
    assert.equal(result.caption_translation.translation, 'Перевод')
  } finally { globalThis.fetch = previous }
})

test('перевод передаёт код source_changed для обновления деталей', async () => {
  const previous = globalThis.fetch
  globalThis.fetch = async () => ({ ok: false, status: 409, json: async () => ({ code: 'source_changed', detail: 'Текст изменился.' }) })
  try {
    await assert.rejects(
      translateCarousel('123', { sourceHash: 'old' }),
      error => error.code === 'source_changed' && error.status === 409 && /изменился/.test(error.message),
    )
  } finally { globalThis.fetch = previous }
})

test('ошибки перевода показывают короткие статусы и сохраняют отмену', async () => {
  const previous = globalThis.fetch
  try {
    globalThis.fetch = async () => ({ ok: false, status: 429, json: async () => ({ detail: 'internal quota id 42' }) })
    await assert.rejects(translateCarousel('123', { sourceHash: 'abc' }), /Лимит переводов/)
    const abort = new Error('cancelled')
    abort.name = 'AbortError'
    globalThis.fetch = async () => { throw abort }
    await assert.rejects(translateCarousel('123', { sourceHash: 'abc' }), error => error === abort)
  } finally { globalThis.fetch = previous }
})

test('произвольные даты и числовые фильтры отправляются POST также для архивов', async () => {
  const previous = globalThis.fetch
  const bodies = []
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/radar/carousels')
    assert.equal(options.method, 'POST')
    bodies.push(JSON.parse(options.body))
    return { ok: true, json: async () => ({ items: [], total: 0 }) }
  }
  try {
    await getCarousels({ period: 'custom', date_from: '2026-06-25', date_to: '2026-10-25', min_likes: 0, max_comments: 20 })
    assert.equal(bodies[0].favorites_only, true)
    assert.equal(bodies[0].date_from, '2026-06-25')
    assert.equal(bodies[0].min_likes, 0)
    assert.equal(bodies[0].max_likes, null)
    await getCarousels({ period: '7d', min_comments: 3 })
    assert.equal(bodies[1].period, '7d')
    assert.equal(bodies[1].min_comments, 3)
  } finally { globalThis.fetch = previous }
})
