import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../auth/useAuth'
import { getCarouselSelections, getCarouselSelectionStatus, getCarouselAvailability, putCarouselSelection, deleteCarouselSelection } from '../api/carouselSelections'
import { readHiddenAuthors } from '../api/hiddenAuthors'
import './selected-carousels.css'

const formatNumber = value => value == null ? '—' : new Intl.NumberFormat('ru-RU').format(value)
const formatDate = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ru-RU') : '—'
const safeReturn = value => {
  try {
    const url = new URL(value, window.location.origin)
    return url.origin === window.location.origin && url.pathname === '/radar/carousels' ? url.pathname + url.search : '/radar/carousels?period=14d'
  } catch { return '/radar/carousels?period=14d' }
}
function instagram(item) {
  if (/^[\w-]+$/.test(item?.shortcode || '')) return `https://www.instagram.com/p/${item.shortcode}/`
  try {
    const url = new URL(item?.permalink)
    if (url.protocol === 'https:' && ['instagram.com', 'www.instagram.com'].includes(url.hostname)) return url.href
  } catch { /* Сохранённая ссылка может отсутствовать. */ }
  return null
}
function Cover({ snapshot }) {
  const [failed, setFailed] = useState(false)
  const source = snapshot.thumbnail_url || snapshot.media_url
  return <div className="selected-cover">{!failed && /^https:\/\//.test(source || '') ? <img src={source} alt="" loading="lazy" onError={() => setFailed(true)} /> : <span>▧<br />Обложка недоступна</span>}</div>
}
function SelectionDrawer({ item, pending, onToggle, onClose, hidden, availability }) {
  const dialog = useRef(null)
  const [embedFailed, setEmbedFailed] = useState(false)
  const snapshot = item.snapshot || {}
  const link = instagram(snapshot)
  const shortcode = /^[\w-]+$/.test(snapshot.shortcode || '') ? snapshot.shortcode : link?.match(/\/p\/([\w-]+)/)?.[1]
  useEffect(() => {
    const previous = document.activeElement
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialog.current?.querySelector('button')?.focus()
    const keydown = event => {
      if (event.key === 'Escape') onClose()
      if (event.key !== 'Tab') return
      const nodes = [...dialog.current.querySelectorAll('button:not(:disabled),a[href],iframe')]
      const first = nodes[0]; const last = nodes[nodes.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', keydown)
    return () => { document.body.style.overflow = previousOverflow; document.removeEventListener('keydown', keydown); previous?.focus() }
  }, [onClose])
  return <div className="selected-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}><aside ref={dialog} className="selected-drawer" role="dialog" aria-modal="true" aria-labelledby="selected-dialog-title">
    <button className="selected-close" onClick={onClose} aria-label="Закрыть просмотр">✕</button>
    <h2 id="selected-dialog-title">@{snapshot.username || 'Автор неизвестен'}</h2>
    {hidden && <p className="selected-muted">Этот автор скрыт в общей сетке. Отобранная публикация остаётся доступна.</p>}
    {availability === false && <p className="selected-muted">Нет в текущем отчёте; показана сохранённая копия.</p>}
    {availability === null && <p className="selected-muted">Не удалось проверить актуальность отчёта. Сохранённая копия доступна.</p>}
    <button disabled={pending} aria-pressed={item.selected !== false} onClick={() => onToggle(item)}>{pending ? 'Сохраняем…' : item.selected === false ? 'Отобрать снова' : 'Снять отбор'}</button>
    <p className="selected-muted">Опубликовано: {formatDate(snapshot.timestamp)} · Отобрано: {formatDate(item.selected_at)}</p>
    <p>♡ {formatNumber(snapshot.likes)} · Комментарии: {formatNumber(snapshot.comments)}</p>
    <p className="selected-muted">Метрики сохранены {formatDate(item.snapshot_at)}</p>
    {shortcode && !embedFailed ? <iframe title={`Карусель @${snapshot.username || ''} в Instagram`} src={`https://www.instagram.com/p/${shortcode}/embed/`} onError={() => setEmbedFailed(true)} allow="autoplay; encrypted-media; picture-in-picture" allowFullScreen /> : <Cover snapshot={snapshot} />}
    <p className="selected-muted">Если Instagram не показывает слайды, откройте оригинал. Сохранённый текст доступен ниже.</p>
    {link && <a href={link} target="_blank" rel="noopener noreferrer">Открыть в Instagram ↗</a>}
    <h3>Сохранённый текст</h3><p className="selected-caption">{snapshot.caption || 'Описание отсутствует.'}</p>
  </aside></div>
}

export default function SelectedCarousels() {
  const { user, token, loading: authLoading, signOut } = useAuth()
  const navigate = useNavigate(); const location = useLocation()
  const [params, setParams] = useSearchParams()
  const q = (params.get('q') || '').slice(0, 200)
  const sort = ['selected_at', 'timestamp', 'likes', 'comments'].includes(params.get('sort')) ? params.get('sort') : 'selected_at'
  const order = params.get('order') === 'asc' ? 'asc' : 'desc'
  const [data, setData] = useState({ owner: null, items: [], total: null, selected_total: null })
  const [availability, setAvailability] = useState({ owner: null, states: {} })
  const [busy, setBusy] = useState(false); const [error, setError] = useState('')
  const [drawer, setDrawer] = useState(null); const [undo, setUndo] = useState(null); const [pending, setPending] = useState(null)
  const inFlight = useRef(false); const requestRef = useRef(null); const generation = useRef(0); const mutationRef = useRef(false)
  const owner = user?.id
  const current = useRef({ owner, token }); current.current = { owner, token }
  const items = data.owner === owner ? data.items : []
  const hidden = new Set(readHiddenAuthors().authors.map(author => author.username))
  const signIn = useCallback(() => navigate(`/sign-in?returnTo=${encodeURIComponent(location.pathname + location.search)}`, { replace: true }), [navigate, location.pathname, location.search])
  const load = useCallback(async (append = false, offset = 0, limit = 50) => {
    if (!owner || !token) return
    requestRef.current?.abort()
    const controller = new AbortController(); requestRef.current = controller
    const version = ++generation.current
    setBusy(true); inFlight.current = true
    try {
      const result = await getCarouselSelections({ q, sort, order, offset, limit }, token, controller.signal)
      if (version !== generation.current || current.current.owner !== owner) return
      setData(previous => ({ ...result, owner, items: append ? [...previous.items, ...result.items.filter(item => !previous.items.some(old => old.id === item.id))] : result.items }))
      setError('')
      if (result.items.length) {
        const report = await getCarouselAvailability(result.items.map(item => item.id), '14d', token, controller.signal).catch(() => ({ status: 'unknown' }))
        if (version === generation.current && current.current.owner === owner) {
          const states = report.status === 'known'
            ? Object.fromEntries(report.states.map(state => [state.id, state.in_report]))
            : Object.fromEntries(result.items.map(item => [item.id, null]))
          setAvailability(previous => ({ owner, states: append && previous.owner === owner ? { ...previous.states, ...states } : states }))
        }
      }
    } catch (failure) {
      if (controller.signal.aborted || current.current.owner !== owner) return
      if (failure.status === 401) { signOut(); signIn() }
      else setError(failure.message || 'Не удалось загрузить подборку. Сохранённые данные не изменены.')
    } finally { if (version === generation.current) { setBusy(false); inFlight.current = false } }
  }, [owner, token, q, sort, order, signIn, signOut])
  useEffect(() => { setDrawer(null); setUndo(null); setError(''); setAvailability({ owner, states: {} }) }, [owner])
  useEffect(() => {
    if (!authLoading && (!owner || !token)) signIn()
    if (!authLoading && owner && token) load()
    return () => { generation.current += 1; requestRef.current?.abort() }
  }, [owner, token, authLoading, load, signIn])
  useEffect(() => {
    const refresh = () => { if (document.visibilityState === 'visible' && !inFlight.current && !mutationRef.current) load() }
    const interval = setInterval(refresh, 30000)
    window.addEventListener('focus', refresh)
    return () => { clearInterval(interval); window.removeEventListener('focus', refresh) }
  }, [load])
  const closeDrawer = useCallback(() => setDrawer(null), [])
  const toggle = async item => {
    if (mutationRef.current) return
    mutationRef.current = true; setPending(item.id); setError('')
    const operationOwner = owner
    generation.current += 1; requestRef.current?.abort(); inFlight.current = false
    try {
      const removing = item.selected !== false
      const result = removing ? await deleteCarouselSelection(item.id, item.revision, token) : await putCarouselSelection(item.id, { source_period: item.source_period || '14d', expected_revision: item.revision }, token)
      if (current.current.owner !== operationOwner) return
      const updated = removing ? { ...item, selected: false, revision: result.revision } : result.item
      setDrawer(previous => previous?.owner === owner && previous.item.id === item.id ? { owner, item: updated } : previous)
      setUndo(removing ? updated : null)
      setData(previous => previous.owner !== owner ? previous : { ...previous, selected_total: result.selected_total, items: removing ? previous.items.filter(entry => entry.id !== item.id) : previous.items, total: removing ? Math.max(0, previous.total - 1) : previous.total })
      await load()
    } catch (failure) {
      if (current.current.owner !== operationOwner) return
      if (failure.status === 401) { signOut(); signIn() }
      else {
        if (failure.status === 409) {
          const fresh = await getCarouselSelectionStatus([item.id], token).catch(() => null)
          if (current.current.owner !== operationOwner || current.current.token !== token) return
          const state = fresh?.states?.[0] || failure.current
          if (state?.revision != null) {
            const updated = state.snapshot ? state : { ...item, ...state }
            setDrawer(previous => previous?.item.id === item.id ? { owner, item: updated } : previous)
            setUndo(previous => previous?.id === item.id ? updated : previous)
          }
          await load()
        }
        setError(failure.message || 'Не удалось сохранить изменение. Повторите попытку.')
      }
    } finally { mutationRef.current = false; setPending(null) }
  }
  const change = (key, value) => setParams(previous => { const next = new URLSearchParams(previous); if (value) next.set(key, value); else next.delete(key); return next }, { replace: true })
  return <main className="selected-page">
    <Link className="selected-back" to={safeReturn(params.get('returnTo'))} state={{ carouselScrollTop: location.state?.carouselScrollTop }}>← Все карусели</Link>
    <header><span className="selected-eyebrow">Личная подборка</span><h1>Отобранные карусели <span>{data.owner === owner && data.selected_total != null ? `(${formatNumber(data.selected_total)})` : '…'}</span></h1><p className="selected-muted">Сохранённые публикации из всех периодов для будущей адаптации.</p>{user && <button type="button" onClick={signOut}>Выйти из подборки</button>}</header>
    <div className="selected-filters"><label>Поиск по автору и тексту<input type="search" maxLength={200} value={q} onChange={event => change('q', event.target.value)} placeholder="Автор или фраза из описания" /></label><label>Сортировка<select value={sort} onChange={event => change('sort', event.target.value)}><option value="selected_at">Дата отбора</option><option value="timestamp">Дата публикации</option><option value="likes">Лайки</option><option value="comments">Комментарии</option></select></label><label>Направление<select value={order} onChange={event => change('order', event.target.value)}><option value="desc">По убыванию</option><option value="asc">По возрастанию</option></select></label></div>
    {error && <div className="selected-error" role="alert">{error} <button onClick={() => load()} disabled={busy}>Повторить загрузку</button></div>}
    {undo && <div className="selected-notice" role="status">Отбор снят. <button disabled={pending != null} onClick={() => toggle(undo)}>Отменить</button></div>}
    {(busy || authLoading) && <p role="status">Загружаем подборку…</p>}
    {data.owner === owner && data.total != null && <p className="selected-muted" aria-live="polite">Показано {items.length} / найдено {formatNumber(data.total)} · Всего отобрано {formatNumber(data.selected_total)}</p>}
    {!busy && !error && data.owner === owner && data.total === 0 && <div className="selected-empty"><h2>{q ? 'Совпадений нет' : 'Пока нет отобранных каруселей'}</h2><p>{q ? 'Попробуйте другого автора или фразу.' : 'Отбирайте отдельные публикации в общей сетке — они появятся здесь.'}</p>{q && <button onClick={() => change('q', '')}>Сбросить поиск</button>}</div>}
    <div className="selected-grid">{items.map(item => <article key={item.id} className="selected-card"><button className="selected-open" onClick={() => setDrawer({ owner, item })} aria-label={`Открыть карусель @${item.snapshot?.username || 'неизвестного автора'}`}><Cover snapshot={item.snapshot || {}} /><div className="selected-card-copy"><strong>@{item.snapshot?.username || 'Автор неизвестен'}</strong><p>{item.snapshot?.caption || 'Без описания'}</p><span>♡ {formatNumber(item.snapshot?.likes)} · Комментарии: {formatNumber(item.snapshot?.comments)}</span><small>Снимок: {formatDate(item.snapshot_at)}</small>{availability.owner === owner && availability.states[item.id] === false && <small>Нет в текущем отчёте · сохранённая копия</small>}{availability.owner === owner && availability.states[item.id] === null && <small>Актуальность отчёта не проверена</small>}</div></button><button className="selected-remove" disabled={pending != null} aria-pressed="true" onClick={() => toggle(item)}>{pending === item.id ? 'Сохраняем…' : '▣ Снять отбор'}</button></article>)}</div>
    {items.length < (data.owner === owner ? data.total : 0) && <button className="selected-more" disabled={busy || pending != null} onClick={() => load(true, items.length)}>Показать ещё 50</button>}
    {drawer?.owner === owner && <SelectionDrawer key={drawer.item.id} item={drawer.item} pending={pending != null} onToggle={toggle} onClose={closeDrawer} hidden={hidden.has(drawer.item.snapshot?.username?.toLowerCase())} availability={availability.owner === owner ? availability.states[drawer.item.id] : undefined} />}
  </main>
}
