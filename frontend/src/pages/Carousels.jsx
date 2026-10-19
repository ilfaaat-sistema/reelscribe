import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { thumbUrl } from '../api/client'
import { getCarousel, getCarousels } from '../api/carousels'
import { HIDDEN_AUTHORS_KEY, readHiddenAuthors, writeHiddenAuthors, hideAuthor, restoreAuthor } from '../api/hiddenAuthors'
import './carousels.css'

const numberFormat = new Intl.NumberFormat('ru-RU')
const dateFormat = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'short', year: 'numeric',
  hour: '2-digit', minute: '2-digit',
})
const SORTS = new Set(['timestamp', 'likes', 'comments'])
const STATUS_LABELS = {
  running: 'Сбор продолжается', cooldown: 'Ожидание лимита Meta', finished: 'Обход авторов завершён',
  failed: 'Сбор прерван', authentication_required: 'Нужна проверка авторизации Meta',
  permissions_or_request_error: 'Нужна проверка доступа Meta',
}
const ACTIVE_STATUSES = new Set(['running', 'cooldown'])

function count(value) {
  return value == null || !Number.isFinite(Number(value)) ? '—' : numberFormat.format(Number(value))
}

function date(value) {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? '—' : dateFormat.format(parsed)
}

function instagramUrl(item) {
  if (/^[\w-]+$/.test(item?.shortcode || '')) {
    return `https://www.instagram.com/p/${item.shortcode}/`
  }
  try {
    const url = new URL(item?.permalink)
    if (url.protocol === 'https:' && ['instagram.com', 'www.instagram.com'].includes(url.hostname)) return url.href
  } catch { /* Ссылка может отсутствовать. */ }
  return null
}

function safeAuthorsReturn(value) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return null
  try {
    const url = new URL(value, window.location.origin)
    return url.origin === window.location.origin && url.pathname === '/radar'
      ? url.pathname + url.search
      : null
  } catch { return null }
}

function Cover({ item, large = false }) {
  const direct = item?.media_url?.startsWith('https://') ? item.media_url : null
  const fallback = item?.shortcode ? thumbUrl(item.shortcode, 'm') : null
  const [source, setSource] = useState(direct || fallback)

  useEffect(() => { setSource(direct || fallback) }, [direct, fallback])

  return (
    <span className={`carousels-cover${large ? ' carousels-cover-large' : ''}`}>
      {source ? (
        <img
          src={source}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setSource(current => current === direct && fallback && fallback !== direct ? fallback : null)}
        />
      ) : <span className="carousels-placeholder">Нет обложки</span>}
      {!large && <span className="carousels-album" aria-hidden="true">▣</span>}
    </span>
  )
}

function Metric({ label, value }) {
  return <span className="carousels-metric"><b>{count(value)}</b><small>{label}</small></span>
}

function Drawer({ id, listItem, onClose, excluded }) {
  const [item, setItem] = useState(listItem || null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const closeRef = useRef(null)

  useEffect(() => {
    const controller = new AbortController()
    getCarousel(id, controller.signal)
      .then(data => { setItem(data); setError('') })
      .catch(e => { if (e.name !== 'AbortError') setError(e.message || 'Не удалось загрузить публикацию') })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [id])

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = event => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const isHidden = excluded?.has(item?.username?.toLowerCase())
  const link = instagramUrl(item)
  const canEmbed = /^[\w-]+$/.test(item?.shortcode || '')

  return (
    <div className="carousels-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
      <aside className="carousels-drawer" role="dialog" aria-modal="true" aria-label="Карусель Instagram">
        <div className="carousels-drawer-head">
          <div><span className="carousels-eyebrow">Карусель</span><strong>{item?.username ? `@${item.username}` : 'Публикация Instagram'}</strong></div>
          <button ref={closeRef} type="button" className="carousels-close" onClick={onClose} aria-label="Закрыть">✕</button>
        </div>
        {error && <p className="carousels-error" role="alert">{error}</p>}
        {loading && !item && <p className="carousels-loading" role="status">Загружаем публикацию…</p>}
        {isHidden && <p className="carousels-muted">Этот автор скрыт. Вернуть его можно в списке «Скрытые авторы».</p>}
        {item && !isHidden && <div className="carousels-drawer-body">
          <div className="carousels-media">
            {canEmbed ? (
              <iframe
                src={`https://www.instagram.com/p/${item.shortcode}/embed/`}
                title={`Карусель @${item.username || ''} в Instagram`}
                loading="lazy"
                allow="autoplay; encrypted-media; picture-in-picture"
                allowFullScreen
              />
            ) : <Cover item={item} large />}
            {link && <a href={link} target="_blank" rel="noopener noreferrer" className="carousels-ig-link">Открыть в Instagram ↗</a>}
          </div>
          <div className="carousels-detail">
            <div className="carousels-detail-meta">{date(item.timestamp)} МСК</div>
            <div className="carousels-detail-stats">
              <Metric label="лайков" value={item.likes} />
              <Metric label="комментариев" value={item.comments} />
              <Metric label="просмотров" value={item.views} />
              <Metric label="подписчиков" value={item.followers} />
            </div>
            <h2>Текст публикации</h2>
            {item.caption
              ? <p className="carousels-caption">{item.caption}</p>
              : <p className="carousels-muted">Текст публикации не получен.</p>}
            {!canEmbed && <p className="carousels-muted">Просмотр Instagram недоступен для этой публикации.</p>}
          </div>
        </div>}
      </aside>
    </div>
  )
}

export default function Carousels() {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const pageRef = useRef(null)
  const restoreScroll = useRef(null)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [hidden, setHidden] = useState(() => readHiddenAuthors())
  const [hiddenError, setHiddenError] = useState('')
  const [lastHidden, setLastHidden] = useState(null)
  const excludedAuthors = useMemo(() => hidden.authors.map(item => item.username), [hidden.authors])
  const excluded = useMemo(() => new Set(excludedAuthors), [excludedAuthors])

  useEffect(() => {
    const sync = event => {
      if (event.key === HIDDEN_AUTHORS_KEY || event.key === null) setHidden(readHiddenAuthors())
    }
    window.addEventListener('storage', sync)
    return () => window.removeEventListener('storage', sync)
  }, [])

  const author = searchParams.get('author')?.trim() || ''
  const sort = SORTS.has(searchParams.get('sort')) ? searchParams.get('sort') : 'likes'
  const order = searchParams.get('order') === 'asc' ? 'asc' : 'desc'
  const limit = searchParams.get('limit') === '100' ? 100 : 50
  const rawOffset = Number(searchParams.get('offset'))
  const offset = Number.isSafeInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0
  const detailId = searchParams.get('id')
  const listParams = useMemo(() => ({ author, sort, order, limit, offset, exclude_authors: excludedAuthors }), [author, sort, order, limit, offset, excludedAuthors])

  useEffect(() => { setData(null) }, [author, sort, order, limit, offset])

  useEffect(() => {
    if (!loading && data && restoreScroll.current != null) {
      if (pageRef.current) pageRef.current.scrollTop = restoreScroll.current
      restoreScroll.current = null
    }
  }, [data, loading])

  const updateQuery = useCallback((changes) => {
    const next = new URLSearchParams(location.search)
    Object.entries(changes).forEach(([key, value]) => {
      if (value == null || value === '') next.delete(key)
      else next.set(key, String(value))
    })
    navigate({ pathname: location.pathname, search: `?${next}` }, { state: location.state })
  }, [location.pathname, location.search, location.state, navigate])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    getCarousels(listParams, controller.signal)
      .then(result => setData(result))
      .catch(e => { if (e.name !== 'AbortError') { setData(null); setError(e.message || 'Не удалось загрузить карусели') } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [listParams, refresh])

  useEffect(() => {
    const status = data?.collection?.status
    if (!ACTIVE_STATUSES.has(status)) return undefined
    const timer = window.setInterval(() => setRefresh(value => value + 1), 30000)
    return () => window.clearInterval(timer)
  }, [data?.collection?.status])

  const changePage = changes => {
    updateQuery(changes)
    pageRef.current?.scrollTo({ top: 0, behavior: 'instant' })
  }

  const changeHidden = (username, hide) => {
    try {
      // Успех показываем только после записи: при отказе localStorage список не меняется.
      const current = readHiddenAuthors()
      if (current.error) { setHiddenError(current.error); return }
      const next = hide ? hideAuthor(current.authors, username) : restoreAuthor(current.authors, username)
      writeHiddenAuthors(next)
      restoreScroll.current = offset === 0 ? pageRef.current?.scrollTop || 0 : 0
      setHidden({ authors: next, error: '' })
      setHiddenError('')
      setLastHidden(hide ? username : null)
      updateQuery({ offset: 0, id: null })
    } catch (e) { setHiddenError(e.message) }
  }

  const backPath = safeAuthorsReturn(location.state?.authorsReturnTo)
  const backToRadar = () => navigate(backPath || '/radar', {
    state: backPath ? { authorsScrollTop: location.state?.authorsScrollTop } : undefined,
  })
  const total = Number.isFinite(Number(data?.total)) && data?.total != null ? Number(data.total) : null
  const collection = data?.collection
  const checked = collection?.checked
  const target = collection?.target
  const profile = author && data?.profiles?.find(p => p.username?.toLowerCase() === author.toLowerCase())
  const authorCount = profile?.carousels
  const isPartial = collection && collection.status !== 'finished'
  const authorUnknown = author && authorCount == null
  const items = (data?.items || []).filter(item => !excluded.has(item.username?.toLowerCase()))
  const listItem = items.find(item => String(item.id) === detailId)

  return <main className="carousels-page" ref={pageRef}>
    <div className="carousels-content">
      <button className="carousels-back" type="button" onClick={backToRadar}>← {backPath ? 'К каталогу авторов' : 'Радар'}</button>
      <header className="carousels-heading">
        <div>
          <span className="carousels-eyebrow">Радар · карусели</span>
          <h1>{author ? `Карусели @${author}` : 'Все карусели'}</h1>
          <p>13–19 октября 2026 · время Москвы</p>
        </div>
        {author && <button type="button" className="carousels-all" onClick={() => changePage({ author: null, offset: 0 })}>Все карусели</button>}
      </header>

      <div className="carousels-summary" aria-live="polite">
        <div className="carousels-summary-count">
          <strong>{authorUnknown ? '—' : count(total)}</strong>
          <span>{authorUnknown ? (isPartial ? 'Данные автора ещё не получены' : 'Данные автора недоступны') : 'найдено каруселей'}</span>
        </div>
        {collection && <div className="carousels-progress">
          <span>{STATUS_LABELS[collection.status] || 'Статус сбора неизвестен'}</span>
          <b>{count(checked)} / {count(target)} авторов проверено</b>
          {target > 0 && checked != null && <div className="carousels-progress-track"><i style={{ width: `${Math.min(100, Math.max(0, checked / target * 100))}%` }} /></div>}
          {ACTIVE_STATUSES.has(collection.status) && <small>Список и счётчики обновляются по мере сбора.</small>}
          {collection.cooldown_until && <small>Продолжение после {date(collection.cooldown_until)} МСК</small>}
          {data.collected_at && <small>Обновлено {date(data.collected_at)} МСК</small>}
          <small>19 октября — неполный день. Показан найденный минимум.</small>
          {collection.review_required > 0 && <small role="status">Неполный сбор у {count(collection.review_required)} авторов.</small>}
        </div>}
        <button type="button" className="carousels-reload" onClick={() => setRefresh(value => value + 1)} disabled={loading}>Обновить</button>
      </div>

      <div className="carousels-controls">
        <label>Сортировать
          <select value={sort} onChange={event => changePage({ sort: event.target.value, offset: 0 })}>
            <option value="timestamp">По дате</option>
            <option value="likes">По лайкам</option>
            <option value="comments">По комментариям</option>
          </select>
        </label>
        <label>Порядок
          <select value={order} onChange={event => changePage({ order: event.target.value, offset: 0 })}>
            <option value="desc">Сначала больше / новые</option>
            <option value="asc">Сначала меньше / старые</option>
          </select>
        </label>
        <label>На странице
          <select value={limit} onChange={event => changePage({ limit: event.target.value, offset: 0 })}>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
        </label>
      </div>

      <details className="carousels-hidden-list">
        <summary>Скрытые авторы ({hidden.authors.length})</summary>
        <p className="carousels-muted">Сохранено в этом браузере. Обновление страницы и очистка кэша сохраняют список; очистка данных сайта удаляет его.</p>
        {hidden.authors.length ? <ul>{hidden.authors.map(item => <li key={item.username}>
          <span>@{item.username}</span><small>{date(item.hidden_at)}</small>
          <button type="button" onClick={() => changeHidden(item.username, false)}>Вернуть в сетку</button>
        </li>)}</ul> : <p className="carousels-muted">Скрытых авторов пока нет.</p>}
      </details>
      {(hidden.error || hiddenError) && <p className="carousels-error" role="alert">{hiddenError || hidden.error}</p>}
      {lastHidden && <div className="carousels-hide-notice" role="status">Автор @{lastHidden} скрыт.
        <button type="button" onClick={() => changeHidden(lastHidden, false)}>Отменить</button>
      </div>}
      {author && excluded.has(author.toLowerCase().replace(/^@/, '')) && <div className="carousels-hide-notice">
        Автор @{author.replace(/^@/, '')} в списке скрытых.
        <button type="button" onClick={() => changeHidden(author, false)}>Вернуть в сетку</button>
      </div>}

      {error && <div className="carousels-error" role="alert">{error}<button type="button" onClick={() => setRefresh(value => value + 1)}>Повторить</button></div>}
      {loading && !data && <p className="carousels-loading" role="status">Загружаем карусели…</p>}
      {!loading && !error && !items.length && <p className="carousels-empty">{authorUnknown ? 'Данные каруселей этого автора пока недоступны.' : 'Каруселей по выбранному фильтру пока нет.'}</p>}
      {!error && !!items.length && <div className="carousels-grid">
        {items.map(item => <div className="carousels-tile" key={item.id}><button
          className="carousels-card"
          type="button"
          onClick={() => updateQuery({ id: item.id })}
          aria-label={`Открыть карусель @${item.username || 'автор неизвестен'}`}
        >
          <Cover item={item} />
          <span className="carousels-card-info">
            <strong>@{item.username || '—'}</strong>
            <small>{date(item.timestamp)} МСК</small>
            <span className="carousels-card-stats"><span>♥ {count(item.likes)}</span><span>◌ {count(item.comments)}</span></span>
          </span>
        </button><button type="button" className="carousels-hide-author"
          aria-label={`Скрыть автора @${item.username}`} title={`Скрыть все карусели @${item.username}`}
          onClick={() => changeHidden(item.username, true)}>
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M3 3l18 18M10.6 10.6a2 2 0 002.8 2.8M9.2 4.5A10 10 0 0112 4c5.5 0 9.5 8 9.5 8a19 19 0 01-3.1 4.3M6 6a21 21 0 00-3.5 6s4 8 9.5 8a10 10 0 005-1.4" /></svg>
        </button></div>)}
      </div>}

      {!error && total != null && total > limit && <nav className="carousels-pagination" aria-label="Страницы каруселей">
        <button type="button" disabled={offset === 0} onClick={() => changePage({ offset: Math.max(0, offset - limit) })}>← Назад</button>
        <span>{numberFormat.format(Math.min(offset + 1, total))}–{numberFormat.format(Math.min(offset + limit, total))} из {count(total)}</span>
        <button type="button" disabled={offset + limit >= total} onClick={() => changePage({ offset: offset + limit })}>Вперёд →</button>
      </nav>}
    </div>
    {detailId && <Drawer key={detailId} id={detailId} listItem={listItem} excluded={excluded} onClose={() => updateQuery({ id: null })} />}
  </main>
}
