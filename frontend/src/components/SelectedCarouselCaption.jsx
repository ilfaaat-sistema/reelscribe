import { useEffect, useRef, useState } from 'react'
import { getCarousel } from '../api/carousels'
import { carouselCaptionView } from '../pages/carouselTranslation'

// Читаем только сохранённый перевод видимых карточек. Генерация остаётся в общем просмотре.
export default function SelectedCarouselCaption({ item, rootRef }) {
  const targetRef = useRef(null)
  const [details, setDetails] = useState(null)
  useEffect(() => {
    const controller = new AbortController()
    let started = false
    setDetails(null)
    const read = () => {
      if (started) return
      started = true
      getCarousel(item.id, controller.signal, item.source_period)
        .then(value => { if (!controller.signal.aborted) setDetails(value) })
        .catch(() => { /* Сохранённый оригинал остаётся доступным при ошибке чтения. */ })
    }
    const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { read(); observer.disconnect() }
    }, { root: rootRef?.current || null })
    if (observer && targetRef.current) observer.observe(targetRef.current)
    else read()
    return () => { controller.abort(); observer?.disconnect() }
  }, [item.id, item.source_period, rootRef])
  const { hasTranslation, displayedCaption } = carouselCaptionView(details || item.snapshot, { detailsLoaded: !!details, showTranslation: true })
  return <span ref={targetRef} className="selected-card-caption" aria-label={hasTranslation ? 'Описание на русском' : 'Оригинал описания'}>
    <span>{displayedCaption || 'Без описания'}</span>
  </span>
}
