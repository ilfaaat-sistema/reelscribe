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
