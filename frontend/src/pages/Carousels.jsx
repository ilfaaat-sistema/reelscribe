import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import Cover from '../components/CarouselCover'
import CarouselCard from '../components/CarouselCard'
import { getCarousel, translateCarousel } from '../api/carousels'
import { CAROUSEL_RANGE_KEYS, carouselPeriod, carouselRangeDraft, validateCarouselRanges, favoriteFilterEnabled, isLiveCarouselPeriod, toggleFavoriteFilter } from '../api/carouselFilters'
import useHiddenAuthors from '../hooks/useHiddenAuthors'
import { FAVORITES_IMPORT_KEY, readFavoriteAuthors, getFavoriteAuthors, addFavoriteAuthor, removeFavoriteAuthor } from '../api/favoriteAuthors'
import useSelectedCarousels from '../hooks/useSelectedCarousels'
import useCarouselFeed from '../hooks/useCarouselFeed'
import CarouselCoverTranslation from '../components/CarouselCoverTranslation'
import { carouselCaptionView, isCurrentCarouselRequest, isCurrentCarouselTranslation } from './carouselTranslation'
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
const ORDER_LABELS = {
  timestamp: { desc: 'От новых к старым', asc: 'От старых к новым' },
  likes: { desc: 'От большего числа лайков к меньшему', asc: 'От меньшего числа лайков к большему' },
  comments: { desc: 'От большего числа комментариев к меньшему', asc: 'От меньшего числа комментариев к большему' },
}
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


function Metric({ label, value }) {
  return <span className="carousels-metric"><b>{count(value)}</b><small>{label}</small></span>
}

function Drawer({ period, id, listItem, onClose, excluded, selected, selectionBusy, onToggleSelection }) {
  const [item, setItem] = useState(listItem || null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [showTranslation, setShowTranslation] = useState(true)
  const [translationBusy, setTranslationBusy] = useState(false)
  const [translationError, setTranslationError] = useState('')
  const [translationRefreshRequired, setTranslationRefreshRequired] = useState(false)
  const [translationRefreshBusy, setTranslationRefreshBusy] = useState(false)
  const closeRef = useRef(null)
  const initialItemRef = useRef(listItem || null)
  const itemRef = useRef(item)
  const detailSequenceRef = useRef(0)
  const translationRequestRef = useRef(null)
  const translationRefreshRef = useRef(null)
  const drawerKey = `${String(id)}\u0000${period || ''}`
  const drawerKeyRef = useRef(drawerKey)
  drawerKeyRef.current = drawerKey

  const applyDetails = useCallback(data => {
    itemRef.current = data
    setItem(data)
    setShowTranslation(data?.caption_translation?.state === 'ready')
    setTranslationRefreshRequired(false)
  }, [])

  const refreshDetails = useCallback(async signal => {
    const key = `${String(id)}\u0000${period || ''}`
    const sequence = ++detailSequenceRef.current
    const data = await getCarousel(id, signal, period)
    if (signal?.aborted || sequence !== detailSequenceRef.current || drawerKeyRef.current !== key) return null
    applyDetails(data)
    setError('')
    return data
  }, [applyDetails, id, period])

  useEffect(() => {
    const controller = new AbortController()
    itemRef.current = initialItemRef.current
    setItem(initialItemRef.current)
    setLoading(true)
    setShowTranslation(true)
    setTranslationBusy(false)
    setTranslationError('')
    setTranslationRefreshRequired(false)
    setTranslationRefreshBusy(false)
    translationRequestRef.current?.controller.abort()
    translationRequestRef.current?.refreshController?.abort()
    translationRefreshRef.current?.abort()
    translationRequestRef.current = null
    translationRefreshRef.current = null
    refreshDetails(controller.signal)
      .catch(e => { if (e.name !== 'AbortError') setError(e.message || 'Не удалось загрузить публикацию') })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => {
      controller.abort()
      detailSequenceRef.current += 1
      translationRequestRef.current?.controller.abort()
      translationRequestRef.current?.refreshController?.abort()
      translationRefreshRef.current?.abort()
      translationRequestRef.current = null
      translationRefreshRef.current = null
    }
  }, [id, period, refreshDetails])

  async function requestTranslation() {
    const current = itemRef.current
    const metadata = current?.caption_translation
    if (!current || metadata?.state !== 'available' || !metadata.source_hash || translationRequestRef.current) return

    const request = {
      key: drawerKeyRef.current,
      id: String(current.id ?? id),
      period: period || null,
      sourceHash: metadata.source_hash,
      controller: new AbortController(),
    }
    translationRequestRef.current = request
    setTranslationBusy(true)
    setTranslationError('')
    try {
      const result = await translateCarousel(id, { period: request.period, sourceHash: request.sourceHash }, request.controller.signal)
      const translation = result?.caption_translation
      const latest = itemRef.current
      const requestIsCurrent = isCurrentCarouselRequest({
        item: latest,
        requestId: request.id,
        sourceHash: request.sourceHash,
        requestKey: request.key,
        currentKey: drawerKeyRef.current,
      })
      if (!requestIsCurrent) return
      if (!isCurrentCarouselTranslation({
        result,
        item: latest,
        requestId: request.id,
        sourceHash: request.sourceHash,
        requestKey: request.key,
        currentKey: drawerKeyRef.current,
      })) throw new Error('Сервер вернул некорректный ответ')
      const next = { ...latest, caption_translation: translation }
      itemRef.current = next
      setItem(next)
      setShowTranslation(true)
    } catch (requestError) {
      if (requestError?.name === 'AbortError' || drawerKeyRef.current !== request.key) return
      if (requestError?.code === 'source_changed' || requestError?.code === 'source_missing') {
        const refreshController = new AbortController()
        request.refreshController = refreshController
        try { await refreshDetails(refreshController.signal) }
        catch (refreshError) {
          if (refreshError?.name !== 'AbortError' && translationRequestRef.current === request) {
            setError(refreshError.message || 'Не удалось обновить публикацию')
            setTranslationRefreshRequired(true)
          }
        }
      } else if (requestError?.code === 'translation_not_needed' && translationRequestRef.current === request) {
        const latest = itemRef.current
        if (latest?.caption_translation?.source_hash === request.sourceHash) {
          const next = { ...latest, caption_translation: { ...latest.caption_translation, state: 'not_needed', translation: null } }
          itemRef.current = next
          setItem(next)
          setShowTranslation(false)
        }
      }
      if (translationRequestRef.current === request && drawerKeyRef.current === request.key) {
        setTranslationError(requestError.message || 'Не удалось перевести текст')
      }
    } finally {
      if (translationRequestRef.current === request) {
        translationRequestRef.current = null
        setTranslationBusy(false)
      }
    }
  }

  async function retryDetailsRefresh() {
    if (translationRefreshRef.current) return
    const controller = new AbortController()
    translationRefreshRef.current = controller
    setTranslationRefreshBusy(true)
    setTranslationError('')
    try {
      const data = await refreshDetails(controller.signal)
      if (data) setError('')
    } catch (refreshError) {
      if (refreshError?.name !== 'AbortError') {
        setError(refreshError.message || 'Не удалось обновить публикацию')
        setTranslationError('Не удалось обновить текст. Попробуйте ещё раз.')
      }
    } finally {
      if (translationRefreshRef.current === controller) {
        translationRefreshRef.current = null
        setTranslationRefreshBusy(false)
      }
    }
  }

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = event => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const isHidden = excluded?.has(item?.username?.toLowerCase())
  const link = instagramUrl(item)
  const canEmbed = /^[\w-]+$/.test(item?.shortcode || '')
  const { hasTranslation, displayedCaption, canRequest, unavailable: translationUnavailable } = carouselCaptionView(item, { detailsLoaded: !loading, showTranslation, refreshRequired: translationRefreshRequired })

  return (
    <div className="carousels-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
      <aside className="carousels-drawer" role="dialog" aria-modal="true" aria-label="Карусель Instagram">
        <button ref={closeRef} type="button" className="carousels-close" onClick={onClose} aria-label="Закрыть">✕</button>
        {error && <p className="carousels-error" role="alert">{error}</p>}
        {loading && !item && <p className="carousels-loading" role="status">Загружаем публикацию…</p>}
        {isHidden && <p className="carousels-muted">Этот автор скрыт. Вернуть его можно в списке «Скрытые авторы».</p>}
        {item && !isHidden && <div className="carousels-drawer-body">
          <div className="carousels-media">
            <div className="carousels-drawer-head">
              <div className="carousels-drawer-title"><span className="carousels-eyebrow">Карусель</span><strong>{item.username ? `@${item.username}` : 'Публикация Instagram'}</strong></div>
              <button type="button" className="carousels-select-drawer" disabled={selectionBusy} aria-pressed={selected}
                aria-label={selectionBusy ? 'Сохраняем отбор карусели' : selected ? 'Убрать карусель из отобранных' : 'Отобрать карусель'}
                title={selected ? 'Убрать карусель из отобранных' : 'Отобрать карусель'} onClick={() => onToggleSelection(item)}>
                <span aria-hidden="true">{selectionBusy ? '…' : selected ? '♥' : '♡'}</span>
              </button>
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
            <CarouselCoverTranslation id={id} period={period} full />
            <div className="carousels-translation-heading">
              <h2>Текст публикации</h2>
              <div className="carousels-translation-actions">
                {hasTranslation && <div className="carousels-translation-switch" role="group" aria-label="Язык текста публикации">
                  <button type="button" aria-pressed={!showTranslation} onClick={() => setShowTranslation(false)}>Оригинал</button>
                  <button type="button" aria-pressed={showTranslation} onClick={() => setShowTranslation(true)}>Русский</button>
                </div>}
                {canRequest && <button className="carousels-translation-request" type="button" disabled={translationBusy} onClick={requestTranslation}>
                  {translationBusy ? 'Переводим…' : 'Перевести'}
                </button>}
                {translationRefreshRequired && <button className="carousels-translation-refresh" type="button" disabled={translationRefreshBusy} onClick={retryDetailsRefresh}>
                  {translationRefreshBusy ? 'Обновляем…' : 'Обновить текст'}
                </button>}
              </div>
            </div>
            {item.caption
              ? <p className="carousels-caption">{displayedCaption}</p>
              : <p className="carousels-muted">Текст публикации не получен.</p>}
            {translationUnavailable && <p className="carousels-translation-status">Перевод сейчас недоступен.</p>}
            {translationError && <p className="carousels-translation-error" role="alert">{translationError}</p>}
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
  const hidden = useHiddenAuthors()
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
  const [lastHidden, setLastHidden] = useState(null)
  const [searchDraft, setSearchDraft] = useState(() => searchParams.get('author') || '')
  const [searchFocused, setSearchFocused] = useState(false)
  const [searchError, setSearchError] = useState('')
  const excludedAuthors = useMemo(() => hidden.authors.map(item => item.username), [hidden.authors])
  const excluded = useMemo(() => new Set(excludedAuthors), [excludedAuthors])

  const author = searchParams.get('author')?.trim() || ''
  useEffect(() => { setSearchDraft(author) }, [author])
  const period = carouselPeriod(searchParams)
  const rangeKey = JSON.stringify(carouselRangeDraft(searchParams))
  const [rangeDraft, setRangeDraft] = useState(() => carouselRangeDraft(searchParams))
  const [rangeError, setRangeError] = useState('')
  useEffect(() => { setRangeDraft(JSON.parse(rangeKey)); setRangeError('') }, [rangeKey])
  const ranges = useMemo(() => {
    const draft = JSON.parse(rangeKey)
    return Object.fromEntries(CAROUSEL_RANGE_KEYS.map(key => [key, key.startsWith('date_') ? (period === 'custom' ? draft[key] || null : null) : draft[key] === '' ? null : Number(draft[key])]))
  }, [rangeKey, period])
  const favoriteOnly = favoriteFilterEnabled(searchParams)
  const sourceAccount = searchParams.get('source') === 'neyro_set7' ? 'neyro_set7' : null
  const sort = SORTS.has(searchParams.get('sort')) ? searchParams.get('sort') : 'likes'
  const order = searchParams.get('order') === 'asc' ? 'asc' : 'desc'
  const limit = searchParams.get('limit') === '100' ? 100 : 50
  const detailId = searchParams.get('id')
  const includeAuthors = !isLiveCarouselPeriod(period) && favoriteOnly ? favorites.authors : null
  const listParams = useMemo(() => ({ ...ranges, period, favorites_only: favoriteOnly, author, sort, order, limit, exclude_authors: excludedAuthors, include_authors: includeAuthors, source_account: sourceAccount }), [ranges, period, favoriteOnly, author, sort, order, limit, excludedAuthors, includeAuthors, sourceAccount])
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
    if (!next.has('period') && carouselPeriod(next) === '4m') next.set('period', '4m')
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

  const applyRanges = event => {
    event.preventDefault()
    const customDates = Boolean(rangeDraft.date_from || rangeDraft.date_to || period === 'custom')
    const result = validateCarouselRanges(rangeDraft, customDates)
    setRangeError(result.error)
    if (result.error) return
    changePage({ ...result.values, ...(customDates ? { period: 'custom' } : {}), id: null, offset: 0 })
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
    updateQuery({ author: username || null, favorite: '1', id: null, offset: 0 })
  }
  const toggleAllFavorites = () => {
    setSearchDraft('')
    setSearchFocused(false)
    setSearchError('')
    restoreScroll.current = pageRef.current?.scrollTop || 0
    updateQuery(toggleFavoriteFilter(searchParams))
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

  const changeHidden = async (username, hide) => {
    const scrollTop = pageRef.current?.scrollTop || 0
    if (await hidden.setHidden(username, hide)) {
      restoreScroll.current = scrollTop
      setLastHidden(hide ? username : null)
      updateQuery({ offset: 0, id: null })
    }
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
  const coverage = data?.coverage
  const collection = data?.collection
  const checked = collection?.checked
  const target = collection?.target
  const profile = author && data?.profiles?.find(p => p.username?.toLowerCase() === author.toLowerCase())
  const authorCount = profile?.carousels
  const isPartial = coverage?.status === 'partial' || (collection && collection.status !== 'finished')
  const authorUnknown = author && authorCount == null
  const items = (hidden.loading ? [] : data?.items || []).filter(item => !excluded.has(item.username?.toLowerCase()))
  const listItem = items.find(item => String(item.id) === detailId)
  const selectedIds = new Set(selected.items.map(item => item.id))

  return <main className="carousels-page" ref={pageRef}>
    <div className="carousels-content">
      <button className="carousels-back" type="button" onClick={backToRadar}>← {backPath ? 'К каталогу авторов' : 'Радар'}</button>
      <header className="carousels-heading">
        <div>
          <span className="carousels-eyebrow">Радар · карусели</span>
          <h1>{author ? `Карусели @${author}` : sourceAccount ? 'Карусели NEYRO_SET7' : isLiveCarouselPeriod(selectedPeriod) && favoriteOnly ? 'Карусели избранных' : 'Все карусели'}</h1>
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
        {collection && !(isLiveCarouselPeriod(selectedPeriod) && !favoriteOnly) && <div className="carousels-progress">
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

      {coverage && <div className={`carousels-coverage${coverage.status === 'partial' ? ' is-partial' : ''}`} role="status">
        <strong>{coverage.status === 'complete' ? 'Покрытие периода подтверждено' : 'История загружена частично'}</strong>
        <span>{count(coverage.complete_authors)} / {count(coverage.total_authors)} авторов с подтверждённым покрытием.</span>
        <span>Доступные границы: {coverage.available_from || 'ещё нет'} — {coverage.available_to || 'ещё нет'}.</span>
        {coverage.status === 'partial' && <span>Результат может пополняться; отсутствие карточек не означает отсутствие публикаций.</span>}
      </div>}
      {data?.collected_at && <p className="carousels-muted">Счётчики по последнему сбору: {date(data.collected_at)} МСК. Фильтры не запускают повторный сбор.</p>}

      <div className="carousels-controls">
        <label>Период
          <select aria-label="Период каруселей" value={selectedPeriod || ''} onChange={event => { if (event.target.value === 'custom') { changePage({ period: 'custom', date_from: data.start_date, date_to: data.end_date, id: null, offset: 0 }); return } changePage({ period: event.target.value, date_from: null, date_to: null, id: null, offset: 0 }) }}>
            {!period && !data && <option value="">Автоматически</option>}
            <option value="7d">7 дней · 13–19 октября</option>
            <option value="14d">Последние 14 дней</option>
            <option value="30d">Последние 30 дней</option>
            <option value="4m">Последние 4 месяца</option>
            <option value="custom" disabled={!data?.start_date || !data?.end_date}>Произвольные даты</option>
            <option value="archive14d">Архив 6–19 октября</option>
          </select>
        </label>
        <button type="button"
          className={`carousels-archive-shortcut${selectedPeriod === 'archive14d' && !favoriteOnly && !author && !sourceAccount ? ' is-active' : ''}`}
          aria-pressed={selectedPeriod === 'archive14d' && !favoriteOnly && !author && !sourceAccount}
          title="Карусели 586 авторов за 6–19 октября"
          onClick={() => changePage({ period: 'archive14d', favorite: '0', author: null, source: null, id: null, offset: 0 })}>
          Архив · 586 авторов
        </button>
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
        {!isLiveCarouselPeriod(selectedPeriod) && <label className="carousels-favorite-toggle">
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
            <option value="desc">{ORDER_LABELS[sort].desc}</option>
            <option value="asc">{ORDER_LABELS[sort].asc}</option>
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

      <form className="carousels-range-form" onSubmit={applyRanges} noValidate>
        <fieldset><legend>Даты публикации · Москва</legend>
          <label>С<input type="date" aria-label="Дата с" value={rangeDraft.date_from} onChange={event => setRangeDraft(value => ({ ...value, date_from: event.target.value }))} /></label>
          <label>По<input type="date" aria-label="Дата по" value={rangeDraft.date_to} onChange={event => setRangeDraft(value => ({ ...value, date_to: event.target.value }))} /></label>
        </fieldset>
        {[['likes', 'Лайки'], ['comments', 'Комментарии']].map(([key, title]) => <fieldset key={key}><legend>{title}</legend>
          {['min', 'max'].map((bound, index) => <label key={bound}>{index ? 'До' : 'От'}<input type="number" min="0" step="1" inputMode="numeric" aria-label={`${title} ${index ? 'до' : 'от'}`} placeholder="Любое" value={rangeDraft[`${bound}_${key}`]} onChange={event => setRangeDraft(value => ({ ...value, [`${bound}_${key}`]: event.target.value }))} /></label>)}
        </fieldset>)}
        <div className="carousels-range-actions"><button type="submit">Применить фильтры</button>
          <button type="button" onClick={() => { setRangeError(''); setRangeDraft(Object.fromEntries(CAROUSEL_RANGE_KEYS.map(key => [key, '']))); changePage({ ...Object.fromEntries(CAROUSEL_RANGE_KEYS.map(key => [key, null])), period: period === 'custom' ? '4m' : period, id: null, offset: 0 }) }}>Сбросить диапазоны</button></div>
        {rangeError && <p className="carousels-range-error" role="alert">{rangeError}</p>}
      </form>

      <section className="carousels-favorites-list" aria-label="Избранные авторы">
        <div className="carousels-favorites-heading">
          <h2><button type="button" className="carousels-favorites-toggle" aria-expanded={favoritesExpanded}
            aria-controls="carousels-favorites-content" onClick={toggleFavoritesExpanded}>
            <span aria-hidden="true">{favoritesExpanded ? '▾' : '▸'}</span> Избранные авторы ({favorites.authors.length})
            <small>{favoritesExpanded ? 'Скрыть' : 'Показать'}</small>
          </button></h2>
          <button type="button" className={`carousels-favorites-reset${favoriteOnly && !author ? ' is-active' : ''}`}
            aria-pressed={favoriteOnly && !author} onClick={toggleAllFavorites}>Все избранные</button>
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
            <span>@{row.username}</span><small>{FAVORITE_STATUS[row.status] || 'Ожидает'}{row.last_checked_at ? ` · ${date(row.last_checked_at)} МСК` : ' · ещё не проверен'}</small>{row.error && <small>{row.error}</small>}{row.history_status && <small>История: {({ pending: 'ожидает загрузки', queued: 'в очереди', running: 'загружается', collecting: 'загружается', complete: 'покрытие подтверждено', partial: 'частично', cooldown: 'пауза API', unavailable: 'профиль недоступен', error: 'ошибка', failed: 'ошибка' })[row.history_status] || 'статус уточняется'}{row.history_from ? ` · ${date(row.history_from)} — ${date(row.history_to)}` : ''}</small>}{row.history_error && <small>{row.history_error}</small>}
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
        <p className="carousels-muted">Общий список на всех устройствах.</p>
        {hidden.loading ? <p role="status">Загружаем общий список…</p> : hidden.authors.length ? <ul>{hidden.authors.map(item => <li key={item.username}>
          <span>@{item.username}</span><small>{date(item.hidden_at)}</small>
          <button type="button" disabled={hidden.loading || hidden.busy.has(item.username)} onClick={() => changeHidden(item.username, false)}>Вернуть в сетку</button>
        </li>)}</ul> : <p className="carousels-muted">Скрытых авторов пока нет.</p>}
      </details>
      {hidden.warning && <p className="carousels-error" role="alert">{hidden.warning}</p>}
      {hidden.error && <p className="carousels-error" role="alert">{hidden.error}<button type="button" onClick={hidden.refresh}>Повторить</button></p>}
      {lastHidden && <div className="carousels-hide-notice" role="status">Автор @{lastHidden} скрыт.
        <button type="button" disabled={hidden.loading || hidden.busy.has(lastHidden.toLowerCase())} onClick={() => changeHidden(lastHidden, false)}>Отменить</button>
      </div>}
      {author && excluded.has(author.toLowerCase().replace(/^@/, '')) && <div className="carousels-hide-notice">
        Автор @{author.replace(/^@/, '')} в списке скрытых.
        <button type="button" disabled={hidden.loading || hidden.busy.has(author.toLowerCase().replace(/^@/, ''))} onClick={() => changeHidden(author, false)}>Вернуть в сетку</button>
      </div>}

      {error && <div className="carousels-error" role="alert">{error}<button type="button" onClick={refresh}>Повторить</button></div>}
      {(hidden.loading || (loading && !data)) && <p className="carousels-loading" role="status">Загружаем карусели…</p>}
      {!hidden.loading && !loading && !error && !items.length && <p className="carousels-empty">{isLiveCarouselPeriod(period) && favoriteOnly ? favoriteLoading ? 'Проверяем общий список избранных…' : !favorites.authors.length ? 'Добавьте профиль в общий список избранных.' : data?.coverage?.status === 'partial' ? 'История загружена не полностью. За выбранные даты и с этими фильтрами пока ничего не найдено; результат может пополниться.' : 'Авторы проверены. За выбранный период и с текущими фильтрами каруселей нет.' : favoriteOnly && !favorites.authors.length ? 'Список избранных авторов пуст. Добавьте авторов звёздой на карточках.' : authorUnknown ? 'Данные каруселей этого автора пока недоступны.' : 'Каруселей по выбранному фильтру пока нет.'}</p>}
      {isLiveCarouselPeriod(period) && !favoriteOnly && <p className="carousels-muted">Уже собранные карусели всех авторов за текущее окно. Новый сбор по всем авторам не запускается.</p>}
      {!!items.length && <div className="carousels-grid">
        {items.map(item => <div className="carousels-tile" key={item.id}><CarouselCard item={item} onOpen={() => updateQuery({ id: item.id, period: selectedPeriod })}
          coverContent={<CarouselCoverTranslation id={item.id} period={selectedPeriod} rootRef={pageRef} />} /><button type="button" className={`carousels-select${selectedIds.has(String(item.id)) ? ' is-selected' : ''}`}
          disabled={selected.loading || selected.busy.has(String(item.id))}
          aria-pressed={selectedIds.has(String(item.id))}
          aria-label={selectedIds.has(String(item.id)) ? `Убрать карусель @${item.username} из отобранных` : `Отобрать карусель @${item.username}`}
          title={selectedIds.has(String(item.id)) ? 'Убрать карусель из отобранных' : 'Отобрать карусель'}
          onClick={() => changeSelected(item)}><span aria-hidden="true">{selectedIds.has(String(item.id)) ? '♥' : '♡'}</span>
        </button><button type="button" className="carousels-hide-author"
          aria-label={`Скрыть автора @${item.username}`} title={`Скрыть все карусели @${item.username}`}
          disabled={hidden.loading || hidden.busy.has(item.username?.toLowerCase())} onClick={() => changeHidden(item.username, true)}>
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
    {detailId && <Drawer period={selectedPeriod} key={`${period || 'auto'}-${detailId}`} id={detailId} listItem={listItem} excluded={excluded} selected={selectedIds.has(String(detailId))} selectionBusy={selected.loading || selected.busy.has(String(detailId))} onToggleSelection={changeSelected} onClose={() => updateQuery({ id: null })} />}
  </main>
}
