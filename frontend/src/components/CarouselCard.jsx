import CarouselCover from './CarouselCover'

const numbers = new Intl.NumberFormat('ru-RU')
const dates = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
})
const count = value => value == null || !Number.isFinite(Number(value)) ? '—' : numbers.format(Number(value))
const date = value => !value || !Number.isFinite(Date.parse(value)) ? '—' : dates.format(new Date(value))

// Обе ленты используют одинаковую обложку, метрики и геометрию карточки.
export default function CarouselCard({ item, onOpen, coverContent, children }) {
  return <button className="carousels-card" type="button" onClick={onOpen}
    aria-label={`Открыть карусель @${item.username || 'автор неизвестен'}`}>
    <CarouselCover item={item} />
    {coverContent}
    <span className="carousels-card-info">
      <strong>@{item.username || '—'}</strong>
      <small>{date(item.timestamp)} МСК</small>
      <span className="carousels-card-stats"><span>♥ {count(item.likes)}</span><span>◌ {count(item.comments)}</span></span>
      {children}
    </span>
  </button>
}
