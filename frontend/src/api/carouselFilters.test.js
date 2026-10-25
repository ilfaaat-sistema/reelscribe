import test from 'node:test'
import assert from 'node:assert/strict'
import { favoriteFilterEnabled, toggleFavoriteFilter } from './carouselFilters.js'

function toggle(params) {
  for (const [key, value] of Object.entries(toggleFavoriteFilter(params))) {
    if (value == null) params.delete(key)
    else params.set(key, String(value))
  }
}

test('14d: один избранный → все избранные → выключено → включено без смены периода и прочих фильтров', () => {
  const params = new URLSearchParams('period=14d&author=ilia.paliy&sort=comments&order=asc&source=neyro_set7&limit=100')
  assert.equal(favoriteFilterEnabled(params), true)
  toggle(params)
  assert.equal(params.has('author'), false)
  assert.equal(favoriteFilterEnabled(params), true)
  toggle(params)
  assert.equal(favoriteFilterEnabled(params), false)
  toggle(params)
  assert.equal(favoriteFilterEnabled(params), true)
  assert.equal(params.get('period'), '14d')
  assert.equal(params.get('sort'), 'comments')
  assert.equal(params.get('order'), 'asc')
  assert.equal(params.get('source'), 'neyro_set7')
  assert.equal(params.get('limit'), '100')
})

test('архив: toggle меняет ограничение избранными, не подменяет период', () => {
  const params = new URLSearchParams('period=archive14d')
  assert.equal(favoriteFilterEnabled(params), false)
  toggle(params)
  assert.equal(favoriteFilterEnabled(params), true)
  toggle(params)
  assert.equal(favoriteFilterEnabled(params), false)
  assert.equal(params.get('period'), 'archive14d')
})

test('30d по умолчанию включает избранное и toggle сохраняет месяц', () => {
  const params = new URLSearchParams('period=30d&favorite=1')
  toggle(params)
  assert.equal(favoriteFilterEnabled(params), false)
  assert.equal(params.get('period'), '30d')
  toggle(params)
  assert.equal(favoriteFilterEnabled(params), true)
})

test('четыре месяца для избранного по умолчанию сохраняют семантику старых карточек и архивов', async () => {
  const { carouselPeriod, isLiveCarouselPeriod } = await import('./carouselFilters.js')
  assert.equal(carouselPeriod(new URLSearchParams('favorite=1')), '4m')
  assert.equal(carouselPeriod(new URLSearchParams('favorite=1&id=old')), null)
  assert.equal(carouselPeriod(new URLSearchParams('favorite=1&period=7d')), '7d')
  assert.equal(isLiveCarouselPeriod('custom'), true)
  assert.equal(isLiveCarouselPeriod('4m'), true)
  assert.equal(isLiveCarouselPeriod('7d'), false)
})

test('диапазоны проверяют календарные даты, целые значения и обе границы', async () => {
  const { validateCarouselRanges } = await import('./carouselFilters.js')
  const draft = { date_from: '2026-10-25', date_to: '2026-10-25', min_likes: '0', max_likes: '100', min_comments: '', max_comments: '2' }
  assert.deepEqual(validateCarouselRanges(draft, true).values, { ...draft, min_likes: 0, max_likes: 100, min_comments: null, max_comments: 2 })
  for (const changes of [{ date_to: '' }, { date_to: '2026-02-30' }, { date_to: '2026-10-24' }, { min_likes: '-1' }, { max_comments: '1.2' }, { min_likes: '101' }, { min_likes: '9007199254740992' }]) {
    assert.ok(validateCarouselRanges({ ...draft, ...changes }, true).error)
  }
  assert.equal(validateCarouselRanges({ min_likes: '0' }).error, '')
})
