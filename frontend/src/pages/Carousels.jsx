import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { thumbUrl } from '../api/client'
import { getCarousel } from '../api/carousels'
import { HIDDEN_AUTHORS_KEY, readHiddenAuthors, writeHiddenAuthors, hideAuthor, restoreAuthor } from '../api/hiddenAuthors'
import { FAVORITES_IMPORT_KEY, readFavoriteAuthors, getFavoriteAuthors, addFavoriteAuthor, removeFavoriteAuthor } from '../api/favoriteAuthors'
import useSelectedCarousels from '../hooks/useSelectedCarousels'
import useCarouselFeed from '../hooks/useCarouselFeed'
import './carousels.css'

const numberFormat = new Intl.NumberFormat('ru-RU')
const dateFormat = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'short', year: 'numeric',
  hour: '2-digit', minute: '2-digit',
})
const periodDateFormat = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'long', year: 'numeric',
})
const SORTS = new Set(['timestamp', 'likes', 'comments'])
const CYRILLIC = { а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'kh', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'shch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya' }
const searchName = value => String(value || '').trim().replace(/^@/, '').toLowerCase().replace(/[а-яё]/g, letter => CYRILLIC[letter])
const STATUS_LABELS = {
  running: 'Сбор продолжается', cooldown: 'Ожидание лимита Meta', finished: 'Обход авторов завершён',
  failed: 'Сбор прерван', authentication_required: 'Нужна проверка авторизации Meta',
  permissions_or_request_error: 'Нужна проверка доступа Meta',
}
const FAVORITE_STATUS = { pending: 'Ожидает', queued: 'Ожидает', collecting: 'Собирается', running: 'Собирается', updated: 'Обновлено', finished: 'Обновлено', cooldown: 'Пауза Meta', meta_pause: 'Пауза Meta', meta_paused: 'Пауза Meta', unavailable: 'Недоступен', error: 'Ошибка', failed: 'Ошибка' }
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

function Drawer({ period, id, listItem, onClose, excluded, selected, selectionBusy, onToggleSelection }) {
  const [item, setItem] = useState(listItem || null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const closeRef = useRef(null)

  useEffect(() => {
    const controller = new AbortController()
    getCarousel(id, controller.signal, period)
      .then(data => { setItem(data); setError('') })
      .catch(e => { if (e.name !== 'AbortError') setError(e.message || 'Не удалось загрузить публикацию') })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [id, period])

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
        <button ref={closeRef} type="button" className="carousels-close" onClick={onClose} aria-label="Закрыть">✕</button>
        {item && <button type="button" className="carousels-select-drawer" disabled={selectionBusy} aria-pressed={selected} onClick={() => onToggleSelection(item)}>{selectionBusy ? 'Сохраняем…' : selected ? '♥ Отобрано · снять отметку' : '♡ Отобрать карусель'}</button>}
        {error && <p className="carousels-error" role="alert">{error}</p>}
        {loading && !item && <p className="carousels-loading" role="status">Загружаем публикацию…</p>}
        {isHidden && <p className="carousels-muted">Этот автор скрыт. Вернуть его можно в списке «Скрытые авторы».</p>}
        {item && !isHidden && <div className="carousels-drawer-body">
          <div className="carousels-media">
            <div className="carousels-drawer-head">
              <div className="carousels-drawer-title"><span className="carousels-eyebrow">Карусель</span><strong>{item.username ? `@${item.username}` : 'Публикация Instagram'}</strong></div>
              <div className="carousels-head-stats">
                <Metric label="подписчиков" value={item.followers} />
                <Metric label="лайков" value={item.likes} />
                <Metric label="комментариев" value={item.comments} />
                <Metric label="просмотров" value={item.views} />
              </div>
            </div>
            {canEmbed ? (
              <iframe
                src={`https://www.instagram.com/p/${item.shortcode}/embed/`}
                title={`Карусель @${item.username || ''} в Instagram`}
                loading="lazy"
                allow="autoplay; encrypted-media; picture-in-picture"
                allowFullScreen
              />
            ) : <Cover item={item} large />}
            <div className="carousels-media-footer">
              {link && <a href={link} target="_blank" rel="noopener noreferrer" className="carousels-ig-link">Открыть в Instagram ↗</a>}
              <span className="carousels-media-date">{date(item.timestamp)} МСК</span>
            </div>
          </div>
          <div className="carousels-detail">
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
  const sentinelRef = useRef(null)
  const [hidden, setHidden] = useState(() => readHiddenAuthors())
  const [favorites, setFavorites] = useState({ authors: [] })
  const selected = useSelectedCarousels()
  const [favoriteRows, setFavoriteRows] = useState([])
  const [favoriteLoading, setFavoriteLoading] = useState(true)
  const [favoriteBusy, setFavoriteBusy] = useState(false)
  const [favoriteError, setFavoriteError] = useState('')
  const [favoriteNotice, setFavoriteNotice] = useState('')
  const [profileDraft, setProfileDraft] = useState('')
  const [favoritesExpanded, setFavoritesExpanded] = useState(() => {
    try { return localStorage.getItem('reelscribe:carousels:favorites-collapsed') !== '1' } catch { return true }
  })
  const [legacy] = useState(() => readFavoriteAuthors())
  const [importDone, setImportDone] = useState(() => { try { return localStorage.getItem(FAVORITES_IMPORT_KEY) === '1' } catch { return false } })
  const applyFavorites = useCallback(rows => {
    setFavoriteRows(rows)
    setFavorites(current => {
      const authors = rows.map(row => row.username)
      return JSON.stringify(current.authors) === JSON.stringify(authors) ? current : { authors }
    })
  }, [])
  const syncFavorites = useCallback(async signal => {
    try {
      const result = await getFavoriteAuthors(signal)
      applyFavorites(result.authors)
      setFavoriteError('')
    } catch (e) { if (e.name !== 'AbortError') setFavoriteError(e.message) }
    finally { if (!signal?.aborted) setFavoriteLoading(false) }
  }, [applyFavorites])
  useEffect(() => {
    const controller = new AbortController()
    syncFavorites(controller.signal)
    const timer = setInterval(() => syncFavorites(controller.signal), 30000)
    const onFocus = () => syncFavorites(controller.signal)
    window.addEventListener('focus', onFocus)
    return () => { controller.abort(); clearInterval(timer); window.removeEventListener('focus', onFocus) }
  }, [syncFavorites])
  const [hiddenError, setHiddenError] = useState('')
  const [lastHidden, setLastHidden] = useState(null)
  const [searchDraft, setSearchDraft] = useState(() => searchParams.get('author') || '')
  const [searchFocused, setSearchFocused] = useState(false)
  const [searchError, setSearchError] = useState('')
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
  useEffect(() => { setSearchDraft(author) }, [author])
  const period = ['7d', '14d', 'archive14d'].includes(searchParams.get('period')) ? searchParams.get('period') : null
  const favoriteOnly = searchParams.get('favorite') === '1'
  const sourceAccount = searchParams.get('source') === 'neyro_set7' ? 'neyro_set7' : null
  const sort = SORTS.has(searchParams.get('sort')) ? searchParams.get('sort') : 'likes'
  const order = searchParams.get('order') === 'asc' ? 'asc' : 'desc'
  const limit = searchParams.get('limit') === '100' ? 100 : 50
  const detailId = searchParams.get('id')
  const includeAuthors = favoriteOnly ? favorites.authors : null
  const listParams = useMemo(() => ({ period, author, sort, order, limit, exclude_authors: excludedAuthors, include_authors: includeAuthors, source_account: sourceAccount }), [period, author, sort, order, limit, excludedAuthors, includeAuthors, sourceAccount])
  const { data, loading, error, loadingMore, moreError, loadMore, refresh, hasMore } = useCarouselFeed(listParams)

  useEffect(() => {
    if (!hasMore || loading || loadingMore || error || moreError || !sentinelRef.current || !('IntersectionObserver' in window)) return undefined
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) loadMore()
    }, { root: pageRef.current, rootMargin: '600px 0px' })
    observer.observe(sentinelRef.current)
    return () => observer.disconnect()
  }, [hasMore, loading, loadingMore, error, moreError, loadMore])

  useEffect(() => {
    if (!loading && data && restoreScroll.current != null) {
      if (pageRef.current) pageRef.current.scrollTop = restoreScroll.current
      restoreScroll.current = null
    }
  }, [data, loading])

  useEffect(() => {
    if (!loading && data && Number.isFinite(location.state?.carouselScrollTop)) {
      if (pageRef.current) pageRef.current.scrollTop = location.state.carouselScrollTop
      navigate(location.pathname + location.search, { replace: true, state: { ...location.state, carouselScrollTop: null } })
    }
  }, [data, loading, location.pathname, location.search, location.state, navigate])

  const updateQuery = useCallback((changes) => {
    const next = new URLSearchParams(location.search)
    Object.entries(changes).forEach(([key, value]) => {
      if (value == null || value === '') next.delete(key)
      else next.set(key, String(value))
    })
    navigate({ pathname: location.pathname, search: `?${next}` }, { state: location.state })
  }, [location.pathname, location.search, location.state, navigate])

  const changePage = changes => {
    updateQuery(changes)
    pageRef.current?.scrollTo({ top: 0, behavior: 'instant' })
  }

  const searchTerm = searchName(searchDraft)
  const searchMatches = searchTerm.length >= 2
    ? (data?.profiles || []).filter(profile => profile.username?.toLowerCase().includes(searchTerm)).slice(0, 6)
    : []
  const chooseAuthor = username => {
    setSearchDraft(username || '')
    setSearchFocused(false)
    setSearchError('')
    changePage({ author: username || null, id: null, offset: 0 })
  }
  const chooseFavoriteAuthor = username => {
    setSearchDraft(username || '')
    setSearchFocused(false)
    setSearchError('')
    restoreScroll.current = pageRef.current?.scrollTop || 0
    updateQuery({ author: username || null, id: null, offset: 0 })
  }
  const toggleFavoritesExpanded = () => {
    const expanded = !favoritesExpanded
    setFavoritesExpanded(expanded)
    try { localStorage.setItem('reelscribe:carousels:favorites-collapsed', expanded ? '0' : '1') } catch { /* Сворачивание работает и без локального сохранения. */ }
  }
  const submitAuthor = event => {
    event.preventDefault()
    if (!searchTerm) { chooseAuthor(null); return }
    const exact = searchMatches.find(profile => profile.username?.toLowerCase() === searchTerm)
    if (exact || searchMatches.length === 1) chooseAuthor((exact || searchMatches[0]).username)
    else if (searchMatches.length) setSearchFocused(true)
    else setSearchError('Автор не найден в сохранённой истории.')
  }

  const changeHidden = (username, hide) => {
    try {
      // Успех показываем только после записи: при отказе localStorage список не меняется.
      const current = readHiddenAuthors()
      if (current.error) { setHiddenError(current.error); return }
      const next = hide ? hideAuthor(current.authors, username) : restoreAuthor(current.authors, username)
      writeHiddenAuthors(next)
      restoreScroll.current = pageRef.current?.scrollTop || 0
      setHidden({ authors: next, error: '' })
      setHiddenError('')
      setLastHidden(hide ? username : null)
      updateQuery({ offset: 0, id: null })
    } catch (e) { setHiddenError(e.message) }
  }

  const changeFavorite = async (username, forceAdd = false) => {
    if (favoriteBusy || favoriteLoading) return false
    setFavoriteBusy(true)
    setFavoriteError('')
    setFavoriteNotice('')
    try {
      if (!forceAdd && favorites.authors.includes(username.toLowerCase())) {
        await removeFavoriteAuthor(username)
        const rows = favoriteRows.filter(row => row.username !== username.toLowerCase())
        applyFavorites(rows)
        setFavoriteNotice(`@${username} убран из общего списка.`)
      } else {
        const row = await addFavoriteAuthor(forceAdd ? username : '@' + username)
        applyFavorites([...favoriteRows.filter(item => item.username !== row.username), row])
        setFavoriteNotice(`@${row.username} сохранён. Статус сбора показан в списке.`)
      }
      refresh()
      return true
    } catch (e) { setFavoriteError(e.message); return false }
    finally { setFavoriteBusy(false) }
  }
  const submitFavorite = async event => {
    event.preventDefault()
    if (await changeFavorite(profileDraft, true)) setProfileDraft('')
  }
  const legacyCandidates = legacy.authors.filter(username => !favorites.authors.includes(username))
  const importLegacy = async () => {
    if (favoriteBusy) return
    setFavoriteBusy(true)
    try {
      for (const username of legacyCandidates) await addFavoriteAuthor('@' + username)
      await syncFavorites()
      localStorage.setItem(FAVORITES_IMPORT_KEY, '1')
      setImportDone(true)
      setFavoriteNotice('Локальное избранное перенесено. Резервная копия в браузере сохранена.')
      refresh()
    } catch (e) { await syncFavorites(); setFavoriteError(e.message) }
    finally { setFavoriteBusy(false) }
  }

  const changeSelected = item => {
    selected.setSelected(item, selectedPeriod || period || '14d', !selected.items.some(entry => entry.id === String(item.id)))
  }

  const backPath = safeAuthorsReturn(location.state?.authorsReturnTo)
  const backToRadar = () => navigate(backPath || '/radar', {
    state: backPath ? { authorsScrollTop: location.state?.authorsScrollTop } : undefined,
  })
  const total = Number.isFinite(Number(data?.total)) && data?.total != null ? Number(data.total) : null
  const selectedPeriod = period || (data?.start_date === '2026-10-06' ? 'archive14d' : data ? '7d' : null)
  const collection = data?.collection
  const checked = collection?.checked
  const target = collection?.target
  const profile = author && data?.profiles?.find(p => p.username?.toLowerCase() === author.toLowerCase())
  const authorCount = profile?.carousels
  const isPartial = collection && collection.status !== 'finished'
  const authorUnknown = author && authorCount == null
  const items = (data?.items || []).filter(item => !excluded.has(item.username?.toLowerCase()))
  const listItem = items.find(item => String(item.id) === detailId)
  const selectedIds = new Set(selected.items.map(item => item.id))

  return <main className="carousels-page" ref={pageRef}>
    <div className="carousels-content">
      <button className="carousels-back" type="button" onClick={backToRadar}>← {backPath ? 'К каталогу авторов' : 'Радар'}</button>
      <header className="carousels-heading">
        <div>
          <span className="carousels-eyebrow">Радар · карусели</span>
          <h1>{author ? `Карусели @${author}` : sourceAccount ? 'Карусели NEYRO_SET7' : selectedPeriod === '14d' ? 'Карусели избранных' : 'Все карусели'}</h1>
          <p>{data?.start_date && data?.end_date ? `${periodDateFormat.format(new Date(`${data.start_date}T12:00:00+03:00`))} — ${periodDateFormat.format(new Date(`${data.end_date}T12:00:00+03:00`))} · время Москвы` : 'Период загружается…'}</p>
        </div>
        <div className="carousels-heading-actions">
          {(author || sourceAccount) && <button type="button" className="carousels-all" onClick={() => changePage({ author: null, source: null, offset: 0 })}>Все карусели</button>}
        </div>
      </header>
      {selected.error && <p className="carousels-error" role="alert">{selected.error}<button type="button" onClick={selected.refresh}>Повторить</button></p>}

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
          {selectedPeriod === 'archive14d' && <small>19 октября — неполный день. Показан найденный минимум.</small>}
          {collection.review_required > 0 && <small role="status">Неполный сбор у {count(collection.review_required)} авторов.</small>}
        </div>}
        <button type="button" className="carousels-reload" onClick={() => { refresh(); syncFavorites() }} disabled={loading}>Обновить</button>
      </div>

      <div className="carousels-controls">
        <label>Период
          <select aria-label="Период каруселей" value={selectedPeriod || ''} onChange={event => changePage({ period: event.target.value, id: null, offset: 0 })}>
            {!period && !data && <option value="">Автоматически</option>}
            <option value="7d">7 дней · 13–19 октября</option>
            <option value="14d">Последние 14 дней · избранные</option>
            <option value="archive14d">Архив 6–19 октября</option>
          </select>
        </label>
        <form className="carousels-author-search" onSubmit={submitAuthor}
          onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setSearchFocused(false) }}>
          <input aria-label="Поиск автора" placeholder="Сердитов или serditov" autoComplete="off"
            value={searchDraft} onFocus={() => setSearchFocused(true)}
            onChange={event => { setSearchDraft(event.target.value); setSearchError(''); setSearchFocused(true) }} />
          {searchDraft && <button type="button" aria-label="Очистить поиск автора" onClick={() => chooseAuthor(null)}>×</button>}
          <button type="submit" aria-label="Найти автора">⌕</button>
          {searchFocused && searchMatches.length > 0 && <div className="carousels-author-options" role="listbox" aria-label="Найденные авторы">
            {searchMatches.map(profile => <button key={profile.username} type="button" role="option" aria-selected={author === profile.username}
              onClick={() => chooseAuthor(profile.username)}>@{profile.username}</button>)}
          </div>}
          {searchError && <span className="carousels-author-search-error" role="alert">{searchError}</span>}
        </form>
        {selectedPeriod !== '14d' && <label className="carousels-favorite-toggle">
          <input type="checkbox" checked={favoriteOnly} onChange={event => changePage({ favorite: event.target.checked ? '1' : null, offset: 0 })} />
          Только избранные
        </label>}
        <label className="carousels-favorite-toggle">
          <input type="checkbox" checked={sourceAccount === 'neyro_set7'} onChange={event => changePage({ source: event.target.checked ? 'neyro_set7' : null, offset: 0 })} />
          Только NEYRO_SET7
        </label>
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
        <div className="carousels-page-actions">
        <label>Подгружать по
          <select value={limit} onChange={event => changePage({ limit: event.target.value, offset: 0 })}>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
        </label>
          <button type="button" className="carousels-all" onClick={() => navigate(`/radar/carousels/selected?${new URLSearchParams({ returnTo: location.pathname + location.search })}`, { state: { carouselScrollTop: pageRef.current?.scrollTop || 0 } })}>Отобранные карусели ({selected.loading ? '…' : count(selected.items.length)})</button>
        </div>
      </div>

      <section className="carousels-favorites-list" aria-label="Избранные авторы">
        <div className="carousels-favorites-heading">
          <h2><button type="button" className="carousels-favorites-toggle" aria-expanded={favoritesExpanded}
            aria-controls="carousels-favorites-content" onClick={toggleFavoritesExpanded}>
            <span aria-hidden="true">{favoritesExpanded ? '▾' : '▸'}</span> Избранные авторы ({favorites.authors.length})
            <small>{favoritesExpanded ? 'Скрыть' : 'Показать'}</small>
          </button></h2>
          <button type="button" className={`carousels-favorites-reset${!author ? ' is-active' : ''}`}
            aria-pressed={!author} onClick={() => chooseFavoriteAuthor(null)}>{selectedPeriod === '14d' || favoriteOnly ? 'Все избранные' : 'Все авторы'}</button>
        </div>
        <div id="carousels-favorites-content" hidden={!favoritesExpanded}>
        <p className="carousels-muted">Общий список на всех устройствах. Сбор новых авторов начинается автоматически; доступны публичные профессиональные профили.</p>
        <form className="carousels-profile-form" onSubmit={submitFavorite}>
          <label htmlFor="favorite-profile">Ссылка на профиль Instagram или @username</label>
          <div><input id="favorite-profile" type="text" inputMode="url" autoCapitalize="none" autoCorrect="off" spellCheck={false} autoComplete="off"
            placeholder="https://www.instagram.com/username/" value={profileDraft} onChange={event => setProfileDraft(event.target.value)} />
          <button type="submit" disabled={favoriteBusy || favoriteLoading || !profileDraft.trim()}>Добавить</button></div>
        </form>
        {favoriteLoading ? <p role="status">Загружаем общий список…</p> : favoriteRows.length ? <ul>{favoriteRows.map(row => <li key={row.username}
          className={author.toLowerCase().replace(/^@/, '') === row.username ? 'is-active' : undefined}>
          <button type="button" className="carousels-favorite-filter"
            aria-pressed={author.toLowerCase().replace(/^@/, '') === row.username}
            aria-label={`Показать карусели @${row.username}`} onClick={() => chooseFavoriteAuthor(row.username)}>
            <span>@{row.username}</span><small>{FAVORITE_STATUS[row.status] || 'Ожидает'}{row.last_checked_at ? ` · ${date(row.last_checked_at)} МСК` : ' · ещё не проверен'}</small>{row.error && <small>{row.error}</small>}
          </button>
          <button type="button" disabled={favoriteBusy} onClick={() => changeFavorite(row.username)} aria-label={`Убрать @${row.username} из избранного`}>Убрать</button>
        </li>)}</ul> : <p>Список пуст. Вставьте ссылку на профиль выше.</p>}
        {!favoriteLoading && !importDone && legacyCandidates.length > 0 && <details>
          <summary>Перенести избранное из этого браузера ({legacyCandidates.length})</summary>
          <p>{legacyCandidates.map(username => '@' + username).join(', ')}</p>
          <button type="button" disabled={favoriteBusy} onClick={importLegacy}>Перенести в общий список</button>
          <p>Локальная резервная копия сохранится.</p>
        </details>}
        {legacy.error && <p role="alert">{legacy.error}</p>}
        {favoriteNotice && <p role="status">{favoriteNotice}</p>}
        {favoriteError && <p className="carousels-error" role="alert">{favoriteError}<button type="button" onClick={() => syncFavorites()}>Повторить</button></p>}
        </div>
      </section>

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

      {error && <div className="carousels-error" role="alert">{error}<button type="button" onClick={refresh}>Повторить</button></div>}
      {loading && !data && <p className="carousels-loading" role="status">Загружаем карусели…</p>}
      {!loading && !error && !items.length && <p className="carousels-empty">{period === '14d' ? favoriteLoading ? 'Проверяем общий список избранных…' : !favorites.authors.length ? 'Добавьте профиль в общий список избранных.' : favoriteRows.some(row => (!author || row.username === author.toLowerCase()) && !row.last_checked_at) ? 'Автор ещё не проверен. Сбор начнётся автоматически; статус доступен выше.' : 'Авторы проверены. За выбранный период и с текущими фильтрами каруселей нет.' : favoriteOnly && !favorites.authors.length ? 'Список избранных авторов пуст. Добавьте авторов звёздой на карточках.' : authorUnknown ? 'Данные каруселей этого автора пока недоступны.' : 'Каруселей по выбранному фильтру пока нет.'}</p>}
      {!!items.length && <div className="carousels-grid">
        {items.map(item => <div className="carousels-tile" key={item.id}><button
          className="carousels-card"
          type="button"
          onClick={() => updateQuery({ id: item.id, period: selectedPeriod })}
          aria-label={`Открыть карусель @${item.username || 'автор неизвестен'}`}
        >
          <Cover item={item} />
          <span className="carousels-card-info">
            <strong>@{item.username || '—'}</strong>
            <small>{date(item.timestamp)} МСК</small>
            <span className="carousels-card-stats"><span>♥ {count(item.likes)}</span><span>◌ {count(item.comments)}</span></span>
          </span>
        </button><button type="button" className={`carousels-select${selectedIds.has(String(item.id)) ? ' is-selected' : ''}`}
          disabled={selected.loading || selected.busy.has(String(item.id))}
          aria-pressed={selectedIds.has(String(item.id))}
          aria-label={selectedIds.has(String(item.id)) ? `Убрать карусель @${item.username} из отобранных` : `Отобрать карусель @${item.username}`}
          title={selectedIds.has(String(item.id)) ? 'Убрать карусель из отобранных' : 'Отобрать карусель'}
          onClick={() => changeSelected(item)}><span aria-hidden="true">{selectedIds.has(String(item.id)) ? '♥' : '♡'}</span>
        </button><button type="button" className="carousels-hide-author"
          aria-label={`Скрыть автора @${item.username}`} title={`Скрыть все карусели @${item.username}`}
          onClick={() => changeHidden(item.username, true)}>
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M3 3l18 18M10.6 10.6a2 2 0 002.8 2.8M9.2 4.5A10 10 0 0112 4c5.5 0 9.5 8 9.5 8a19 19 0 01-3.1 4.3M6 6a21 21 0 00-3.5 6s4 8 9.5 8a10 10 0 005-1.4" /></svg>
        </button><button type="button" className={`carousels-favorite-author${favorites.authors.includes(item.username?.toLowerCase()) ? ' is-favorite' : ''}`}
          aria-pressed={favorites.authors.includes(item.username?.toLowerCase())}
          aria-label={favorites.authors.includes(item.username?.toLowerCase()) ? `Убрать @${item.username} из избранного` : `Добавить @${item.username} в избранное`}
          title={favorites.authors.includes(item.username?.toLowerCase()) ? 'Убрать из избранного' : 'Добавить в избранное'}
          disabled={favoriteBusy || favoriteLoading} onClick={() => changeFavorite(item.username)}>
          <span aria-hidden="true">{favorites.authors.includes(item.username?.toLowerCase()) ? '★' : '☆'}</span>
        </button></div>)}
      </div>}

      {data && <div className="carousels-feed-end" ref={sentinelRef} aria-live="polite">
        <span>{count(items.length)} / {count(total)}</span>
        {loadingMore && <span role="status">Загружаем следующие карусели…</span>}
        {moreError && <p role="alert">{moreError}</p>}
        {hasMore && <button type="button" onClick={loadMore} disabled={loading || loadingMore}>
          {moreError ? 'Повторить загрузку' : 'Загрузить ещё'}
        </button>}
        {!hasMore && !loading && !error && !moreError && items.length > 0 && <span>Все найденные карусели показаны</span>}
      </div>}
    </div>
    {detailId && <Drawer period={period} key={`${period || 'auto'}-${detailId}`} id={detailId} listItem={listItem} excluded={excluded} selected={selectedIds.has(String(detailId))} selectionBusy={selected.loading || selected.busy.has(String(detailId))} onToggleSelection={changeSelected} onClose={() => updateQuery({ id: null })} />}
  </main>
}
