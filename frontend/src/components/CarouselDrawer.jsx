import { useCallback, useEffect, useRef, useState } from 'react'
import Cover from './CarouselCover'
import CarouselCoverTranslation from './CarouselCoverTranslation'
import { getCarousel, translateCarousel } from '../api/carousels'
import { carouselCaptionView, isCurrentCarouselRequest, isCurrentCarouselTranslation } from '../pages/carouselTranslation'
const numberFormat = new Intl.NumberFormat('ru-RU')
const dateFormat = new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })

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

function Metric({ label, value }) {
  return <span className="carousels-metric"><b>{count(value)}</b><small>{label}</small></span>
}

export default function CarouselDrawer({ period, id, listItem, onClose, excluded, selected, selectionBusy, onToggleSelection, selectedAt }) {
  const [item, setItem] = useState(listItem || null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [showTranslation, setShowTranslation] = useState(true)
  const [translationBusy, setTranslationBusy] = useState(false)
  const [translationError, setTranslationError] = useState('')
  const [translationRefreshRequired, setTranslationRefreshRequired] = useState(false)
  const [translationRefreshBusy, setTranslationRefreshBusy] = useState(false)
  const closeRef = useRef(null)
  const panelRef = useRef(null)
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
    const previous = document.activeElement
    closeRef.current?.focus()
    const onKey = event => {
      if (event.key === 'Escape') onClose()
      if (event.key !== 'Tab') return
      const controls = [...(panelRef.current?.querySelectorAll('button:not(:disabled), a[href], iframe, summary') || [])]
      const first = controls[0]; const last = controls.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('keydown', onKey); previous?.focus() }
  }, [onClose])

  const isHidden = excluded?.has(item?.username?.toLowerCase())
  const link = instagramUrl(item)
  const canEmbed = /^[\w-]+$/.test(item?.shortcode || '')
  const { hasTranslation, displayedCaption, canRequest, unavailable: translationUnavailable } = carouselCaptionView(item, { detailsLoaded: !loading, showTranslation, refreshRequired: translationRefreshRequired })

  return (
    <div className="carousels-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
      <aside ref={panelRef} className="carousels-drawer" role="dialog" aria-modal="true" aria-label="Карусель Instagram">
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
              <span className="carousels-media-date">{date(item.timestamp)} МСК{selectedAt && <><br />Отобрано: {date(selectedAt)} МСК</>}</span>
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

