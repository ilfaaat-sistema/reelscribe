import test from 'node:test'
import assert from 'node:assert/strict'
import { fetchSelectedCarousels, setCarouselSelected } from './selectedCarousels.js'

const item = (id = '17924867421853442602806001') => ({
  id, source_period: '14d', selected_at: '2026-10-20T12:00:00Z',
  snapshot: { id, media_type: 'CAROUSEL_ALBUM', username: 'alice', caption: 'Сохранённый текст',
    shortcode: 'abc', permalink: 'https://www.instagram.com/p/abc/', likes: 12 },
})
const ok = data => ({ ok: true, json: async () => data })

test('общий список приходит с сервера целиком, с длинными строковыми ID и без кэша', async () => {
  const list = Array.from({ length: 41 }, (_, i) => item(String(i + 1)))
  list.push(item())
  const data = await fetchSelectedCarousels(undefined, async (url, options) => {
    assert.equal(url, '/api/radar/carousel-likes')
    assert.equal(options.cache, 'no-store')
    return ok({ items: list, total: list.length })
  })
  assert.equal(data.items.length, 42)
  assert.equal(data.items.at(-1).id, item().id)
  assert.equal(data.items[0].snapshot.caption, 'Сохранённый текст')
})

test('PUT передаёт только период и принимает подтверждённый сервером снимок', async () => {
  const saved = item()
  const result = await setCarouselSelected(saved.id, '14d', true, async (url, options) => {
    assert.equal(url, `/api/radar/carousel-likes/${saved.id}`)
    assert.equal(options.method, 'PUT')
    assert.equal(options.cache, 'no-store')
    assert.deepEqual(JSON.parse(options.body), { period: '14d' })
    return ok({ item: saved })
  })
  assert.equal(result.id, saved.id)
})

test('DELETE явно снимает отметку без тела и требует подтверждения того же ID', async () => {
  assert.equal(await setCarouselSelected('123', '7d', false, async (_, options) => {
    assert.equal(options.method, 'DELETE')
    assert.equal(options.body, undefined)
    return ok({ id: '123', selected: false })
  }), null)
  await assert.rejects(setCarouselSelected('123', '7d', false, async () => ok({ id: '456', selected: false })), /не подтвердил/)
})

test('ошибки сети, статуса и некорректные данные отклоняются', async () => {
  await assert.rejects(fetchSelectedCarousels(undefined, async () => { throw new Error('offline') }), /Нет соединения/)
  await assert.rejects(setCarouselSelected('123', '7d', true, async () => ({ ok: false, status: 503 })), /503/)
  await assert.rejects(fetchSelectedCarousels(undefined, async () => ok({ items: [item('bad')] })), /некорректную/)
  const unsafe = item('123')
  unsafe.snapshot.permalink = 'javascript:alert(1)'
  await assert.rejects(setCarouselSelected('123', '7d', true, async () => ok({ item: unsafe })), /не подтвердил/)
})

test('некорректный ID, период и неявное состояние не отправляются на сервер', async () => {
  const fetcher = () => { assert.fail('Запрос не должен выполняться') }
  await assert.rejects(setCarouselSelected('x', '7d', true, fetcher), /ID/)
  await assert.rejects(setCarouselSelected('123', '60d', true, fetcher), /период/)
  await assert.rejects(setCarouselSelected('123', '7d', undefined, fetcher), /состояние/)
})

test('архивный период сохраняется и читается в общей подборке', async () => {
  const archived = { ...item('123'), source_period: 'archive14d' }
  const result = await setCarouselSelected('123', 'archive14d', true, async (_, options) => {
    assert.deepEqual(JSON.parse(options.body), { period: 'archive14d' })
    return ok({ item: archived })
  })
  assert.equal(result.source_period, 'archive14d')
  const data = await fetchSelectedCarousels(undefined, async () => ok({ items: [archived], total: 1 }))
  assert.equal(data.items[0].source_period, 'archive14d')
})

test('30d запрос сохраняет исходный snapshot через совместимый live bucket', async () => {
  const saved = item('123')
  saved.source_period = '14d'
  saved.snapshot.timestamp = '2026-09-25T12:00:00+00:00'
  saved.snapshot.caption = 'Месячная карточка'
  const result = await setCarouselSelected('123', '30d', true, async (_, options) => {
    assert.deepEqual(JSON.parse(options.body), { period: '30d' })
    return ok({ item: saved })
  })
  assert.equal(result.snapshot.timestamp, saved.snapshot.timestamp)
  assert.equal(result.snapshot.caption, saved.snapshot.caption)
})

test('4m и custom сохраняют отбор через совместимый live snapshot', async () => {
  for (const period of ['4m', 'custom']) {
    const result = await setCarouselSelected('123', period, true, async (_, options) => {
      assert.deepEqual(JSON.parse(options.body), { period })
      return ok({ item: item('123') })
    })
    assert.equal(result.source_period, '14d')
  }
})

test('заметки старых записей пусты, новый формат сохраняет текст и версию', async () => {
  const old = await fetchSelectedCarousels(undefined, async () => ok({ items: [item()], total: 1 }))
  assert.equal(old.items[0].note, '')
  const saved = { ...item(), note: 'Идея\nДля курса 🎯', note_revision: 2, note_updated_at: '2026-10-26T12:00:00Z' }
  const result = await fetchSelectedCarousels(undefined, async () => ok({ items: [saved], total: 1 }))
  assert.equal(result.items[0].note, saved.note)
  assert.equal(result.items[0].note_revision, 2)
})

test('PATCH заметки передаёт ожидаемую версию и отклоняет чужой ID', async () => {
  const { saveCarouselNote } = await import('./selectedCarousels.js')
  const body = { id: '123', note: '😀'.repeat(3000), expected_revision: 2 }
  const result = await saveCarouselNote(body, async (url, opts) => {
    assert.equal(url, '/api/radar/carousel-likes/123/note')
    assert.equal(opts.method, 'PATCH')
    assert.deepEqual(JSON.parse(opts.body), { note: body.note, expected_revision: 2 })
    return ok({ id: '123', note: body.note, note_revision: 3, note_updated_at: '2026-10-26T12:00:00Z' })
  })
  assert.equal(result.note_revision, 3)
  await assert.rejects(saveCarouselNote({ ...body, note: 'a'.repeat(3001) }, () => assert.fail('Нет запроса')), /3000/)
  await assert.rejects(saveCarouselNote(body, async () => ok({ id: '456', note: '', note_revision: 0 })), /другую/)
})

test('конфликт заметки сохраняет актуальный вариант и не повторяет PATCH автоматически', async () => {
  const { saveCarouselNote } = await import('./selectedCarousels.js')
  let calls = 0
  await assert.rejects(saveCarouselNote({ id: '123', note: 'мой', expected_revision: 0 }, async () => {
    calls++
    return { ok: false, status: 409, json: async () => ({ detail: { code: 'note_conflict', item: { id: '123', note: 'другой', note_revision: 1 } } }) }
  }), error => error.code === 'note_conflict' && error.item.note === 'другой')
  assert.equal(calls, 1)
})
