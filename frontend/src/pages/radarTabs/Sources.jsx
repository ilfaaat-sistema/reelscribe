import { useState, useEffect } from 'react'
import { getSources, addSources, removeSource, restoreSource } from '../../api/radar'
import { fmtNum, fmtDate, fmtAgo } from './format'

// Вкладка «Источники» (ТЗ 10): отслеживаемые аккаунты, добавление, «Убрать»/«Вернуть».

function addSummary(res) {
  const parts = []
  if (res.added?.length) parts.push(`Добавлено: ${res.added.map(u => '@' + u).join(', ')}`)
  if (res.restored?.length) parts.push(`Возвращено: ${res.restored.map(u => '@' + u).join(', ')}`)
  if (res.already?.length) parts.push(`Уже есть: ${res.already.map(u => '@' + u).join(', ')}`)
  if (res.invalid?.length) {
    parts.push(`Не распознано: ${res.invalid.map(i => `${i.input} (${i.reason})`).join('; ')}`)
  }
  return parts.join(' · ')
}

export default function Sources({ refreshKey, onChanged }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)
  const [input, setInput] = useState('')
  const [adding, setAdding] = useState(false)
  const [msg, setMsg] = useState(null)           // { text, error, hint }
  const [busyUser, setBusyUser] = useState(null)

  const load = () => {
    setLoadError(null)
    return getSources()
      .then(setData)
      .catch(e => setLoadError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  const add = async () => {
    const val = input.trim()
    if (!val || adding) return
    setAdding(true)
    setMsg(null)
    try {
      const res = await addSources(val)
      const changed = (res.added?.length || 0) + (res.restored?.length || 0) > 0
      setMsg({
        text: addSummary(res),
        error: !changed && !!res.invalid?.length,
        hint: changed ? 'Нажмите «Обновить», чтобы подтянуть рилсы' : null,
      })
      if (changed) setInput('')
      await load()
      if (changed) onChanged?.()
    } catch (e) {
      setMsg({ text: e.message, error: true })
    } finally {
      setAdding(false)
    }
  }

  const act = async (username, fn) => {
    setBusyUser(username)
    setMsg(null)
    try {
      await fn(username)
      await load()
      onChanged?.()
    } catch (e) {
      setMsg({ text: e.message, error: true })
    } finally {
      setBusyUser(null)
    }
  }

  const active = data?.active || []
  const excluded = data?.excluded || []

  return (
    <div className="wrap rs-wide">
      <section className="card rs-sources">
        <div className="rs-add">
          <input
            className="rs-add-input"
            placeholder="@ник или ссылка на профиль"
            value={input}
            disabled={adding}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); add() } }}
          />
          <button className="btn btn-primary rs-add-btn" onClick={add} disabled={adding || !input.trim()}>
            {adding ? 'Добавляю…' : 'Добавить'}
          </button>
        </div>
        <div className="rs-add-msg" style={msg?.error ? { color: 'var(--rose)' } : undefined}>
          {msg?.text}
          {msg?.hint && <> · {msg.hint}</>}
        </div>

        {loadError && (
          <div className="rs-empty">
            Не удалось загрузить источники: {loadError}{' '}
            <button className="btn btn-ghost" onClick={() => { setLoading(true); load() }}>Повторить</button>
          </div>
        )}

        {loading && !data && !loadError && <div className="rs-empty">Загружаю источники…</div>}

        {data && active.length === 0 && (
          <div className="rs-empty">
            Источников пока нет. Вставьте @ник или ссылку на профиль Instagram выше и нажмите «Добавить»
            {data.max_sources ? ` (до ${data.max_sources} аккаунтов)` : ''}.
          </div>
        )}

        {active.length > 0 && (
          <div className="rs-table-wrap">
            <table className="rs-table rs-src-table">
              <thead>
                <tr>
                  <th className="rs-th rs-th-left">Аккаунт</th>
                  <th className="rs-th">Подписчики</th>
                  <th className="rs-th">Медиана</th>
                  <th className="rs-th">Последний рилс</th>
                  <th className="rs-th">Рилсов</th>
                  <th className="rs-th">Залётных</th>
                  <th className="rs-th">Обновлён</th>
                  <th className="rs-th"></th>
                </tr>
              </thead>
              <tbody>
                {active.map(s => (
                  <tr key={s.username} className="rs-row" data-username={s.username}>
                    <td className="rs-td-acc">
                      <a
                        className="rs-acc"
                        href={`https://www.instagram.com/${s.username}/`}
                        target="_blank"
                        rel="noopener noreferrer"
                      >@{s.username}</a>
                      {s.full_name && <span className="rs-fullname">{s.full_name}</span>}
                    </td>
                    <td className="rs-td-num">
                      <span className="num">{fmtNum(s.followers)}</span>
                    </td>
                    <td className="rs-td-num">
                      {s.norm_n >= 5
                        ? <span className="num">{fmtNum(s.median_views)}</span>
                        : <span className="rs-viral-none" title="мало данных">—</span>}
                    </td>
                    <td className="rs-td-num">{fmtDate(s.last_reel_at)}</td>
                    <td className="rs-td-num"><span className="num">{s.reels_count ?? 0}</span></td>
                    <td className="rs-td-num"><span className="num">{s.viral_count ?? 0}</span></td>
                    <td className="rs-td-num">
                      {s.last_scraped_at
                        ? fmtAgo(s.last_scraped_at)
                        : <span className="rs-badge new">ещё не обновлялся</span>}
                    </td>
                    <td className="rs-td-act">
                      <button
                        className="btn btn-ghost rs-remove"
                        disabled={busyUser === s.username}
                        onClick={() => act(s.username, removeSource)}
                      >Убрать</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {excluded.length > 0 && (
          <details className="rs-excluded">
            <summary>Исключённые ({excluded.length})</summary>
            <ul className="rs-excluded-list">
              {excluded.map(s => (
                <li key={s.username} className="rs-excluded-item">
                  <span className="rs-acc">@{s.username}</span>
                  <button
                    className="btn btn-ghost rs-restore"
                    disabled={busyUser === s.username}
                    onClick={() => act(s.username, restoreSource)}
                  >Вернуть</button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
    </div>
  )
}
