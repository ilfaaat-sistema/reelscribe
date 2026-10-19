// Сохраняем глобальный порядок и убираем дубли при изменении отчёта между порциями.
export function mergeCarouselItems(previous, incoming, sort = 'likes', order = 'desc') {
  const rows = new Map(previous.map(item => [String(item.id), item]))
  incoming.forEach(item => rows.set(String(item.id), item))
  return [...rows.values()].sort((a, b) => {
    const av = a[sort], bv = b[sort]
    if (av == null || bv == null) {
      if (av == null && bv != null) return 1
      if (av != null && bv == null) return -1
    } else if (av !== bv) return (av < bv ? -1 : 1) * (order === 'asc' ? 1 : -1)
    return String(a.id) < String(b.id) ? -1 : String(a.id) > String(b.id) ? 1 : 0
  })
}

export function sameCarouselSnapshot(first, next) {
  return first.collected_at === next.collected_at && first.total === next.total
}

export async function readCarouselPrefix(fetchPage, query, wanted, signal) {
  // Повтор ограничен: обновляющаяся коллекция не должна зациклить чтение.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const first = await fetchPage({ ...query, offset: 0 }, signal)
    let report = first
    let items = first.items
    let cursor = items.length
    let changed = false
    if (!cursor && report.total > 0) throw new Error('Сервер вернул пустую порцию. Обновите список.')
    while (cursor < Math.min(wanted, report.total)) {
      const next = await fetchPage({ ...query, offset: cursor }, signal)
      if (!sameCarouselSnapshot(first, next)) { changed = true; break }
      if (!next.items.length && cursor < next.total) throw new Error('Сервер вернул пустую порцию. Обновите список.')
      cursor += next.items.length
      items = mergeCarouselItems(items, next.items, query.sort, query.order)
      if (items.length !== cursor) throw new Error('Сервер повторил карточки. Обновите список.')
      report = next
      if (!next.items.length) break
    }
    if (!changed) return { report: { ...report, items }, cursor }
  }
  throw new Error('Коллекция обновляется. Повторите обновление через несколько секунд.')
}
