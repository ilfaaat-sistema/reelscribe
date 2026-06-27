import { useState, useEffect, useRef } from 'react'
import { getProgress } from '../api/client'

function FeedItem({ item }) {
  const type = item.type || 'reel'
  const cls = type === 'reel' ? 't-reel' : type === 'tv' ? 't-tv' : 't-post'
  const icon = item.state === 'done' ? '✓' : item.state === 'failed' ? '✗' : '⏳'
  const color = item.state === 'done' ? 'var(--green)' : item.state === 'failed' ? 'var(--rose)' : 'var(--amber)'
  return (
    <div className="f">
      <span className={`tag ${cls}`}>{type}</span>
      <span className="sc">{item.sc || item.shortcode}</span>
      <span style={{color,fontFamily:'var(--mono)',fontSize:12}}>{icon}</span>
    </div>
  )
}

export default function Processing({ sessionId, total, onDone }) {
  const [prog, setProg] = useState({ loaded: 0, total: total || 0, failed: 0 })
  const [feed, setFeed] = useState([])
  const [done, setDone] = useState(false)
  const seenRef = useRef(new Set())
  const doneRef = useRef(false)

  useEffect(() => {
    if (!sessionId) return
    let timer

    async function poll() {
      try {
        const p = await getProgress(sessionId)
        setProg(p)
        const newIds = (p.done_ids || []).filter(id => !seenRef.current.has(id))
        newIds.forEach(id => {
          seenRef.current.add(id)
          setFeed(f => [{ sc: id.slice(0, 8) + '…', state: 'done' }, ...f].slice(0, 12))
        })
        const finished = p.total > 0 && (p.loaded + p.failed) >= p.total
        if (finished && !doneRef.current) {
          doneRef.current = true
          setDone(true)
        }
      } catch (_) {}
      if (!doneRef.current) {
        timer = setTimeout(poll, 2000)
      }
    }

    poll()
    return () => clearTimeout(timer)
  }, [sessionId])

  const pct = prog.total > 0 ? Math.round((prog.loaded + prog.failed) / prog.total * 100) : 0
  const downloading = prog.total > 0 ? Math.max(0, prog.total - prog.loaded - prog.failed) : 0

  return (
    <div className="procview">
      <div className="proc">
        <div className="bigeq">
          <i/><i/><i/><i/><i/><i/>
        </div>
        <h2>{done ? 'Готово!' : 'Обработка…'}</h2>
        <p className="psub">
          {done
            ? `Обработано ${prog.loaded} рилс, ошибок: ${prog.failed}`
            : `Расшифровываем речь и собираем метрики · ${pct}%`}
        </p>

        <div className="pbar">
          <i style={{width: pct + '%'}} />
        </div>
        <div className="pcount">{prog.loaded + prog.failed} / {prog.total}</div>

        <div className="pstats">
          <div className="pstat">
            <div className="v">{downloading}</div>
            <div className="l">⏬ скачивание</div>
          </div>
          <div className="pstat">
            <div className="v">{Math.max(0, prog.total - prog.loaded - prog.failed - downloading)}</div>
            <div className="l">🎙 распознавание</div>
          </div>
          <div className="pstat">
            <div className="v" style={{color:'var(--green)'}}>{prog.loaded}</div>
            <div className="l">✓ готово</div>
          </div>
          <div className="pstat">
            <div className="v" style={{color: prog.failed ? 'var(--rose)' : undefined}}>{prog.failed}</div>
            <div className="l">✗ ошибки</div>
          </div>
        </div>

        {feed.length > 0 && (
          <div className="feed">
            {feed.map((item, i) => <FeedItem key={i} item={item} />)}
          </div>
        )}

        <div className="throttle">⏱ Троттлинг: 1–2 параллельно, паузы между запросами</div>

        {done && (
          <button className="btn" style={{marginTop:20}} onClick={onDone}>
            Перейти к результатам →
          </button>
        )}
      </div>
    </div>
  )
}
