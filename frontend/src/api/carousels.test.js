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
