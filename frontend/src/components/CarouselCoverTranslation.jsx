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
  const ready = item.status === 'ready' && Boolean(item.text_ru.trim())
  const text = ready ? item.text_ru
    : item.status === 'limit_reached' && item.retry_after
      ? `Суточный лимит исчерпан. Проверка после ${retryDateFormat.format(new Date(item.retry_after))} МСК`
      : LABELS[item.status] || LABELS.error
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
    <span ref={targetRef} className={`carousels-cover-translation${ready ? ' is-ready' : ''}`}>
      <span className="carousels-cover-translation-lines">{text}</span>
    </span>
  )
}
