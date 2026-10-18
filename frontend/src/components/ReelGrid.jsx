import { useState } from 'react'
import { thumbUrl } from '../api/client'
import { fmtV } from '../lib/utils'
import './reel-grid.css'

function formatCount(value) {
  return value == null ? '—' : Number(value) === 0 ? '0' : fmtV(value).replace(/\s+/g, '')
}

function formatMultiplier(value) {
  return value == null ? '—' : `${Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 1, useGrouping: false })}×`
}

function ReelCover({ shortcode }) {
  const [failed, setFailed] = useState(false)

  return (
    <span className="reel-grid-cover">
      {shortcode && !failed ? (
        <img
          className="reel-grid-image"
          src={thumbUrl(shortcode, 'm')}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="reel-grid-placeholder" aria-hidden="true">Нет обложки</span>
      )}
    </span>
  )
}

function Metric({ icon, label, value, multiplier = false }) {
  const formatted = multiplier ? formatMultiplier(value) : formatCount(value)
  const fullValue = value == null ? 'нет данных' : `${value}${multiplier ? '×' : ''}`
  const description = `${label}: ${fullValue}`

  return (
    <span className="reel-grid-metric" role="img" title={description} aria-label={description}>
      <span className="reel-grid-icon" aria-hidden="true">{icon}</span>
      <span className="reel-grid-value" aria-hidden="true">{formatted}</span>
    </span>
  )
}

export default function ReelGrid({ reels, onOpen }) {
  if (!reels.length) {
    return <p className="reel-grid-empty">Ничего не найдено</p>
  }

  return (
    <div className="reel-grid">
      {reels.map(reel => (
        <button
          key={reel.id}
          type="button"
          className="reel-grid-card"
          aria-label={`Открыть ${reel.author_handle ? `@${reel.author_handle}` : reel.shortcode || reel.id}`}
          onClick={() => onOpen(reel.id)}
        >
          <ReelCover key={reel.shortcode} shortcode={reel.shortcode} />
          <span className="reel-grid-metrics">
            <Metric icon="👁" label="Просмотры" value={reel.views} />
            <Metric icon="❤️" label="Лайки" value={reel.likes} />
            <Metric icon="💬" label="Комментарии" value={reel.comments} />
            <Metric icon="🔥" label="Залётность" value={reel.er} multiplier />
          </span>
        </button>
      ))}
    </div>
  )
}
