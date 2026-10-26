import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import useSelectedCarousels from '../hooks/useSelectedCarousels'
import CarouselCard from '../components/CarouselCard'
import CarouselDrawer from '../components/CarouselDrawer'
import CarouselCoverTranslation from '../components/CarouselCoverTranslation'
import SelectedCarouselCaption from '../components/SelectedCarouselCaption'
import './carousels.css'
import './selected-carousels.css'

const number = value => value == null || !Number.isFinite(Number(value)) ? '—' : new Intl.NumberFormat('ru-RU').format(Number(value))
const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' }) : '—'
const safeReturn = value => {
  try {
    const url = new URL(value, window.location.origin)
    return url.origin === window.location.origin && url.pathname === '/radar/carousels' ? url.pathname + url.search : '/radar/carousels?period=14d'
  } catch { return '/radar/carousels?period=14d' }
}
function sortValue(item, key) {
  const value = key === 'selected_at' ? item.selected_at : item.snapshot[key]
  if (value == null) return null
  if (key === 'selected_at' || key === 'timestamp') {
    const parsed = Date.parse(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export default function SelectedCarousels() {
  const pageRef = useRef(null)
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const { items, loading, error, busy, refresh, setSelected } = useSelectedCarousels()
  const [undo, setUndo] = useState(null)
  const [drawer, setDrawer] = useState(null)
  const closeDrawer = useCallback(() => setDrawer(null), [])
  const [shown, setShown] = useState(50)
  const q = (params.get('q') || '').slice(0, 200)
  const sort = ['selected_at', 'timestamp', 'likes', 'comments'].includes(params.get('sort')) ? params.get('sort') : 'selected_at'
  const order = params.get('order') === 'asc' ? 'asc' : 'desc'

  useEffect(() => { setShown(50) }, [q, sort, order])

  const change = (key, value) => setParams(previous => {
    const next = new URLSearchParams(previous)
    if (value) next.set(key, value)
    else next.delete(key)
    return next
  }, { replace: true })
  const toggle = async item => {
    const selected = items.some(entry => entry.id === item.id)
    if (await setSelected(item.snapshot, item.source_period, !selected)) setUndo(selected ? item : null)
  }
  const restore = async () => {
    if (undo && await setSelected(undo.snapshot, undo.source_period, true)) setUndo(null)
  }

  const results = useMemo(() => {
    const needle = q.trim().replace(/^@/, '').toLocaleLowerCase('ru-RU')
    const filtered = items.filter(item => !needle || String(item.snapshot.username || '').toLocaleLowerCase('ru-RU').includes(needle) || String(item.snapshot.caption || '').toLocaleLowerCase('ru-RU').includes(needle))
    filtered.sort((a, b) => {
      const x = sortValue(a, sort); const y = sortValue(b, sort)
      if (x == null || y == null) return x == null && y == null ? a.id.localeCompare(b.id) : x == null ? 1 : -1
      return x === y ? a.id.localeCompare(b.id) : (x < y ? -1 : 1) * (order === 'asc' ? 1 : -1)
    })
    return filtered
  }, [items, q, sort, order])
  const visible = results.slice(0, shown)
  const back = safeReturn(params.get('returnTo'))
  const selectedInDrawer = drawer && items.some(item => item.id === drawer.id)

  return <main ref={pageRef} className="selected-page">
    <Link className="selected-back" to={back} state={{ carouselScrollTop: location.state?.carouselScrollTop }}>← Все карусели</Link>
    <div className="selected-heading"><span className="selected-eyebrow">Подборка</span><h1>Отобранные карусели <span>({number(items.length)})</span></h1><p className="selected-muted">Ваши отметки доступны с телефона и ноутбука.</p></div>
    <div className="selected-filters">
      <label>Поиск по автору и тексту<input type="search" maxLength={200} value={q} onChange={event => change('q', event.target.value)} placeholder="Автор или фраза из описания" /></label>
      <label>Сортировка<select value={sort} onChange={event => change('sort', event.target.value)}><option value="selected_at">Дата отбора</option><option value="timestamp">Дата публикации</option><option value="likes">Лайки Instagram</option><option value="comments">Комментарии</option></select></label>
      <label>Порядок<select value={order} onChange={event => change('order', event.target.value)}><option value="desc">По убыванию</option><option value="asc">По возрастанию</option></select></label>
    </div>
    {error && <p className="selected-error" role="alert">{error} <button type="button" onClick={refresh}>Повторить</button></p>}
    {loading && <p className="selected-muted" role="status">Загрузка подборки…</p>}
    {undo && <p className="selected-notice" role="status">Отметка снята. <button type="button" disabled={busy.has(undo.id)} onClick={restore}>Отменить</button></p>}
    <p className="selected-muted" aria-live="polite">Показано {visible.length} / найдено {results.length} · Всего отобрано {items.length}</p>
    {!loading && !error && !results.length && <div className="selected-empty"><h2>{q ? 'Совпадений нет' : 'Пока нет отобранных каруселей'}</h2><p>{q ? 'Попробуйте другой запрос.' : 'Нажмите ♥ на отдельной карусели в общей сетке.'}</p>{q && <button type="button" onClick={() => change('q', '')}>Сбросить поиск</button>}</div>}
    <div className="selected-grid carousels-grid">{visible.map(item => <article className="carousels-tile" key={item.id}>
      <CarouselCard item={item.snapshot} onOpen={() => setDrawer(item)}
        coverContent={<><CarouselCoverTranslation id={item.id} period={item.source_period} rootRef={pageRef} /><SelectedCarouselCaption item={item} rootRef={pageRef} /></>}>
        <small className="selected-at">Отобрано: {date(item.selected_at)}</small>
      </CarouselCard>
      <button type="button" className="carousels-select is-selected selected-remove" disabled={busy.has(item.id)} aria-pressed="true"
        aria-label={`Убрать карусель @${item.snapshot.username} из отобранных`} title="Убрать из отобранных" onClick={() => toggle(item)}>♥</button>
    </article>)}</div>
    {visible.length < results.length && <button type="button" className="selected-more" onClick={() => setShown(value => value + 50)}>Показать ещё 50</button>}
    {drawer && <CarouselDrawer key={drawer.id} id={drawer.id} period={drawer.source_period} listItem={drawer.snapshot} selectedAt={drawer.selected_at}
      selected={!!selectedInDrawer} selectionBusy={busy.has(drawer.id)} onToggleSelection={() => toggle(drawer)} onClose={closeDrawer} />}
  </main>
}
