import { useState, useEffect, useRef } from 'react'
import { getRefreshCurrent, getRefreshEstimate, startRefresh } from '../../api/radar'
import { fmtAgo, fmtRub } from './format'

// Полоса «Обновить» над вкладками Лента и Источники (ТЗ 10).
// Каждый GET /refresh/current двигает конвейер обновления на бэке, поэтому пока
// статус running — опрашиваем его каждые 2 с цепочкой setTimeout (как опрос разового сбора).

export default function RefreshBar({ sourcesKey, onDone }) {
  const [current, setCurrent] = useState(null)   // ответ /refresh/current (или null до загрузки)
  const [estimate, setEstimate] = useState(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState(null)
  const [pollError, setPollError] = useState(null)

  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone
  const prevStatusRef = useRef(null)

  const loadEstimate = () => {
    getRefreshEstimate()
      .then(setEstimate)
      .catch(() => setEstimate(null))
  }

  // При монтировании: текущее состояние + оценка
  useEffect(() => {
    let cancelled = false
    getRefreshCurrent()
      .then(data => {
        if (cancelled) return
        prevStatusRef.current = data.status
        setCurrent(data)
      })
      .catch(e => { if (!cancelled) setPollError(e.message) })
    loadEstimate()
    return () => { cancelled = true }
  }, [])

  // Состав источников изменился — оценка устарела
  useEffect(() => {
    if (sourcesKey) loadEstimate()
  }, [sourcesKey])

  const running = current?.status === 'running'

  // Опрос идущего обновления: до 3 сетевых ошибок подряд, таймер чистится при размонтировании
  useEffect(() => {
    if (!running) return
    let stopped = false
    let timeoutId = null
    let errorStreak = 0
    const poll = async () => {
      if (stopped) return
      try {
        const data = await getRefreshCurrent()
        errorStreak = 0
        if (stopped) return
        setCurrent(data)
        if (data.status !== 'running') return
      } catch (e) {
        errorStreak += 1
        if (errorStreak >= 3) {
          if (!stopped) setPollError(e.message)
          return
        }
      }
      if (!stopped) timeoutId = setTimeout(poll, 2000)
    }
    timeoutId = setTimeout(poll, 2000)
    return () => { stopped = true; clearTimeout(timeoutId) }
  }, [running])

  // Переход running → done|error: данные в БД новые, оценка устарела
  useEffect(() => {
    const prev = prevStatusRef.current
    const now = current?.status
    prevStatusRef.current = now
    if (prev === 'running' && (now === 'done' || now === 'error')) {
      loadEstimate()
      onDoneRef.current?.()
    }
  }, [current?.status])

  const start = async () => {
    setStartError(null)
    setPollError(null)
    setStarting(true)
    try {
      await startRefresh(false)
      const data = await getRefreshCurrent()
      setCurrent(data)
    } catch (e) {
      setStartError(e.message)
    } finally {
      setStarting(false)
    }
  }

  // опрос сдался после 3 ошибок — полосу прогресса не держим
  const showRunning = running && !pollError
  const nothingToRefresh = estimate != null && estimate.to_refresh === 0
  const disabled = showRunning || starting || nothingToRefresh

  let state = 'idle'
  if (showRunning) state = 'running'
  else if (startError || pollError || current?.status === 'error') state = 'error'
  else if (current?.status === 'done') state = 'done'

  const errors = Array.isArray(current?.errors) ? current.errors : []
  const pct = current?.batch_total
    ? Math.min(100, Math.round((current.batch_done / current.batch_total) * 100))
    : 0

  let estText
  if (!estimate) {
    estText = <span>…</span>
  } else if (nothingToRefresh) {
    estText = estimate.sources_total === 0
      ? 'Добавьте источники во вкладке «Источники»'
      : 'Все источники обновлены недавно'
  } else {
    estText = (
      <>
        ≈ <b className="num">{fmtRub(estimate.total_rub)}</b> · {estimate.to_refresh} источников
        {estimate.deep > 0 && ` (${estimate.deep} новых)`}
      </>
    )
  }

  const lastAt = current?.finished_at || (current?.status === 'running' ? null : current?.started_at)

  return (
    <div className="wrap rs-wide">
      <section className="rs-refresh" data-state={state}>
        <div className="rs-refresh-main">
          <button className="btn btn-primary rs-refresh-btn" onClick={start} disabled={disabled}>
            {showRunning || starting ? 'Обновляю…' : 'Обновить'}
          </button>
          <div className="rs-refresh-info">
            <span className="rs-refresh-est">{estText}</span>
            {lastAt && !showRunning && <span className="rs-refresh-last">Обновлено {fmtAgo(lastAt)}</span>}
          </div>
        </div>

        {showRunning && (
          <div className="rs-refresh-progress">
            <div className="p-track">
              <div className="p-fill p-fill-running" style={{ width: pct + '%' }} />
            </div>
            <div className="p-text rs-refresh-stage">{current.stage_label || 'Обновляю…'}</div>
          </div>
        )}

        {!showRunning && (startError || pollError || current?.status === 'done' || current?.status === 'error') && (
          <div className="rs-refresh-result">
            {startError || pollError ? (
              <span className="rs-refresh-summary">{startError || pollError}</span>
            ) : (
              <>
                <span className="rs-refresh-summary">
                  {current.status === 'error' ? 'Обновление не удалось. ' : ''}
                  Сохранено {current.reels_saved ?? 0} рилсов · {fmtRub(current.cost_rub)}
                </span>
                {errors.length > 0 && (
                  <ul className="rs-refresh-errors">
                    {errors.map((er, i) => (
                      <li key={i}>
                        {(er.usernames || []).map(u => '@' + u).join(', ')}
                        {er.usernames?.length ? ' — ' : ''}{er.error}
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </div>
        )}
      </section>
    </div>
  )
}
