import test from 'node:test'
import assert from 'node:assert/strict'
import {
  CarouselSelectionApiError,
  getCarouselSelection,
  getCarouselSelectionStatus,
  getCarouselSelections,
  putCarouselSelection,
  deleteCarouselSelection,
} from './carouselSelections.js'

const base = '/api/radar/carousel-selections'

function mockFetch(t, handler) {
  const original = globalThis.fetch
  globalThis.fetch = handler
  t.after(() => { globalThis.fetch = original })
}

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

test('GET списка формирует query и передаёт токен, AbortSignal и no-store', async t => {
  const controller = new AbortController()
  mockFetch(t, async (url, options) => {
    assert.equal(url, `${base}/?sort=likes&order=asc&limit=100&offset=20&q=cat`)
    assert.equal(options.headers.get('Authorization'), 'Bearer test-token')
    assert.equal(options.signal, controller.signal)
    assert.equal(options.cache, 'no-store')
    assert.equal(options.method, undefined)
    return jsonResponse({ items: [], total: 0, selected_total: 0 })
  })
  assert.deepEqual(await getCarouselSelections({ q: 'cat', sort: 'likes', order: 'asc', limit: 100, offset: 20 }, 'test-token', controller.signal), { items: [], total: 0, selected_total: 0 })
})

test('POST status передаёт IDs пакетно с JSON заголовком', async t => {
  mockFetch(t, async (url, options) => {
    assert.equal(url, `${base}/status`)
    assert.equal(options.method, 'POST')
    assert.equal(options.headers.get('Content-Type'), 'application/json')
    assert.deepEqual(JSON.parse(options.body), { ids: ['123', '456'] })
    assert.equal(options.cache, 'no-store')
    return jsonResponse({ states: [], selected_total: 0 })
  })
  await getCarouselSelectionStatus(['123', '456'], 'token')
})

test('GET детали кодирует ID и использует тот же защищённый транспорт', async t => {
  mockFetch(t, async (url, options) => {
    assert.equal(url, `${base}/ig%2Fid`)
    assert.equal(options.headers.get('Authorization'), 'Bearer token')
    assert.equal(options.cache, 'no-store')
    return jsonResponse({ id: 'ig/id' })
  })
  assert.deepEqual(await getCarouselSelection('ig/id', 'token'), { id: 'ig/id' })
})

test('PUT отправляет период и ожидаемую ревизию', async t => {
  mockFetch(t, async (url, options) => {
    assert.equal(url, `${base}/123`)
    assert.equal(options.method, 'PUT')
    assert.deepEqual(JSON.parse(options.body), { source_period: '14d', expected_revision: 2 })
    return jsonResponse({ item: { id: '123' }, selected_total: 1 })
  })
  await putCarouselSelection('123', { source_period: '14d', expected_revision: 2 }, 'token')
})

test('DELETE передаёт expected_revision в query', async t => {
  mockFetch(t, async (url, options) => {
    assert.equal(url, `${base}/123?expected_revision=4`)
    assert.equal(options.method, 'DELETE')
    assert.equal(options.body, undefined)
    return jsonResponse({ id: '123', selected: false, revision: 5, selected_total: 0 })
  })
  await deleteCarouselSelection('123', 4, 'token')
})

test('401 нормализуется в ошибку входа с машинным кодом', async t => {
  mockFetch(t, async () => jsonResponse({ detail: 'Expired' }, 401))
  await assert.rejects(getCarouselSelections({}, 'expired'), error => {
    assert.ok(error instanceof CarouselSelectionApiError)
    assert.equal(error.status, 401)
    assert.equal(error.code, 'AUTH_REQUIRED')
    assert.equal(error.message, 'Сессия истекла. Войдите снова.')
    assert.equal(error.detail, 'Expired')
    return true
  })
})

test('409 сохраняет данные актуального состояния для разрешения конфликта', async t => {
  const current = { id: '123', selected: true, revision: 8 }
  mockFetch(t, async () => jsonResponse({ detail: 'Revision conflict', current }, 409))
  await assert.rejects(putCarouselSelection('123', { source_period: '7d', expected_revision: 3 }, 'token'), error => {
    assert.ok(error instanceof CarouselSelectionApiError)
    assert.equal(error.status, 409)
    assert.equal(error.code, 'REVISION_CONFLICT')
    assert.deepEqual(error.current, current)
    assert.equal(error.body.detail, 'Revision conflict')
    return true
  })
})

test('ошибка AbortSignal сохраняет AbortError', async t => {
  mockFetch(t, async () => { throw Object.assign(new Error('aborted'), { name: 'AbortError' }) })
  await assert.rejects(getCarouselSelections({}, 'token'), { name: 'AbortError' })
})
