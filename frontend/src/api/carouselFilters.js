// Явное favorite=0 выключает ограничение, сохраняя прежнюю ссылку 14d включённой.
export function favoriteFilterEnabled(params) {
  return params.get('favorite') === '1' || (isLiveCarouselPeriod(params.get('period')) && params.get('favorite') !== '0')
}

export function toggleFavoriteFilter(params) {
  const enabled = favoriteFilterEnabled(params)
  return { favorite: enabled && !params.get('author') ? '0' : '1', author: null, id: null, offset: 0 }
}

export function isLiveCarouselPeriod(period) {
  return period === '14d' || period === '30d'
}
