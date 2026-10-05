import { useEffect, useRef, useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getFeed } from '../../api/radar'
import { thumbUrl } from '../../api/client'
import { fmtNum, fmtX, fmtDate, fmtPerDay } from './format'

// Вкладка «Лента» Радара (ТЗ docs/specs/10-radar-intel.md): рилсы всех источников
// с залётностью относительно нормы аккаунта. Фильтры и сортировка живут в адресе.

const PAGE = 60

const PERIODS = [
  { v: '7', label: '7 дней' },
  { v: '30', label: '30 дней' },
  { v: '90', label: '90 дней' },
  { v: '0', label: 'Всё' },
]

// Порядок колонок — по контракту ТЗ (после «Аккаунта» идут сортируемые).
const SORT_COLS = [
  { key: 'viral', label: 'Залётность' },
  { key: 'views', label: 'Просмотры' },
  { key: 'speed', label: 'Скорость' },
  { key: 'reach', label: 'Охват' },
  { key: 'posted_at', label: 'Дата' },
  { key: 'followers', label: 'Подписчики' },
  { key: 'likes', label: 'Лайки' },
  { key: 'comments', label: 'Комментарии' },
]

const fmtDur = s => {
  if (!s) return ''
  const t = Math.round(s)
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`
}

function ViralCell({ it }) {
  const val = fmtX(it.viral_ratio)
  const normTitle = `норма аккаунта: ${fmtNum(it.norm_views)} просмотров`
  if (it.viral_ratio == null) {
    return <span className="rs-viral-none" title="мало рилсов для нормы">—</span>
  }
  if (it.is_viral) {
    const lvl = it.viral_ratio >= 10 ? 'v10' : it.viral_ratio >= 5 ? 'v5' : 'v3'
    return <span className={`rs-badge viral ${lvl}`} title={normTitle}>{val}</span>
  }
  if (!it.norm_reliable) {
    return <span className="rs-badge low" title={`мало данных: норма по ${it.norm_n} рилсам`}>{val}</span>
  }
  return <span className="rs-viral-plain" title={normTitle}>{val}</span>
}

export default function Feed({ refreshKey }) {
  const [searchParams, setSearchParams] = useSearchParams()

  const period = ['7', '30', '90', '0'].includes(searchParams.get('fperiod')) ? searchParams.get('fperiod') : '30'
  const acc = searchParams.get('facc') || ''
  const sort = SORT_COLS.some(c => c.key === searchParams.get('fsort')) ? searchParams.get('fsort') : 'viral'
  const dir = searchParams.get('fdir') === 'asc' ? 'asc' : 'desc'
  const onlyViral = searchParams.get('fviral') === '1'

  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)     // первая страница
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState(null)
  const [retryKey, setRetryKey] = useState(0)
  const reqId = useRef(0)                          // защита от устаревших ответов

  // Запись ключа в адрес: дефолт не пишется, остальные параметры (tab и др.) сохраняются.
  const setParam = useCallback((key, value, def) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value === def || value === '' || value == null) next.delete(key)
      else next.set(key, value)
      return next
    })
  }, [setSearchParams])

  const query = useCallback(offset => ({
    period: Number(period),
    usernames: acc ? [acc] : [],
    onlyViral,
    sort,
    order: dir,
    limit: PAGE,
    offset,
  }), [period, acc, onlyViral, sort, dir])

  // Первая страница: любой фильтр/сортировка, refreshKey и «Повторить» сбрасывают список.
  useEffect(() => {
    const id = ++reqId.current
    setLoading(true)
    setLoadingMore(false)
    setError(null)
    setItems([])
    getFeed(query(0))
      .then(data => {
        if (id !== reqId.current) return
        setItems(data.items || [])
        setTotal(data.total || 0)
        setAccounts(data.accounts || [])
        setLoading(false)
      })
      .catch(e => {
        if (id !== reqId.current) return
        setError(e.message || 'Не удалось загрузить ленту')
        setLoading(false)
      })
  }, [query, refreshKey, retryKey])

  const loadMore = () => {
    const id = ++reqId.current
    setLoadingMore(true)
    setError(null)
    getFeed(query(items.length))
      .then(data => {
        if (id !== reqId.current) return
        setItems(prev => [...prev, ...(data.items || [])])
        setTotal(data.total || 0)
        setLoadingMore(false)
      })
      .catch(e => {
        if (id !== reqId.current) return
        setError(e.message || 'Не удалось загрузить ленту')
        setLoadingMore(false)
      })
  }

  const onSort = key => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      const nextDir = key === sort ? (dir === 'desc' ? 'asc' : 'desc') : 'desc'
      if (key === 'viral') next.delete('fsort'); else next.set('fsort', key)
      if (nextDir === 'desc') next.delete('fdir'); else next.set('fdir', nextDir)
      return next
    })
  }

  const openAnalysis = it => {
    const next = new URLSearchParams({ tab: 'oneoff', accounts: it.username })
    next.set(it.analysis_status === 'done' ? 'reel' : 'select', it.id)
    setSearchParams(next)
  }

  const noSources = accounts.length === 0
  const accOptions = acc && !accounts.some(a => a.username === acc)
    ? [...accounts, { username: acc, count: 0 }]
    : accounts

  return (
    <div className="wrap rs-wide">
      <section className="card rs-feed">
        <div className="rs-filters">
          <div className="seg rs-period">
            {PERIODS.map(p => (
              <button
                key={p.v}
                type="button"
                className={period === p.v ? 'on' : ''}
                onClick={() => setParam('fperiod', p.v, '30')}
              >{p.label}</button>
            ))}
          </div>
          <select
            className="rs-select"
            value={acc}
            onChange={e => setParam('facc', e.target.value, '')}
            aria-label="Аккаунт"
          >
            <option value="">Все аккаунты</option>
            {accOptions.map(a => (
              <option key={a.username} value={a.username}>@{a.username} ({a.count})</option>
            ))}
          </select>
          <label className="rs-check">
            <input
              type="checkbox"
              checked={onlyViral}
              onChange={e => setParam('fviral', e.target.checked ? '1' : '', '')}
            /> Только залётные
          </label>
          <span className="rs-count">Рилсов: <b className="num">{total}</b></span>
        </div>

        {loading && <div className="rs-skeleton" aria-busy="true" />}

        {!loading && error && items.length === 0 && (
          <div className="rs-empty">
            <div>{error}</div>
            <button type="button" className="btn btn-ghost" onClick={() => setRetryKey(k => k + 1)}>Повторить</button>
          </div>
        )}

        {!loading && !error && items.length === 0 && (
          <div className="rs-empty">
            {noSources
              ? 'Добавьте аккаунты во вкладке «Источники» и нажмите «Обновить»'
              : onlyViral ? 'Залётных за период нет' : 'За выбранный период рилсов нет'}
          </div>
        )}

        {items.length > 0 && (
          <div className="rs-table-wrap">
            <table className="rs-table">
              <thead>
                <tr>
                  <th className="rs-th" />
                  <th className="rs-th rs-th-left">Аккаунт</th>
                  {SORT_COLS.map(c => (
                    <th
                      key={c.key}
                      className={`rs-th rs-th-sort${sort === c.key ? ` ${dir}` : ''}`}
                      data-key={c.key}
                      onClick={() => onSort(c.key)}
                    >{c.label}</th>
                  ))}
                  <th className="rs-th" />
                  <th className="rs-th" />
                </tr>
              </thead>
              <tbody>
                {items.map(it => (
                  <tr key={it.id} className="rs-row" data-id={it.id}>
                    <td className="rs-td-thumb">
                      <a className="rs-thumb" href={it.url} target="_blank" rel="noreferrer">
                        <img loading="lazy" src={thumbUrl(it.id)} alt="" />
                        {it.duration_sec ? <span className="rs-dur">{fmtDur(it.duration_sec)}</span> : null}
                      </a>
                    </td>
                    <td className="rs-td-acc">
                      <a className="rs-acc" href={`https://www.instagram.com/${it.username}/`} target="_blank" rel="noreferrer">@{it.username}</a>
                      {it.caption ? <div className="rs-caption">{it.caption}</div> : null}
                    </td>
                    <td className="rs-td-num"><ViralCell it={it} /></td>
                    <td className="rs-td-num">{fmtNum(it.views)}</td>
                    <td className="rs-td-num">{fmtPerDay(it.speed)}</td>
                    <td className="rs-td-num">{it.reach == null ? '—' : fmtX(it.reach)}</td>
                    <td className="rs-td-num">{fmtDate(it.posted_at)}</td>
                    <td className="rs-td-num">{it.followers == null ? '—' : fmtNum(it.followers)}</td>
                    <td className="rs-td-num">{fmtNum(it.likes)}</td>
                    <td className="rs-td-num">{fmtNum(it.comments)}</td>
                    <td className="rs-td-rise">
                      {it.rising && (
                        <span
                          className="rs-badge rising"
                          title={it.rising_mode === 'history'
                            ? 'просмотры растут быстрее среднего по замерам'
                            : 'свежий рилс с высокой скоростью'}
                        >🔥 набирает</span>
                      )}
                    </td>
                    <td className="rs-td-act">
                      <button type="button" className="btn btn-ghost rs-analyze-btn" onClick={() => openAnalysis(it)}>
                        {it.analysis_status === 'done' ? 'Отчёт' : 'Разобрать'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {items.length > 0 && error && (
          <div className="rs-empty">
            <div>{error}</div>
            <button type="button" className="btn btn-ghost" onClick={loadMore}>Повторить</button>
          </div>
        )}

        {items.length > 0 && items.length < total && !error && (
          <div className="rs-more">
            <button type="button" className="btn btn-ghost" onClick={loadMore} disabled={loadingMore}>
              {loadingMore ? 'Загружаю…' : 'Показать ещё'}
            </button>
          </div>
        )}
      </section>
    </div>
  )
}
