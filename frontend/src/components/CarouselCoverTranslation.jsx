import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import useCarouselCoverTranslations from '../hooks/useCarouselCoverTranslations.js'

const retryDateFormat = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
})

const LABELS = {
  missing: 'Перевод обложки пока не получен',
  running: 'Переводим крупный текст обложки…',
  no_text: 'На обложке нет крупного текста',
  unreadable: 'Не удалось прочитать текст обложки',
  image_unavailable: 'Изображение обложки недоступно',
  error: 'Перевод обложки недоступен',
  busy: 'Сервис перевода временно занят',
  limit_reached: 'Лимит перевода обложек исчерпан',
  disabled: 'Перевод обложек выключен',
}

export default function CarouselCoverTranslation({ id, period, rootRef, full = false }) {
  const { item, targetRef } = useCarouselCoverTranslations(id, period, rootRef)
  const popupId = useId()
  const popupRef = useRef(null)
  const hideTimer = useRef(null)
  const [position, setPosition] = useState(null)
  const ready = item.status === 'ready' && Boolean(item.text_ru.trim())
  const text = ready ? item.text_ru
    : item.status === 'limit_reached' && item.retry_after
      ? `Суточный лимит исчерпан. Проверка после ${retryDateFormat.format(new Date(item.retry_after))} МСК`
      : LABELS[item.status] || LABELS.error
  useEffect(() => {
    if (full || !ready) return undefined
    const target = targetRef.current
    const card = target?.closest('.carousels-card')
    if (!target || !card) return undefined
    const originalDescription = card.getAttribute('aria-describedby')
    const cancelHide = () => { clearTimeout(hideTimer.current) }
    const close = () => {
      cancelHide()
      setPosition(null)
      if (originalDescription) card.setAttribute('aria-describedby', originalDescription)
      else card.removeAttribute('aria-describedby')
    }
    const open = () => {
      cancelHide()
      window.dispatchEvent(new CustomEvent('carousel-cover-popup', { detail: popupId }))
      card.setAttribute('aria-describedby', [originalDescription, popupId].filter(Boolean).join(' '))
      const rect = target.getBoundingClientRect()
      const width = Math.min(360, window.innerWidth - 24)
      const maxHeight = Math.min(400, window.innerHeight - 24)
      const below = window.innerHeight - rect.bottom - 12
      const top = below >= Math.min(180, maxHeight) ? rect.bottom + 4 : Math.max(12, rect.top - maxHeight - 4)
      setPosition({ left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)), top,
        width, maxHeight: Math.min(maxHeight, window.innerHeight - top - 12) })
    }
    const leave = () => { cancelHide(); hideTimer.current = setTimeout(close, 120) }
    const enter = event => { if (event.pointerType !== 'touch') open() }
    const keydown = event => { if (event.key === 'Escape') close() }
    const other = event => { if (event.detail !== popupId) close() }
    const scroll = event => { if (!popupRef.current?.contains(event.target)) close() }
    const focus = () => { if (card.matches(':focus-visible')) open() }
    target.addEventListener('pointerenter', enter)
    target.addEventListener('pointerleave', leave)
    card.addEventListener('focus', focus)
    card.addEventListener('blur', close)
    card.addEventListener('click', close)
    window.addEventListener('keydown', keydown)
    window.addEventListener('carousel-cover-popup', other)
    window.addEventListener('scroll', scroll, true)
    window.addEventListener('resize', close)
    // Мост к portal нужен только для перехода указателя; новый tab stop не создаётся.
    target._coverPopupKeep = cancelHide
    target._coverPopupLeave = leave
    return () => {
      close()
      target.removeEventListener('pointerenter', enter)
      target.removeEventListener('pointerleave', leave)
      card.removeEventListener('focus', focus)
      card.removeEventListener('blur', close)
      card.removeEventListener('click', close)
      window.removeEventListener('keydown', keydown)
      window.removeEventListener('carousel-cover-popup', other)
      window.removeEventListener('scroll', scroll, true)
      window.removeEventListener('resize', close)
      delete target._coverPopupKeep
      delete target._coverPopupLeave
    }
  }, [full, ready, id, period, popupId, targetRef])

  if (full) return (
    <section ref={targetRef} className="carousels-cover-translation-full" aria-label="Перевод обложки">
      <h2>Текст обложки · перевод</h2>
      <p className={ready ? 'carousels-cover-translation-text' : 'carousels-muted'}>{text}</p>
      {ready && item.source_text && <details>
        <summary>Оригинал крупного текста</summary>
        <p className="carousels-cover-translation-text">{item.source_text}</p>
      </details>}
    </section>
  )
  return (
    <span ref={targetRef} aria-describedby={position && ready ? popupId : undefined} className={`carousels-cover-translation${ready ? ' is-ready' : ''}`}>
      <span className="carousels-cover-translation-lines">{text}</span>
      {position && ready && createPortal(<span id={popupId} ref={popupRef} role="tooltip"
        className="carousels-cover-translation-popup" style={position}
        onPointerEnter={() => targetRef.current?._coverPopupKeep?.()}
        onPointerLeave={() => targetRef.current?._coverPopupLeave?.()}>{text}</span>, document.body)}
    </span>
  )
}
