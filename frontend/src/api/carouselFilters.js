// Явное favorite=0 выключает ограничение, сохраняя прежнюю ссылку 14d включённой.
export function favoriteFilterEnabled(params) {
  return params.get('favorite') === '1' || (isLiveCarouselPeriod(params.get('period')) && params.get('favorite') !== '0')
}

export function toggleFavoriteFilter(params) {
  const enabled = favoriteFilterEnabled(params)
  return { favorite: enabled && !params.get('author') ? '0' : '1', author: null, id: null, offset: 0 }
}

export function isLiveCarouselPeriod(period) {
  return ['14d', '30d', '4m', 'custom'].includes(period)
}

export const CAROUSEL_RANGE_KEYS = ['date_from', 'date_to', 'min_likes', 'max_likes', 'min_comments', 'max_comments']

export function carouselPeriod(params) {
  const value = params.get('period')
  if (['7d', 'archive14d', '14d', '30d', '4m', 'custom'].includes(value)) return value
  // Старые прямые ссылки на карточку остаются привязаны к архиву.
  return params.get('favorite') === '1' && !params.get('id') ? '4m' : null
}

export function carouselRangeDraft(params) {
  return Object.fromEntries(CAROUSEL_RANGE_KEYS.map(key => [key, params.get(key) || '']))
}

export function validateCarouselRanges(draft, customDates = false) {
  const values = {}
  if (customDates) {
    for (const key of ['date_from', 'date_to']) {
      const value = draft[key] || ''
      const parsed = new Date(`${value}T12:00:00Z`)
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== value) {
        return { error: 'Укажите корректные даты с и по.' }
      }
      values[key] = value
    }
    if (values.date_from > values.date_to) return { error: 'Дата с должна быть не позже даты по.' }
  }
  for (const key of ['min_likes', 'max_likes', 'min_comments', 'max_comments']) {
    const value = String(draft[key] ?? '').trim()
    if (value && (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)))) return { error: 'Лайки и комментарии должны быть целыми числами от нуля.' }
    values[key] = value === '' ? null : Number(value)
  }
  for (const name of ['likes', 'comments']) {
    if (values[`min_${name}`] != null && values[`max_${name}`] != null && values[`min_${name}`] > values[`max_${name}`]) return { error: 'Значение от должно быть не больше значения до.' }
  }
  return { values, error: '' }
}
