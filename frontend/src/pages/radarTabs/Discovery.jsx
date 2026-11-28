import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { addSources, getDiscovery, getSources, searchDiscovery } from '../../api/radar'
import { fmtDate } from './format'
import { DISCOVERY_STATUS_LABELS, PROFILE_STATUS_LABELS, downloadDiscovery, instagramUrl, profileUrl } from './discoveryExport'
import './discovery.css'

const DEFAULT_USERNAME = 'alex_magnier'
const POLL_DELAY_MS = 5000
const METRICS = [
  ['followers', 'Подписчики'],
  ['following', 'Подписки'],
  ['posts', 'Публикации'],
]
const number = new Intl.NumberFormat('ru-RU')
const timestamp = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' })
const fmtCount = value => value == null ? '—' : number.format(value)

function Coverage({ snapshot }) {
  const date = snapshot.checked_at && new Date(snapshot.checked_at)
  const withStatistics = snapshot.rows.filter(row => METRICS.every(([key]) => row[key] != null)).length
  return (
    <div className="rs-discovery-coverage" role="status">
      <div className="rs-discovery-coverage-line">
        <h2>
          Подписки <a href={profileUrl(snapshot.username)} target="_blank" rel="noopener noreferrer">@{snapshot.username}</a>
        </h2>
        <span className={`rs-discovery-status ${snapshot.status}`}>{DISCOVERY_STATUS_LABELS[snapshot.status]}</span>
      </div>
      <p>
        Найдено <b className="num">{fmtCount(snapshot.collected)}</b>
        {snapshot.expected == null
          ? ' · счётчик подписок недоступен'
          : <> из <b className="num">{fmtCount(snapshot.expected)}</b></>}
        {date && Number.isFinite(date.getTime()) && <> · Проверено <time dateTime={snapshot.checked_at}>{timestamp.format(date)}</time></>}
      </p>
      <p title="Заполнены подписчики, подписки и публикации">
        Статистика заполнена для <b className="num">{fmtCount(withStatistics)}</b> из {fmtCount(snapshot.rows.length)} профилей
      </p>
      {snapshot.reason && <p className="rs-discovery-reason">{snapshot.reason}</p>}
    </div>
  )
}

function DiscoveryTable({ rows, sort, onSort, selected, saved, adding, onSelect }) {
  return (
    <div className="rs-table-wrap" role="region" aria-label="Результаты поиска авторов" tabIndex={0}>
      <table className="rs-table rs-discovery-table">
        <thead>
          <tr>
            <th className="rs-th rs-th-left" scope="col">Выбор</th>
            <th className="rs-th rs-th-left" scope="col">Аккаунт</th>
            <th className="rs-th rs-th-left" scope="col">Описание</th>
            {METRICS.map(([key, label]) => {
              const direction = sort?.key === key ? sort.direction : undefined
              return (
                <th className="rs-th" scope="col" key={key} aria-sort={direction}>
                  <button className="rs-discovery-sort" type="button" onClick={() => onSort(key)}>
                    {label} <span aria-hidden="true">{direction === 'ascending' ? '↑' : direction ? '↓' : '↕'}</span>
                  </button>
                </th>
              )
            })}
            <th className="rs-th rs-th-left" scope="col">Доступ</th>
            <th className="rs-th rs-th-left" scope="col">Рилс</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(row => {
            const inSources = saved.has(row.username)
            const reelUrl = instagramUrl(row.reel_url)
            const isPrivate = row.private === true || row.profile_status === 'private'
            const dataStatus = PROFILE_STATUS_LABELS[row.profile_status]
            return (
              <tr className="rs-row" key={row.username}>
                <td>
                  {inSources ? <span className="rs-discovery-saved">В источниках</span> : (
                    <label className="rs-check">
                      <input
                        type="checkbox"
                        aria-label={`Выбрать @${row.username}`}
                        checked={selected.has(row.username)}
                        disabled={adding}
                        onChange={e => onSelect(row.username, e.target.checked)}
                      />
                    </label>
                  )}
                </td>
                <td className="rs-td-acc">
                  <a className="rs-acc" href={instagramUrl(row.profile_url) || profileUrl(row.username)} target="_blank" rel="noopener noreferrer">@{row.username}</a>
                  {row.name && <span className="rs-fullname">{row.name}</span>}
                  {(row.profile_source || row.profile_checked_at) && (
                    <span className="rs-fullname" title="Источник и дата статистики профиля">
                      {row.profile_source}
                      {row.profile_source && row.profile_checked_at && ' · '}
                      {row.profile_checked_at && <time dateTime={row.profile_checked_at}>{fmtDate(row.profile_checked_at)}</time>}
                    </span>
                  )}
                </td>
                <td className="rs-discovery-bio">{row.bio || '—'}</td>
                {METRICS.map(([key]) => <td className="rs-td-num" key={key}><span className="num">{fmtCount(row[key])}</span></td>)}
                <td>
                  {isPrivate ? 'Закрыт' : row.private === false ? 'Открыт' : dataStatus || '—'}
                  {row.private === false && !isPrivate && row.profile_status !== 'available' && dataStatus && <span className="rs-fullname">{dataStatus}</span>}
                </td>
                <td>
                  {reelUrl ? (
                    <a href={reelUrl} target="_blank" rel="noopener noreferrer" aria-label={`Открыть рилс @${row.username}`}>
                      {row.reel_date ? fmtDate(row.reel_date) : 'Открыть'}
                    </a>
                  ) : '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function Discovery({ onSourcesChanged }) {
  const [input, setInput] = useState(`@${DEFAULT_USERNAME}`)
  const [snapshot, setSnapshot] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [loadError, setLoadError] = useState(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState(null)
  const [pollError, setPollError] = useState(null)
  const [pollAttempt, setPollAttempt] = useState(0)
  const [filter, setFilter] = useState('')
  const [onlyOutsideSources, setOnlyOutsideSources] = useState(false)
  const [sort, setSort] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [saved, setSaved] = useState(() => new Set())
  const [sourcesStatus, setSourcesStatus] = useState('loading')
  const [sourcesAttempt, setSourcesAttempt] = useState(0)
  const [adding, setAdding] = useState(false)
  const [addMessage, setAddMessage] = useState(null)
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState(null)

  useEffect(() => {
    const controller = new AbortController()
    getDiscovery(DEFAULT_USERNAME, controller.signal)
      .then(result => { if (!controller.signal.aborted) setSnapshot(result) })
      .catch(error => {
        if (!controller.signal.aborted) {
          setLoadError(error.status === 404 ? `Сохранённый список @${DEFAULT_USERNAME} пока недоступен.` : error.message)
        }
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [loadAttempt])

  useEffect(() => {
    const controller = new AbortController()
    getSources(controller.signal)
      .then(result => {
        if (controller.signal.aborted) return
        if (!Array.isArray(result.active)) throw new Error('Не удалось прочитать список источников')
        const active = new Set(result.active.filter(source => source.tracking_reels !== false).map(source => source.username))
        setSaved(previous => new Set([...previous, ...active]))
        setSelected(previous => new Set([...previous].filter(username => !active.has(username))))
        setSourcesStatus('ready')
      })
      .catch(() => { if (!controller.signal.aborted) setSourcesStatus('failed') })
    return () => controller.abort()
  }, [sourcesAttempt])

  const runningUsername = snapshot?.status === 'running' ? snapshot.username : null
  useEffect(() => {
    if (!runningUsername) return
    const controller = new AbortController()
    let timer
    let errors = 0
    const poll = async () => {
      try {
        const result = await getDiscovery(runningUsername, controller.signal)
        if (controller.signal.aborted) return
        setSnapshot(result)
        if (result.status !== 'running') return
        errors = 0
      } catch (error) {
        if (controller.signal.aborted) return
        errors += 1
        if (errors >= 3) {
          setPollError(error.message)
          return
        }
      }
      timer = setTimeout(poll, POLL_DELAY_MS)
    }
    timer = setTimeout(poll, POLL_DELAY_MS)
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [runningUsername, pollAttempt])

  const search = async event => {
    event.preventDefault()
    if (!input.trim() || loading || searching || runningUsername || adding) return
    setSearching(true)
    setSearchError(null)
    try {
      const result = await searchDiscovery(input.trim())
      setSnapshot(result)
      setInput(`@${result.username}`)
      setLoadError(null)
      setPollError(null)
      setFilter('')
      setSelected(new Set())
      setAddMessage(null)
      setExportError(null)
    } catch (error) {
      setSearchError(error.message)
    } finally {
      setSearching(false)
    }
  }

  const rows = useMemo(() => {
    const query = filter.trim().replace(/^@/, '').toLocaleLowerCase('ru-RU')
    const found = (snapshot?.rows || []).filter(row =>
      (!onlyOutsideSources || !saved.has(row.username)) &&
      (!query || [row.username, row.name, row.bio].some(value => value?.toLocaleLowerCase('ru-RU').includes(query))),
    )
    if (!sort) return found
    return found.sort((a, b) => {
      const left = a[sort.key]
      const right = b[sort.key]
      if (left == null) return right == null ? 0 : 1
      if (right == null) return -1
      return sort.direction === 'ascending' ? left - right : right - left
    })
  }, [snapshot, filter, sort, saved, onlyOutsideSources])

  const pickSort = key => setSort(previous => ({
    key,
    direction: previous?.key === key && previous.direction === 'descending' ? 'ascending' : 'descending',
  }))

  const select = (username, checked) => setSelected(previous => {
    const next = new Set(previous)
    if (checked) next.add(username)
    else next.delete(username)
    return next
  })

  const saveSources = async () => {
    if (!selected.size || adding || searching) return
    setAdding(true)
    setAddMessage(null)
    try {
      const { added = [], restored = [], already = [], invalid = [] } = await addSources([...selected].join(', '))
      const accepted = [...added, ...restored, ...already]
      setSaved(previous => new Set([...previous, ...accepted]))
      setSelected(previous => new Set([...previous].filter(username => !accepted.includes(username))))
      setAddMessage({
        text: [
          added.length > 0 && `Добавлено: ${added.length}`,
          restored.length > 0 && `Возвращено: ${restored.length}`,
          already.length > 0 && `Уже в источниках: ${already.length}`,
          ...invalid.map(item => `${item.input}: ${item.reason}`),
        ].filter(Boolean).join(' · '),
        accepted: accepted.length > 0,
        error: invalid.length > 0,
      })
      if (added.length || restored.length) onSourcesChanged?.()
    } catch (error) {
      setAddMessage({ text: error.message, error: true })
    } finally {
      setAdding(false)
    }
  }

  const download = async () => {
    setExportError(null)
    setExporting(true)
    try {
      await downloadDiscovery(snapshot)
    } catch {
      setExportError('Не удалось скачать Excel. Попробуйте ещё раз.')
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="wrap rs-wide">
      <section className="card rs-discovery" aria-label="Поиск авторов по подпискам">
        <div className="rs-discovery-panel">
          <label className="rs-discovery-label" htmlFor="rs-discovery-profile">Профиль Instagram, чьи подписки нужно посмотреть</label>
          <form className="rs-add" onSubmit={search}>
            <input
              id="rs-discovery-profile"
              className="rs-add-input"
              placeholder="@ник или ссылка на профиль"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              value={input}
              disabled={searching || Boolean(runningUsername)}
              onChange={event => setInput(event.target.value)}
            />
            <button className="btn btn-primary rs-add-btn" type="submit" disabled={!input.trim() || loading || searching || Boolean(runningUsername) || adding}>
              {searching ? 'Ищу…' : 'Найти подписки'}
            </button>
          </form>
          {loading && <p className="hint" role="status">Загружаю сохранённый список…</p>}
          {loadError && (
            <div className="banner banner-notice">
              <span role="status">{loadError}</span>
              <button className="btn btn-ghost" type="button" disabled={searching} onClick={() => {
                setLoading(true)
                setLoadError(null)
                setSearchError(null)
                setLoadAttempt(previous => previous + 1)
              }}>Повторить загрузку</button>
            </div>
          )}
          {searchError && <p className="banner banner-error" role="alert">{searchError}</p>}
          {snapshot && <Coverage snapshot={snapshot} />}
          {pollError && (
            <div className="banner banner-error">
              <span role="alert">Не удалось проверить статус сбора. {pollError}</span>
              <button className="btn btn-ghost" type="button" onClick={() => {
                setPollError(null)
                setPollAttempt(previous => previous + 1)
              }}>Проверить снова</button>
            </div>
          )}
          {snapshot?.rows.length > 0 && (
            <div className="rs-discovery-controls">
              <div className="rs-discovery-filter">
                <label className="rs-discovery-label" htmlFor="rs-discovery-filter">Фильтр по нику, имени или описанию</label>
                <input
                  id="rs-discovery-filter"
                  className="rs-add-input"
                  type="search"
                  value={filter}
                  onChange={event => setFilter(event.target.value)}
                />
              </div>
              <label className="rs-check rs-discovery-source-filter">
                <input
                  type="checkbox"
                  checked={onlyOutsideSources}
                  disabled={sourcesStatus !== 'ready'}
                  onChange={event => setOnlyOutsideSources(event.target.checked)}
                />
                Ещё нет в источниках
              </label>
              <div className="rs-discovery-actions">
                <span className="rs-count">Показано <b className="num">{fmtCount(rows.length)}</b> из {fmtCount(snapshot.rows.length)}</span>
                <button className="btn btn-ghost" type="button" disabled={!selected.size || adding || searching} onClick={saveSources}>
                  {adding ? 'Добавляю…' : `Добавить в источники${selected.size ? ` (${selected.size})` : ''}`}
                </button>
                <button className="btn btn-ghost" type="button" title="Скачать все строки сохранённого списка" disabled={exporting || searching} onClick={download}>
                  {exporting ? 'Готовлю Excel…' : 'Скачать Excel'}
                </button>
              </div>
            </div>
          )}
          {snapshot?.rows.length > 0 && sourcesStatus === 'failed' && (
            <div className="banner banner-notice">
              <span role="status">Не удалось проверить, какие авторы уже есть в источниках.</span>
              <button className="btn btn-ghost" type="button" onClick={() => {
                setSourcesStatus('loading')
                setSourcesAttempt(previous => previous + 1)
              }}>Загрузить источники снова</button>
            </div>
          )}
          {addMessage && <p className={addMessage.error ? 'banner banner-error' : 'banner banner-notice'} role="status">
            <span>{addMessage.text}</span>
            {addMessage.accepted && <Link to="/radar?tab=sources">Открыть источники</Link>}
          </p>}
          {exportError && <p className="banner banner-error" role="alert">{exportError}</p>}
        </div>
        {rows.length > 0 && <DiscoveryTable rows={rows} sort={sort} onSort={pickSort} selected={selected} saved={saved} adding={adding} onSelect={select} />}
        {snapshot?.rows.length > 0 && !rows.length && <p className="rs-empty" role="status">По этому фильтру авторов нет.</p>}
        {snapshot?.rows.length === 0 && ['complete', 'partial'].includes(snapshot.status) && <p className="rs-empty">В списке пока нет профилей.</p>}
      </section>
    </div>
  )
}
