import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getSessions } from '../api/client'

const STATUS_RU = {
  running: 'В работе',
  done: 'Готово',
}

function fmtDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit' }) +
    ' ' + d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
}

export default function History() {
  const navigate = useNavigate()
  const [sessions, setSessions] = useState(null)
  const [error, setError] = useState(null)

  const load = () => {
    setError(null)
    getSessions()
      .then(setSessions)
      .catch(e => setError(e.message))
  }

  useEffect(load, [])

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
  if (sessions === null) {
    return <div className="resview" style={{ padding: 24, opacity: 0.6 }}>Загрузка…</div>
  }

  return (
    <div className="resview">
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '16px 24px 8px' }}>
        <h2 style={{ margin: 0 }}>История импортов</h2>
        <div style={{ flex: 1 }} />
        <button className="btn ghost sm" onClick={() => navigate('/')}>+ Новый импорт</button>
      </div>
      {sessions.length === 0 && (
        <p style={{ padding: '0 24px', opacity: 0.6 }}>Импортов ещё не было.</p>
      )}
      <div className="rwrap" style={{ padding: '0 24px 24px' }}>
        <table>
          <thead>
            <tr>
              <th>Дата</th>
              <th>Комментарий</th>
              <th>Роликов</th>
              <th>Готово</th>
              <th>Ошибки</th>
              <th>Статус</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {sessions.map(s => (
              <tr key={s.id} style={{ cursor: 'pointer' }}
                  onClick={() => navigate(`/results/${s.id}`)}>
                <td>{fmtDate(s.created_at)}</td>
                <td>{s.comment || <span style={{ opacity: 0.5 }}>—</span>}</td>
                <td>{s.total}</td>
                <td>{s.loaded}</td>
                <td>{s.failed || 0}</td>
                <td>{STATUS_RU[s.status] || s.status}</td>
                <td><span style={{ opacity: 0.5 }}>открыть →</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
