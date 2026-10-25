import test from 'node:test'
import assert from 'node:assert/strict'
import { carouselRefreshInterval, carouselFeedBaseKey, mergeCarouselItems, readCarouselPrefix } from './carouselFeed.js'

const query = { author: 'alice', sort: 'likes', order: 'desc', limit: 50, exclude_authors: ['bob'] }

test('Пересечение порций не создаёт дублей, обновлённые метрики сохраняют порядок', () => {
  const previous = [{ id: '1', likes: 10 }, { id: '2', likes: null }]
  const next = [{ id: '1', likes: 100 }, { id: '3', likes: 30 }]
  assert.deepEqual(mergeCarouselItems(previous, next).map(r => [r.id, r.likes]), [['1', 100], ['3', 30], ['2', null]])
  assert.deepEqual(mergeCarouselItems(previous, next, 'likes', 'asc').map(r => r.id), ['3', '1', '2'])
  assert.equal(previous[0].likes, 10)
})

test('Фоновое обновление перечитывает весь загруженный префикс с теми же фильтрами', async () => {
  const calls = []
  const rows = Array.from({ length: 123 }, (_, i) => ({ id: String(i), likes: 123 - i }))
  const fetchPage = async (q, signal) => {
    calls.push(q)
    assert.deepEqual(q.exclude_authors, ['bob'])
    assert.equal(q.author, 'alice')
    assert.ok(signal)
    return { total: 123, items: rows.slice(q.offset, q.offset + q.limit), collection: { status: 'finished' } }
  }
  const result = await readCarouselPrefix(fetchPage, query, 150, new AbortController().signal)
  assert.deepEqual(calls.map(q => q.offset), [0, 50, 100])
  assert.equal(result.cursor, 123)
  assert.equal(result.report.items.length, 123)
  assert.equal(result.report.collection.status, 'finished')
})

test('Пустая неожиданная порция не зацикливает загрузку', async () => {
  let calls = 0
  const fetchPage = async () => ({ total: 10, items: (++calls, []) })
  await assert.rejects(readCarouselPrefix(fetchPage, query, 50), /пустую порцию/)
  assert.equal(calls, 1)
})

test('Новая карточка между страницами вызывает перечитывание, а не пропуск в finished', async () => {
  const rows = Array.from({ length: 100 }, (_, i) => ({ id: String(i), likes: 100 - i }))
  const updated = [{ id: 'new', likes: 1000 }, ...rows]
  let calls = 0
  const fetchPage = async q => {
    const old = calls++ === 0
    const data = old ? rows : updated
    return { total: data.length, collected_at: old ? 'old' : 'new', collection: { status: 'finished' }, items: data.slice(q.offset, q.offset + q.limit) }
  }
  const result = await readCarouselPrefix(fetchPage, query, 150)
  assert.equal(result.report.items.length, 101)
  assert.equal(result.cursor, 101)
  assert.equal(result.report.items[0].id, 'new')
  assert.equal(result.report.collected_at, 'new')
  assert.equal(calls, 5)
})

test('Непрерывная смена снимка ограничена тремя попытками', async () => {
  let calls = 0
  const fetchPage = async q => ({ total: 100, collected_at: String(++calls), items: Array.from({ length: 50 }, (_, i) => ({ id: String(q.offset + i), likes: 0 })) })
  await assert.rejects(readCarouselPrefix(fetchPage, query, 100), /Коллекция обновляется/)
  assert.equal(calls, 6)
})


test('on/off избранного меняет ключ сброса загруженного префикса', () => {
  const on = { ...query, period: '14d', favorites_only: true }
  const off = { ...on, favorites_only: false }
  assert.notEqual(carouselFeedBaseKey(on), carouselFeedBaseKey(off))
  assert.notEqual(carouselFeedBaseKey(on), carouselFeedBaseKey({ ...on, exclude_authors: ['hidden'] }))
})

test('живые 14d/30d продолжают обновляться после finished, архив не опрашивается', async () => {
  const { carouselRefreshInterval } = await import('./carouselFeed.js')
  assert.equal(carouselRefreshInterval('30d', 'finished'), 60000)
  assert.equal(carouselRefreshInterval('14d', 'finished'), 60000)
  assert.equal(carouselRefreshInterval('30d', 'running'), 30000)
  assert.equal(carouselRefreshInterval('archive14d', 'finished'), null)
})

test('каждый диапазон и скрытые авторы сбрасывают ключ накопленной ленты', () => {
  const query = { period: 'custom', sort: 'likes', order: 'desc', limit: 50 }
  for (const key of ['date_from', 'date_to', 'min_likes', 'max_likes', 'min_comments', 'max_comments', 'exclude_authors']) {
    assert.notEqual(carouselFeedBaseKey(query), carouselFeedBaseKey({ ...query, [key]: key === 'exclude_authors' ? ['hidden'] : 1 }))
  }
  assert.equal(carouselRefreshInterval('4m', 'finished'), 60000)
  assert.equal(carouselRefreshInterval('custom', 'finished'), 60000)
})
