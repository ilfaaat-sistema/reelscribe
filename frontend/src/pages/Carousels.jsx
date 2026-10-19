import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { thumbUrl } from '../api/client'
import { getCarousel } from '../api/carousels'
import { HIDDEN_AUTHORS_KEY, readHiddenAuthors, writeHiddenAuthors, hideAuthor, restoreAuthor } from '../api/hiddenAuthors'
import { FAVORITE_AUTHORS_KEY, readFavoriteAuthors, writeFavoriteAuthors, toggleFavoriteAuthor } from '../api/favoriteAuthors'
import useCarouselFeed from '../hooks/useCarouselFeed'
import './carousels.css'

const numberFormat = new Intl.NumberFormat('ru-RU')
const dateFormat = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'short', year: 'numeric',
  hour: '2-digit', minute: '2-digit',
})
const SORTS = new Set(['timestamp', 'likes', 'comments'])
const CYRILLIC = { а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'kh', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'shch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya' }
const searchName = value => String(value || '').trim().replace(/^@/, '').toLowerCase().replace(/[а-яё]/g, letter => CYRILLIC[letter])
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
          <div className="carousels-drawer-title"><span className="carousels-eyebrow">Карусель</span><strong>{item?.username ? `@${item.username}` : 'Публикация Instagram'}</strong></div>
          {item && !isHidden && <div className="carousels-head-summary">
            <span className="carousels-head-date">{date(item.timestamp)} МСК</span>
            <div className="carousels-head-stats">
              <Metric label="лайков" value={item.likes} />
              <Metric label="комментариев" value={item.comments} />
              <Metric label="просмотров" value={item.views} />
              <Metric label="подписчиков" value={item.followers} />
            </div>
          </div>}
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
  const [favorites, setFavorites] = useState(() => readFavoriteAuthors())
  const [favoriteError, setFavoriteError] = useState(() => readFavoriteAuthors().error)
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
      if (event.key === FAVORITE_AUTHORS_KEY || event.key === null) {
        const result = readFavoriteAuthors()
        setFavorites(result)
        setFavoriteError(result.error)
      }
    }
    window.addEventListener('storage', sync)
    return () => window.removeEventListener('storage', sync)
  }, [])

  const author = searchParams.get('author')?.trim() || ''
  useEffect(() => { setSearchDraft(author) }, [author])
  const favoriteOnly = searchParams.get('favorite') === '1'
  const sourceAccount = searchParams.get('source') === 'neyro_set7' ? 'neyro_set7' : null
  const sort = SORTS.has(searchParams.get('sort')) ? searchParams.get('sort') : 'likes'
  const order = searchParams.get('order') === 'asc' ? 'asc' : 'desc'
  const limit = searchParams.get('limit') === '100' ? 100 : 50
  const detailId = searchParams.get('id')
  const includeAuthors = favoriteOnly ? favorites.authors : null
  const listParams = useMemo(() => ({ author, sort, order, limit, exclude_authors: excludedAuthors, include_authors: includeAuthors, source_account: sourceAccount }), [author, sort, order, limit, excludedAuthors, includeAuthors, sourceAccount])
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

  const changeFavorite = username => {
    try {
      const next = toggleFavoriteAuthor(favorites.authors, username)
      writeFavoriteAuthors(next)
      setFavorites({ authors: next, error: '' })
      setFavoriteError('')
    } catch (e) { setFavoriteError(e.message) }
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
          <h1>{author ? `Карусели @${author}` : sourceAccount ? 'Карусели NEYRO_SET7' : 'Все карусели'}</h1>
          <p>13–19 октября 2026 · время Москвы</p>
        </div>
        {(author || sourceAccount) && <button type="button" className="carousels-all" onClick={() => changePage({ author: null, source: null, offset: 0 })}>Все карусели</button>}
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
        <button type="button" className="carousels-reload" onClick={refresh} disabled={loading}>Обновить</button>
      </div>

      <div className="carousels-controls">
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
        <label className="carousels-favorite-toggle">
          <input type="checkbox" checked={favoriteOnly} onChange={event => changePage({ favorite: event.target.checked ? '1' : null, offset: 0 })} />
          Только избранные
        </label>
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
        <label>Подгружать по
          <select value={limit} onChange={event => changePage({ limit: event.target.value, offset: 0 })}>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
        </label>
      </div>

      <details className="carousels-favorites-list">
        <summary>Избранные авторы ({favorites.authors.length})</summary>
        <p className="carousels-muted">Сохранено в этом браузере. Добавляйте и убирайте авторов звёздой на карточке.</p>
        {favorites.authors.length ? <ul>{favorites.authors.map(username => <li key={username}>
          <span>@{username}</span><button type="button" onClick={() => changeFavorite(username)} aria-label={`Убрать @${username} из избранного`}>Убрать</button>
        </li>)}</ul> : <p className="carousels-muted">Список пуст. Добавьте автора звёздой на его карточке.</p>}
      </details>
      {favoriteError && <p className="carousels-error" role="alert">{favoriteError}</p>}

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
      {!loading && !error && !items.length && <p className="carousels-empty">{favoriteOnly && !favorites.authors.length ? 'Список избранных авторов пуст. Добавьте авторов звёздой на карточках.' : authorUnknown ? 'Данные каруселей этого автора пока недоступны.' : 'Каруселей по выбранному фильтру пока нет.'}</p>}
      {!!items.length && <div className="carousels-grid">
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
        </button><button type="button" className={`carousels-favorite-author${favorites.authors.includes(item.username?.toLowerCase()) ? ' is-favorite' : ''}`}
          aria-pressed={favorites.authors.includes(item.username?.toLowerCase())}
          aria-label={favorites.authors.includes(item.username?.toLowerCase()) ? `Убрать @${item.username} из избранного` : `Добавить @${item.username} в избранное`}
          title={favorites.authors.includes(item.username?.toLowerCase()) ? 'Убрать из избранного' : 'Добавить в избранное'}
          onClick={() => changeFavorite(item.username)}>
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
    {detailId && <Drawer key={detailId} id={detailId} listItem={listItem} excluded={excluded} onClose={() => updateQuery({ id: null })} />}
  </main>
}
