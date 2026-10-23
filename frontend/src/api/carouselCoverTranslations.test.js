import test from 'node:test'
import assert from 'node:assert/strict'
import { createCarouselCoverTranslationStore, getCarouselCoverTranslations, translateCarouselCover } from './carouselCoverTranslations.js'

const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve() }
function clock() {
  let time = 0
  let counter = 0
  const timers = new Map()
  return {
    now: () => time,
    jump(ms) { time += ms },
    pending: () => [...timers.values()].map(timer => timer.at - time),
    schedule(fn, delay) { const id = ++counter; timers.set(id, { fn, at: time + delay }); return id },
    cancel(id) { timers.delete(id) },
    async advance(ms) {
      time += ms
      for (let i = 0; i < 30; i++) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= time)
        if (!due.length) break
        for (const [id, timer] of due) { if (timers.delete(id)) timer.fn() }
        await flush()
      }
    },
  }
}
const item = (id, status = 'missing') => ({ id, status, source_text: 'Hello', text_ru: status === 'ready' ? 'Привет' : '' })
const batch = (ids, status = 'missing', enabled = true) => ({ enabled, items: ids.map(id => item(id, status)) })

function fixture(options = {}) {
  const time = clock()
  const calls = []
  const store = createCarouselCoverTranslationStore({
    ...time, isVisible: () => true,
    read: async ids => { calls.push(['GET', ids]); return batch(ids) },
    start: async (id, period) => { calls.push(['POST', id, period]); return item(id, 'ready') },
    ...options,
  })
  return { store, time, calls }
}

test('Пакетный GET предшествует POST, готовый результат общий для периода и Drawer', async () => {
  const { store, time, calls } = fixture()
  const release = store.watch('1', '7d')
  store.watch('2', '14d')
  await time.advance(0)
  assert.deepEqual(calls[0], ['GET', ['1', '2']])
  assert.equal(calls.filter(call => call[0] === 'POST').length, 2)
  assert.equal(store.getSnapshot('1').text_ru, 'Привет')
  release()
  store.watch('1', '30d')
  await time.advance(0)
  assert.equal(calls.length, 3)
})

test('Кэш сервера не запускает модель; disabled не обещает обработку', async () => {
  let posts = 0
  const { store, time } = fixture({
    read: async ids => ({ enabled: false, items: ids.map(id => item(id, id === '2' ? 'missing' : 'ready')) }),
    start: async () => { posts++; return item('1', 'ready') },
  })
  store.watch('1', '7d'); store.watch('2', '7d')
  await time.advance(0)
  assert.equal(store.getSnapshot('1').status, 'ready')
  assert.equal(store.getSnapshot('2').status, 'disabled')
  store.watch('3', '7d')
  await time.advance(10000)
  assert.equal(store.getSnapshot('3').status, 'ready')
  assert.equal(posts, 0)
})

test('Не более двух одновременных HTTP запросов', async () => {
  let pending = 0
  let maximum = 0
  const resolve = []
  const { store, time } = fixture({ start: id => {
    pending++; maximum = Math.max(maximum, pending)
    return new Promise(done => resolve.push(() => { pending--; done(item(id, 'ready')) }))
  } })
  for (const id of ['1', '2', '3', '4']) store.watch(id, '14d')
  await time.advance(0)
  assert.equal(resolve.length, 2)
  resolve.shift()(); await flush()
  assert.equal(resolve.length, 2)
  resolve.shift()(); await flush()
  while (resolve.length) { resolve.shift()(); await flush() }
  assert.equal(maximum, 2)
})

test('running опрашивается исключительно GET, даже если сервер позже вернул missing', async () => {
  let reads = 0
  let posts = 0
  const { store, time } = fixture({
    read: async ids => batch(ids, ++reads === 1 ? 'running' : 'missing'),
    start: async () => { posts++; return item('1', 'ready') },
  })
  store.watch('1', '7d')
  await time.advance(0); await time.advance(2000)
  assert.equal(reads, 2)
  assert.equal(posts, 0)
  assert.equal(store.getSnapshot('1').status, 'error')
})

test('Таймаут и терминальные ошибки не повторяют POST после повторного просмотра', async () => {
  for (const status of ['error', 'no_text', 'unreadable', 'image_unavailable', 'limit_reached', 'disabled', 'timeout']) {
    let posts = 0
    const { store, time } = fixture({ start: async id => {
      posts++
      if (status === 'timeout') throw new Error('timeout')
      return item(id, status)
    } })
    const release = store.watch('1', '7d')
    await time.advance(0)
    release(); store.watch('1', '14d')
    await time.advance(60000)
    assert.equal(posts, 1, status)
  }
})

test('busy повторяется ограниченно и каждый повтор предваряется GET', async () => {
  const calls = []
  const { store, time } = fixture({
    read: async ids => { calls.push('GET'); return batch(ids) },
    start: async id => { calls.push('POST'); return item(id, 'busy') },
  })
  store.watch('1', '7d')
  await time.advance(0); await time.advance(2000); await time.advance(5000); await time.advance(60000)
  assert.deepEqual(calls, ['GET', 'POST', 'GET', 'POST', 'GET', 'POST'])
  assert.equal(store.getSnapshot('1').status, 'busy')
})

test('Скрытая вкладка не начинает GET/POST, очередь приостанавливается между ответами', async () => {
  let visible = false
  const { store, time, calls } = fixture({ isVisible: () => visible })
  store.watch('1', '7d')
  await time.advance(0)
  assert.equal(calls.length, 0)
  visible = true; store.wake(); await flush()
  assert.equal(calls.length, 2)
  visible = false; store.watch('2', '7d')
  await time.advance(10000)
  assert.equal(calls.length, 2)
})

test('Ответ GET, пришедший после скрытия вкладки, не запускает POST', async () => {
  let visible = true
  let finish
  let posts = 0
  const { store, time } = fixture({
    isVisible: () => visible,
    read: ids => new Promise(resolve => { finish = () => resolve(batch(ids)) }),
    start: async id => { posts++; return item(id, 'ready') },
  })
  store.watch('1', '14d')
  await time.advance(0)
  visible = false; finish(); await flush()
  assert.equal(posts, 0)
})

test('Пакеты чтения не превышают 100 ID; HTTP ошибки чтения не запускают POST', async () => {
  const reads = []
  let posts = 0
  const { store, time } = fixture({
    read: async ids => { reads.push(ids.length); throw new Error('503') },
    start: async id => { posts++; return item(id, 'ready') },
  })
  for (let id = 0; id < 210; id++) store.watch(String(id), '14d')
  await time.advance(0)
  assert.deepEqual(reads, [100, 100, 10])
  assert.equal(posts, 0)
})

test('HTTP контракт: только ID в GET и period в POST, текст хранится plain text', async () => {
  const original = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return { ok: true, json: async () => options.method === 'POST'
      ? { item: item('1', 'ready') }
      : { enabled: true, items: [{ id: '1', status: 'ready', text_ru: '<script>текст</script>' }] } }
  }
  try {
    const result = await getCarouselCoverTranslations(['1', '1', '2'])
    assert.equal(result.items[0].text_ru, '<script>текст</script>')
    assert.equal(result.items[1].status, 'error')
    assert.match(calls[0].url, /ids=1%2C2$/)
    await translateCarouselCover('1', 'archive14d')
    assert.deepEqual(JSON.parse(calls[1].options.body), { period: 'archive14d' })
    assert.equal(calls[1].options.method, 'POST')
  } finally { globalThis.fetch = original }
})


const limited = (id, retryAfter) => ({ ...item(id, 'limit_reached'), retry_after: retryAfter })
const liveBudget = { mode: 'live', pilot_limit: 20, daily_limit: 500, reset_at: '2026-10-23T21:00:00.000Z' }

test('На московской полуночи после live-лимита сначала GET, затем POST только для missing', async () => {
  const calls = []
  let reads = 0
  const { store, time } = fixture({
    read: async ids => {
      calls.push('GET')
      return { enabled: true, budget: liveBudget, items: ids.map(id => ++reads === 1 ? limited(id, liveBudget.reset_at) : item(id)) }
    },
    start: async id => { calls.push('POST'); return item(id, 'ready') },
  })
  await time.advance(Date.parse('2026-10-23T20:59:59Z'))
  store.watch('1', '7d'); await time.advance(0)
  assert.deepEqual(store.getBudgetSnapshot(), liveBudget)
  assert.deepEqual(calls, ['GET'])
  await time.advance(999)
  assert.deepEqual(calls, ['GET'])
  await time.advance(1)
  assert.deepEqual(calls, ['GET', 'GET', 'POST'])
  assert.equal(store.getSnapshot('1').status, 'ready')
})

test('POST с limit_reached не расходует право запуска: после даты ответ GET ready не вызывает POST', async () => {
  const calls = []
  let reads = 0
  const { store, time } = fixture({
    read: async ids => { calls.push('GET'); return batch(ids, ++reads === 1 ? 'missing' : 'ready') },
    start: async id => { calls.push('POST'); return limited(id, '1970-01-01T00:00:01Z') },
  })
  store.watch('1', '14d'); await time.advance(0)
  assert.deepEqual(calls, ['GET', 'POST'])
  await time.advance(1000)
  assert.deepEqual(calls, ['GET', 'POST', 'GET'])
  assert.equal(store.getSnapshot('1').status, 'ready')
})

test('Отказ POST по live-бюджету может возобновиться после нового GET missing', async () => {
  const calls = []
  let posts = 0
  const { store, time } = fixture({
    read: async ids => { calls.push('GET'); return batch(ids) },
    start: async id => { calls.push('POST'); return ++posts === 1 ? limited(id, '1970-01-01T00:00:01Z') : item(id, 'ready') },
  })
  store.watch('1', '14d'); await time.advance(0); await time.advance(1000)
  assert.deepEqual(calls, ['GET', 'POST', 'GET', 'POST'])
})

test('Pilot retry_after=null терминален: нет polling, таймера или POST после focus', async () => {
  let reads = 0
  const focusTarget = new EventTarget()
  const { store, time, calls } = fixture({
    focusTarget,
    read: async ids => { reads++; return { enabled: true, items: ids.map(id => limited(id, null)) } },
  })
  const release = store.watch('1', '7d'); await time.advance(0)
  assert.deepEqual(time.pending(), [])
  await time.advance(30 * 86400000)
  focusTarget.dispatchEvent(new Event('focus')); await flush()
  release(); store.watch('1', '30d'); await time.advance(0)
  assert.equal(reads, 1)
  assert.equal(calls.length, 0)
  assert.deepEqual(time.pending(), [])
})

test('Deadline в hidden не запускает GET/POST; focus пересверяет время без выполнения старого таймера', async () => {
  let visible = true
  let reads = 0
  let posts = 0
  const focusTarget = new EventTarget()
  const visibilityTarget = new EventTarget()
  const { store, time } = fixture({
    focusTarget, visibilityTarget, isVisible: () => visible,
    read: async ids => ({ enabled: true, items: ids.map(id => ++reads === 1 ? limited(id, '1970-01-01T00:00:01Z') : item(id)) }),
    start: async id => { posts++; return item(id, 'ready') },
  })
  store.watch('1', '7d'); await time.advance(0)
  visible = false
  visibilityTarget.dispatchEvent(new Event('visibilitychange'))
  time.jump(2000)
  focusTarget.dispatchEvent(new Event('focus')); await flush()
  assert.equal(reads, 1)
  assert.equal(posts, 0)
  visible = true
  focusTarget.dispatchEvent(new Event('focus')); await flush()
  assert.equal(reads, 2)
  assert.equal(posts, 1)
})

test('После deadline вне viewport очередь не читает; повторный watch получает кэш первым', async () => {
  const calls = []
  let reads = 0
  const { store, time } = fixture({
    read: async ids => { calls.push('GET'); return { enabled: true, items: ids.map(id => ++reads === 1 ? limited(id, '1970-01-01T00:00:01Z') : item(id)) } },
    start: async id => { calls.push('POST'); return item(id, 'ready') },
  })
  const release = store.watch('1', '7d'); await time.advance(0)
  release(); await time.advance(2000)
  assert.deepEqual(calls, ['GET'])
  store.watch('1', '30d'); await time.advance(0)
  assert.deepEqual(calls, ['GET', 'GET', 'POST'])
})

test('Ни один таймер ожидания retry_after не превышает безопасный int32', async () => {
  let reads = 0
  const { store, time } = fixture({ read: async ids => {
    reads++
    return { enabled: true, items: ids.map(id => limited(id, '2030-01-01T00:00:00Z')) }
  } })
  store.watch('1', '7d'); await time.advance(0)
  assert.deepEqual(time.pending(), [2 ** 31 - 1])
  await time.advance(2 ** 31 - 1)
  assert.deepEqual(time.pending(), [2 ** 31 - 1])
  assert.equal(reads, 1)
})

test('Повтор просроченного retry_after не создаёт GET-цикл, focus допускает одну новую сверку', async () => {
  let reads = 0
  const focusTarget = new EventTarget()
  const { store, time } = fixture({ focusTarget, read: async ids => {
    reads++
    return { enabled: true, items: ids.map(id => limited(id, '1970-01-01T00:00:01Z')) }
  } })
  store.watch('1', '7d'); await time.advance(0); await time.advance(1000)
  assert.equal(reads, 2)
  assert.deepEqual(time.pending(), [])
  await time.advance(100000)
  assert.equal(reads, 2)
  focusTarget.dispatchEvent(new Event('focus')); await flush()
  assert.equal(reads, 3)
  assert.deepEqual(time.pending(), [])
})

test('После истечения бюджета disabled запрещает POST, error с датой не становится повторяемым', async () => {
  for (const status of ['disabled', 'error', 'running']) {
    let reads = 0
    let posts = 0
    const { store, time } = fixture({
      read: async ids => ({ enabled: status !== 'disabled', items: ids.map(id => {
        reads++
        return reads === 1 ? limited(id, '1970-01-01T00:00:01Z') : { ...item(id, status === 'disabled' ? 'missing' : status), retry_after: '1970-01-01T00:00:02Z' }
      }) }),
      start: async id => { posts++; return item(id, 'ready') },
    })
    store.watch('1', '7d'); await time.advance(0); await time.advance(1000); await time.advance(1000)
    assert.equal(posts, 0, status)
    assert.equal(store.getSnapshot('1').status, status)
  }
})

test('Server running, затем limit_reached с датой не разрешает новую платную попытку', async () => {
  let reads = 0
  const { store, time, calls } = fixture({ read: async ids => ({ enabled: true, items: ids.map(id => ++reads === 1 ? item(id, 'running') : limited(id, '1970-01-01T00:00:03Z')) }) })
  store.watch('1', '7d'); await time.advance(0); await time.advance(2000); await time.advance(10000)
  store.wake(); await flush()
  assert.equal(reads, 2)
  assert.equal(calls.length, 0)
  assert.deepEqual(time.pending(), [])
})

test('API сохраняет budget и retry_after как ISO UTC; неизвестные даты/бюджет не планируются', async () => {
  const original = globalThis.fetch
  let response = {
    enabled: true,
    budget: { ...liveBudget, reset_at: '2026-10-24T00:00:00+03:00' },
    items: [limited('1', '2026-10-24T00:00:00+03:00'), limited('2', '2026-10-24T00:00:00'), limited('3', 'не дата'), limited('4', '2026-02-30T00:00:00Z')],
  }
  globalThis.fetch = async () => ({ ok: true, json: async () => response })
  try {
    const data = await getCarouselCoverTranslations(['1', '2', '3', '4'])
    assert.deepEqual(data.budget, liveBudget)
    assert.equal(data.items[0].retry_after, liveBudget.reset_at)
    assert.equal(data.items[1].retry_after, null)
    assert.equal(data.items[2].retry_after, null)
    assert.equal(data.items[3].retry_after, null)
    for (const daily_limit of [500, 2500]) {
      response = { enabled: true, budget: { ...liveBudget, daily_limit }, items: [] }
      assert.equal((await getCarouselCoverTranslations(['1'])).budget.daily_limit, daily_limit)
    }
    for (const daily_limit of [501, 2501, true, false, '2500', 2500.5, null]) {
      response = { enabled: true, budget: { ...liveBudget, daily_limit }, items: [] }
      assert.equal((await getCarouselCoverTranslations(['1'])).budget, null)
    }
    response = { item: limited('1', '2026-10-24T00:00:00+03:00') }
    assert.equal((await translateCarouselCover('1', '7d')).retry_after, liveBudget.reset_at)
  } finally { globalThis.fetch = original }
})

test('Подписки focus/visibility общие для карточки и Drawer и снимаются после последнего watch', async () => {
  const counts = { added: 0, removed: 0 }
  const target = {
    addEventListener() { counts.added++ },
    removeEventListener() { counts.removed++ },
  }
  const { store, time } = fixture({ focusTarget: target, visibilityTarget: target })
  const releaseCard = store.watch('1', '7d')
  const releaseDrawer = store.watch('1', '7d')
  await time.advance(0)
  assert.deepEqual(counts, { added: 2, removed: 0 })
  releaseCard()
  assert.equal(counts.removed, 0)
  releaseDrawer()
  assert.deepEqual(counts, { added: 2, removed: 2 })
})
