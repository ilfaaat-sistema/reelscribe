import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { getErrors, retryJobs, errorsExportUrl } from '../api/client'

export default function Errors() {
  const { sessionId } = useParams()
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState({})
  const [retrying, setRetrying] = useState(false)

  const load = () => {
    setError(null)
    getErrors(sessionId)
      .then(setReport)
      .catch(e => setError(e.message))
  }

  useEffect(load, [sessionId])

  async function handleRetry() {
    if (!sessionId) {
      const ok = window.confirm(
        'Это вернёт в очередь ВСЕ незавершённые рилсы по всей базе (не только на этой странице) ' +
        'и запустит их повторное скачивание через платные API (Apify/RapidAPI). Продолжить?'
      )
      if (!ok) return
    }
    setRetrying(true)
    try {
      await retryJobs(sessionId)
      load()
    } catch (e) {
      alert(e.message || 'Не удалось повторить обработку незавершённых рилсов')
    }
    setRetrying(false)
  }

  function toggle(reason) {
    setOpen(o => ({ ...o, [reason]: !o[reason] }))
  }

  if (error) {
    return (
      <div className="resview" style={{ padding: 24 }}>
        <div className="errbanner">
          {error}
          <button className="btn ghost sm" onClick={load}>Повторить</button>
        </div>
      </div>
    )
  }
  if (report === null) {
    return <div className="resview" style={{ padding: 24, opacity: 0.6 }}>Загрузка…</div>
  }

  return (
    <div className="resview">
      <div className="rtop">
        <span className="ti">
          Ошибки{sessionId ? ' сессии' : ''} <span className="rcount">· {report.total}</span>
        </span>
        <div className="spacer" />
        {report.total > 0 && (
          <>
            <button className="btn sm ghost" onClick={handleRetry} disabled={retrying}
                    title={sessionId
                      ? 'Сбросить незавершённые рилсы этой сессии обратно в очередь'
                      : 'Сбросить в очередь ВСЕ незавершённые рилсы по всей базе — повторное скачивание через платные API'}>
              {sessionId ? '↻ Повторить незавершённые' : '↻ Повторить незавершённые по всей базе'}
            </button>
            <a className="btn sm ghost" href={errorsExportUrl({ format: 'csv', ...(sessionId ? { session: sessionId } : {}) })}>
              ⬇ Экспорт отчёта
            </a>
          </>
        )}
      </div>

      {report.total === 0 && (
        <p style={{ padding: '24px', opacity: 0.6 }}>Ошибок нет 🎉</p>
      )}

      <div style={{ padding: '0 24px 24px' }}>
        {report.groups.map(g => (
          <div key={g.reason} className="rwrap" style={{ marginTop: 14, borderRadius: 10, overflow: 'hidden' }}>
            <div
              onClick={() => toggle(g.reason)}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer',
                padding: '10px 14px', background: 'var(--panel)', borderBottom: '1px solid var(--line)',
              }}
            >
              <span style={{ opacity: 0.6 }}>{open[g.reason] ? '▾' : '▸'}</span>
              <span style={{ flex: 1, fontWeight: 600 }}>{g.reason}</span>
              <span className="pill p-fail">{g.count}</span>
            </div>
            {open[g.reason] && (
              <table>
                <thead>
                  <tr>
                    <th>Код</th>
                    <th>Автор</th>
                    <th>Попытки</th>
                    <th>Статус</th>
                    <th>Текст ошибки</th>
                  </tr>
                </thead>
                <tbody>
                  {g.items.map(it => (
                    <tr key={it.reel_id}>
                      <td>
                        <a className="authlink mono" href={it.url} target="_blank" rel="noopener noreferrer">
                          {it.shortcode} ↗
                        </a>
                      </td>
                      <td>{it.author_handle ? <span className="authlink">@{it.author_handle}</span> : '—'}</td>
                      <td className="mono">{it.attempts}</td>
                      <td>{it.state}</td>
                      <td><span className="failtx" title={it.job_error || ''}>{it.job_error || '—'}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
