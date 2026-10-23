import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import path from 'node:path'

// Переменная нужна только для проверки ещё не интегрированного соседнего worktree.
// После интеграции тест по умолчанию читает модули рядом с собой.
const apiUrl = process.env.CAROUSEL_COVER_FRONTEND_DIR
  ? pathToFileURL(path.join(process.env.CAROUSEL_COVER_FRONTEND_DIR, 'src/api/carouselCoverTranslations.js'))
  : new URL('./carouselCoverTranslations.js', import.meta.url)
const hookUrl = process.env.CAROUSEL_COVER_FRONTEND_DIR
  ? pathToFileURL(path.join(process.env.CAROUSEL_COVER_FRONTEND_DIR, 'src/hooks/useCarouselCoverTranslations.js'))
  : new URL('../hooks/useCarouselCoverTranslations.js', import.meta.url)

const {
  createCarouselCoverTranslationStore,
  getCarouselCoverTranslations,
  translateCarouselCover,
} = await import(apiUrl)

// Чистый gate не требует React. Удаление imports сохраняет фактическую реализацию функции.
const hookSource = (await readFile(hookUrl, 'utf8')).replace(/^import .*\n/gm, '')
const hookModule = await import('data:text/javascript;base64,' + Buffer.from(hookSource).toString('base64'))
const { createCoverVisibilityGate } = hookModule

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }

function clock() {
  let now = 0
  let sequence = 0
  const timers = new Map()
  return {
    now: () => now,
    schedule(fn, delay) {
      const id = ++sequence
      timers.set(id, { fn, at: now + delay })
      return id
    },
    cancel(id) { timers.delete(id) },
    pending() { return [...timers.values()].map(timer => timer.at - now) },
    async advance(milliseconds) {
      now += milliseconds
      for (let pass = 0; pass < 50; pass++) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= now)
        if (!due.length) break
        for (const [id, timer] of due) if (timers.delete(id)) timer.fn()
        await flush()
      }
    },
  }
}

const item = (id, status, text = '') => ({ id, status, source_text: text, text_ru: text })
const liveBudget = resetAt => ({ mode: 'live', pilot_limit: 20, daily_limit: 500, reset_at: resetAt })

test('500 ms считаются заново после hidden; до непрерывной видимости сети нет', async () => {
  const time = clock()
  const calls = []
  let tabVisible = true
  const store = createCarouselCoverTranslationStore({
    ...time,
    isVisible: () => tabVisible,
    read: async ids => {
      calls.push(['GET', ids])
      return { enabled: true, items: ids.map(id => item(id, 'missing')) }
    },
    start: async (id, period) => {
      calls.push(['POST', id, period])
      return item(id, 'ready', 'Перевод')
    },
  })
  const gate = createCoverVisibilityGate({
    onVisible: () => store.watch('101', '14d'),
    isVisible: () => tabVisible,
    schedule: time.schedule,
    cancel: time.cancel,
  })

  gate.update(true)
  await time.advance(499)
  assert.deepEqual(calls, [])
  tabVisible = false
  gate.update(true)
  await time.advance(10_000)
  assert.deepEqual(calls, [])

  tabVisible = true
  gate.update(true)
  await time.advance(499)
  assert.deepEqual(calls, [])
  await time.advance(1)
  assert.deepEqual(calls, [['GET', ['101']], ['POST', '101', '14d']])
  assert.equal(store.getSnapshot('101').status, 'ready')

  tabVisible = false
  gate.update(true)
  gate.dispose()
})

test('running опрашивается только GET, missing после запуска не создаёт второй POST', async () => {
  const time = clock()
  const calls = []
  let reads = 0
  const store = createCarouselCoverTranslationStore({
    ...time,
    isVisible: () => true,
    read: async ids => {
      calls.push('GET')
      reads++
      return { enabled: true, items: ids.map(id => item(id, reads === 1 ? 'running' : 'missing')) }
    },
    start: async id => { calls.push('POST'); return item(id, 'ready') },
  })

  store.watch('102', '7d')
  await time.advance(0)
  await time.advance(2_000)
  await time.advance(60_000)
  assert.deepEqual(calls, ['GET', 'GET'])
  assert.equal(store.getSnapshot('102').status, 'error')
})

test('клиент ограничивает суммарные запросы двумя и начинает каждый ID с batch GET', async () => {
  const time = clock()
  let active = 0
  let maximum = 0
  const releases = []
  const events = []
  const store = createCarouselCoverTranslationStore({
    ...time,
    isVisible: () => true,
    read: async ids => {
      events.push(['GET', ids])
      return { enabled: true, items: ids.map(id => item(id, 'missing')) }
    },
    start: (id, period) => {
      events.push(['POST', id, period])
      active++
      maximum = Math.max(maximum, active)
      return new Promise(resolve => releases.push(() => {
        active--
        resolve(item(id, 'ready', 'Готово'))
      }))
    },
  })

  for (const id of ['1', '2', '3', '4']) store.watch(id, 'archive14d')
  await time.advance(0)
  assert.deepEqual(events[0], ['GET', ['1', '2', '3', '4']])
  assert.equal(releases.length, 2)
  while (releases.length) {
    releases.shift()()
    await flush()
  }
  assert.equal(maximum, 2)
  assert.equal(events.filter(event => event[0] === 'POST').length, 4)
})

test('live retry_after: до срока нет POST, после срока сначала GET и затем POST', async () => {
  const time = clock()
  const resetAt = new Date(10_000).toISOString()
  const calls = []
  let reads = 0
  const store = createCarouselCoverTranslationStore({
    ...time,
    isVisible: () => true,
    read: async ids => {
      calls.push(['GET', ids])
      reads++
      return {
        enabled: true,
        budget: liveBudget(resetAt),
        items: ids.map(id => reads === 1
          ? { ...item(id, 'limit_reached'), retry_after: resetAt }
          : item(id, 'missing')),
      }
    },
    start: async (id, period) => {
      calls.push(['POST', id, period])
      return item(id, 'ready', 'Готово')
    },
  })

  store.watch('201', '30d')
  await time.advance(0)
  await time.advance(9_999)
  assert.deepEqual(calls, [['GET', ['201']]])
  await time.advance(1)
  assert.deepEqual(calls, [['GET', ['201']], ['GET', ['201']], ['POST', '201', '30d']])
  assert.equal(store.getSnapshot('201').status, 'ready')
})

test('retry_after, наступивший в hidden, проверяется через visibility/focus seams', async () => {
  const time = clock()
  const resetAt = new Date(1_000).toISOString()
  const calls = []
  const events = new Map()
  let visible = true
  let reads = 0
  const target = {
    addEventListener(name, listener) { events.set(name, listener) },
    removeEventListener(name, listener) { if (events.get(name) === listener) events.delete(name) },
  }
  const store = createCarouselCoverTranslationStore({
    ...time,
    isVisible: () => visible,
    focusTarget: target,
    visibilityTarget: target,
    read: async ids => {
      calls.push('GET')
      reads++
      return {
        enabled: true,
        budget: liveBudget(resetAt),
        items: ids.map(id => reads === 1
          ? { ...item(id, 'limit_reached'), retry_after: resetAt }
          : item(id, 'missing')),
      }
    },
    start: async id => { calls.push('POST'); return item(id, 'ready', 'Готово') },
  })

  const release = store.watch('202', '7d')
  await time.advance(0)
  visible = false
  await time.advance(10_000)
  assert.deepEqual(calls, ['GET'])
  events.get('focus')()
  await flush()
  assert.deepEqual(calls, ['GET'])
  visible = true
  events.get('visibilitychange')()
  await flush()
  assert.deepEqual(calls, ['GET', 'GET', 'POST'])
  release()
  assert.equal(events.size, 0)
})

test('дальний retry_after ограничен int32, pilot null и ready не опрашиваются', async () => {
  const safeMaximum = 2 ** 31 - 1
  for (const scenario of ['far', 'pilot', 'ready']) {
    const time = clock()
    const calls = []
    const resetAt = new Date(safeMaximum + 50_000).toISOString()
    const store = createCarouselCoverTranslationStore({
      ...time,
      isVisible: () => true,
      read: async ids => {
        calls.push('GET')
        const response = scenario === 'far'
          ? { ...item(ids[0], 'limit_reached'), retry_after: resetAt }
          : scenario === 'pilot'
            ? { ...item(ids[0], 'limit_reached'), retry_after: null }
            : item(ids[0], 'ready', 'Кэш')
        return { enabled: true, budget: scenario === 'pilot'
          ? { mode: 'pilot', pilot_limit: 20, daily_limit: 500, reset_at: resetAt }
          : liveBudget(resetAt), items: [response] }
      },
      start: async () => { calls.push('POST'); return item('203', 'ready') },
    })
    store.watch('203', '7d')
    await time.advance(0)
    assert.equal(calls.filter(call => call === 'POST').length, 0, scenario)
    if (scenario === 'far') assert.ok(time.pending().every(delay => delay <= safeMaximum))
    else assert.deepEqual(time.pending(), [], scenario)
    store.wake()
    await flush()
    assert.deepEqual(calls, ['GET'], scenario)
  }
})

test('HTTP client seams нормализуют UTC budget/retry_after и передают только period', async () => {
  const original = globalThis.fetch
  const calls = []
  const controller = new AbortController()
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return {
      ok: true,
      json: async () => options.method === 'POST'
        ? { item: { ...item('301', 'limit_reached'), retry_after: '2026-10-24T00:00:00+03:00' } }
        : {
            enabled: true,
            budget: { mode: 'live', pilot_limit: 20, daily_limit: 500, reset_at: '2026-10-24T00:00:00+03:00' },
            items: [{ ...item('301', 'limit_reached'), retry_after: '2026-10-24T00:00:00+03:00' }],
          },
    }
  }
  try {
    const read = await getCarouselCoverTranslations(['301'], controller.signal)
    assert.deepEqual(read.budget, liveBudget('2026-10-23T21:00:00.000Z'))
    assert.equal(read.items[0].retry_after, '2026-10-23T21:00:00.000Z')
    assert.equal(calls[0].options.signal, controller.signal)
    const started = await translateCarouselCover('301', 'archive14d', controller.signal)
    assert.equal(started.retry_after, '2026-10-23T21:00:00.000Z')
    assert.deepEqual(JSON.parse(calls[1].options.body), { period: 'archive14d' })
    assert.equal(calls[1].options.signal, controller.signal)
    assert.equal(calls[1].options.method, 'POST')
  } finally { globalThis.fetch = original }
})
