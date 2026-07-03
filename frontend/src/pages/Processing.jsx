import { useState, useEffect, useRef } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
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

export default function Processing() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const [prog, setProg] = useState({ loaded: 0, total: 0, failed: 0, done_ids: [], failed_ids: [] })
  const [feed, setFeed] = useState([])
  const [done, setDone] = useState(false)
  const [pollError, setPollError] = useState(null)
  const [retryTick, setRetryTick] = useState(0)
  const seenDoneRef = useRef(new Set())
  const seenFailRef = useRef(new Set())
  const doneRef = useRef(false)
  const failStreakRef = useRef(0)

  useEffect(() => {
    if (!sessionId) return
    let timer
    let stopped = false

    async function poll() {
      try {
        const p = await getProgress(sessionId)
        if (stopped) return
        failStreakRef.current = 0
        setPollError(null)
        setProg(p)

        const newDone = (p.done_ids || []).filter(id => !seenDoneRef.current.has(id))
        newDone.forEach(id => {
          seenDoneRef.current.add(id)
          setFeed(f => [{ sc: id.slice(0, 8) + '…', state: 'done' }, ...f].slice(0, 15))
        })

        const newFailed = (p.failed_ids || []).filter(id => !seenFailRef.current.has(id))
        newFailed.forEach(id => {
          seenFailRef.current.add(id)
          setFeed(f => [{ sc: id.slice(0, 8) + '…', state: 'failed' }, ...f].slice(0, 15))
        })

        const finished = p.total > 0 && (p.loaded + p.failed) >= p.total
        if (finished && !doneRef.current) {
          doneRef.current = true
          setDone(true)
        }
      } catch (e) {
        if (stopped) return
        failStreakRef.current += 1
        if (failStreakRef.current >= 5) {
          setPollError(e.message || 'неизвестная ошибка')
        }
      }
      if (!doneRef.current && !stopped) {
        timer = setTimeout(poll, 2000)
      }
    }

    poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [sessionId, retryTick])

  const pct = prog.total > 0 ? Math.round((prog.loaded + prog.failed) / prog.total * 100) : 0
  const inProgress = prog.total > 0 ? Math.max(0, prog.total - prog.loaded - prog.failed) : 0

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

        {pollError && (
          <div className="errbanner">
            ⚠ Не удаётся получить статус обработки: {pollError}
            <button className="btn sm ghost" onClick={() => { failStreakRef.current = 0; setRetryTick(t => t + 1) }}>
              Повторить
            </button>
          </div>
        )}

        <div className="pbar">
          <i style={{width: pct + '%'}} />
        </div>
        <div className="pcount">{prog.loaded + prog.failed} / {prog.total}</div>

        <div className="pstats">
          <div className="pstat">
            <div className="v">{inProgress}</div>
            <div className="l">⏳ в обработке</div>
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

        {done ? (
          <button className="btn" style={{marginTop:20}} onClick={() => navigate(`/results/${sessionId}`)}>
            Перейти к результатам →
          </button>
        ) : prog.loaded > 0 && (
          <button className="btn ghost" style={{marginTop:20}} onClick={() => navigate(`/results/${sessionId}`)}>
            Посмотреть готовые ({prog.loaded}) → · остальное догрузится в фоне
          </button>
        )}
      </div>
    </div>
  )
}
