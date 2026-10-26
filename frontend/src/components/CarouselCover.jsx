import { useEffect, useState } from 'react'
import { thumbUrl } from '../api/client'

export default function CarouselCover({ item, large = false }) {
  const direct = [item?.media_url, item?.thumbnail_url].find(value => value?.startsWith('https://')) || null
  const fallback = /^[\w-]+$/.test(item?.shortcode || '') ? thumbUrl(item.shortcode, 'm') : null
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
      ) : <span className="carousels-placeholder">Обложка недоступна{!large && <small>Открыть просмотр слайдов →</small>}</span>}
      {!large && <span className="carousels-album" aria-hidden="true">▣</span>}
    </span>
  )
}

