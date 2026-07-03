import { useState, useEffect, useCallback, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { getReels, getSession, retryJobs, getProgress } from '../api/client'
import { fmtV, fmtPct, erClass } from '../lib/utils'
import ReelDrawer from '../components/ReelDrawer'
import ExportModal from '../components/ExportModal'

function StatusPill({ status }) {
  if (status === 'done') return <span className="pill p-done">✓ готово</span>
  if (status === 'failed') return <span className="pill p-fail">✗ ошибка</span>
  if (status === 'no_audio') return (
    <span className="pill" style={{ color: 'var(--amber)', background: 'color-mix(in srgb,var(--amber) 16%,transparent)' }}>
      📷 нет аудио
    </span>
  )
  if (status === 'transcribing') return <span style={{ color: 'var(--amber)', fontSize: 11 }}>🎙 расшифровка</span>
  if (status === 'translating') return <span style={{ color: 'var(--iris)', fontSize: 11 }}>🌐 перевод</span>
  if (status === 'downloading') return <span style={{ color: 'var(--sky)', fontSize: 11 }}>⬇ скачивание</span>
  return <span style={{ color: 'var(--faint)', fontSize: 11 }}>⏳ очередь</span>
}

// Колонки таблицы — порядок/тултипы/цвет-группы как в reelscribe_v12.html (COLMETA).
const COLS = [
  { key: 'type', th: 'Тип', cls: 'nos c-type',
    cell: r => <span className={`tag ${r.type === 'reel' ? 't-reel' : r.type === 'tv' ? 't-tv' : 't-post'}`}>{r.type}</span> },
  { key: 'date', th: 'Дата', title: 'Дата публикации', sortKey: 'posted_at', cls: 'c-date mono',
    cell: r => (r.posted_at ? r.posted_at.slice(0, 10) : '—') },
  { key: 'cap', th: 'Текст поста', cls: 'nos c-cap',
    cell: r => (
      <div className="capcol" title={r.caption || ''}>
        {r.has_note && <span className="noteic" title="есть заметка">📝 </span>}
        {r.caption || <span style={{ color: 'var(--faint)' }}>—</span>}
      </div>
    ) },
  { key: 'tx', th: 'Расшифровка', cls: 'nos',
    cell: (r, { showTr }) => {
      if (r.transcript_status === 'no_audio') return <span className="failtx">— нет аудио —</span>
      if (r.transcript_status === 'failed') return <span className="failtx">— ошибка —</span>
      const ru = showTr && r.transcript_text_ru
      const txt = ru ? r.transcript_text_ru : r.transcript_text
      return (
        <div className="txcell" title={txt || ''}>
          {ru && <span className="trbadge">RU</span>}
          {txt || <span style={{ color: 'var(--faint)' }}>—</span>}
        </div>
      )
    } },
  { key: 'author', th: 'Автор', title: 'Логин автора — клик по строке открывает карточку рилса', cls: 'grp-auth',
    cell: r => (r.author_handle ? <span className="authlink">@{r.author_handle}</span> : <span className="mono">@{r.shortcode}</span>) },
  { key: 'url', th: '🔗', title: 'Ссылка на рилс в Instagram', cls: 'nos',
    cell: r => <a className="authlink" href={r.url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}>↗</a> },
  { key: 'views', th: '👁', title: 'Просмотры — сколько раз посмотрели видео', sortKey: 'views', cls: 'c-num grp-reach',
    cell: r => <span className="views">{fmtV(r.views)}</span> },
  { key: 'er', th: '🔥', title: '🔥 Залётность = просмотры ÷ подписчики. Во сколько раз больше людей увидели рилс, чем у автора подписчиков. Больше 1 — разлетелось за пределы аудитории.', sortKey: 'er', cls: 'c-num grp-reach',
    cell: r => (r.er ? <span className={`er ${erClass(r.er)}`}>{r.er.toFixed(1)}×</span> : <span className="mono">—</span>) },
  { key: 'followers', th: '👥', title: 'Подписчиков у автора', sortKey: 'author_followers', cls: 'c-num grp-raw',
    cell: r => <span className="mono">{fmtV(r.author_followers)}</span> },
  { key: 'likes', th: '❤️', title: 'Лайки', sortKey: 'likes', cls: 'c-num grp-raw',
    cell: r => <span className="mono">{fmtV(r.likes)}</span> },
  { key: 'comments', th: '💬', title: 'Комментарии', sortKey: 'comments', cls: 'c-num grp-raw',
    cell: r => <span className="mono">{fmtV(r.comments)}</span> },
  { key: 'lpf', th: '❤️/👥', title: 'Лайки ÷ подписчики — насколько активно реагирует аудитория автора (в %)', sortKey: 'lpf', cls: 'c-num grp-ratio',
    cell: r => <span className="mono">{fmtPct(r.lpf)}</span> },
  { key: 'cpf', th: '💬/👥', title: 'Комментарии ÷ подписчики — насколько контент провоцирует обсуждение (в %)', sortKey: 'cpf', cls: 'c-num grp-ratio',
    cell: r => <span className="mono">{fmtPct(r.cpf)}</span> },
  { key: 'eng', th: '💞', title: '💞 Вовлечённость = (лайки+комменты) ÷ просмотры — какая доля посмотревших отреагировала. Честно показывает, зашло ли видео.', sortKey: 'eng', cls: 'c-num grp-ratio',
    cell: r => <span className="mono">{fmtPct(r.eng)}</span> },
  { key: 'status', th: 'Статус', cls: 'c-st',
    cell: r => <StatusPill status={r.transcript_status} /> },
]

function TopList({ title, reels, valueFor, onOpen }) {
  return (
    <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: '16px 18px' }}>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, textTransform: 'uppercase', color: 'var(--mut)', marginBottom: 12, letterSpacing: '.04em' }}>
        {title}
      </div>
      {reels.map(r => (
        <div
          key={r.id}
          onClick={() => onOpen(r.id)}
          style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 0', borderBottom: '1px solid var(--line)', cursor: 'pointer' }}
        >
          <span style={{ color: 'var(--iris)', fontFamily: 'var(--mono)', fontSize: 12 }}>@{r.author_handle || '—'}</span>
          <span style={{ flex: 1, fontSize: 12, color: 'var(--faint)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {r.transcript_text?.slice(0, 40) || r.caption?.slice(0, 40) || r.shortcode}
          </span>
          {valueFor(r)}
        </div>
      ))}
      {!reels.length && <div style={{ color: 'var(--faint)', fontSize: 12 }}>Нет данных</div>}
    </div>
  )
}

function Analytics({ reels, onOpen }) {
  const done = reels.filter(r => r.transcript_status === 'done')
  const failed = reels.filter(r => r.transcript_status === 'failed')
  const noAudio = reels.filter(r => r.transcript_status === 'no_audio')
  const withEr = reels.filter(r => r.er != null)
  const avgEr = withEr.length ? (withEr.reduce((s, r) => s + r.er, 0) / withEr.length) : null
  const top = (key) => [...reels].filter(r => r[key] != null).sort((a, b) => b[key] - a[key]).slice(0, 5)
  const topByViews = top('views')
  const topByEr = top('er')
  const topByEng = top('eng')
  const topByComments = top('comments')

  return (
    <div style={{ padding: '20px 18px', maxWidth: 980 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 24 }}>
        {[
          ['Рилсов', reels.length, 'var(--tx)'],
          ['Готово', done.length, 'var(--green)'],
          [noAudio.length ? 'Нет аудио' : 'Ошибки', noAudio.length || failed.length, noAudio.length ? 'var(--amber)' : (failed.length ? 'var(--rose)' : 'var(--faint)')],
          ['Средний ER', avgEr ? avgEr.toFixed(1) + '×' : '—', 'var(--amber)'],
        ].map(([l, v, c]) => (
          <div key={l} style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 12, padding: '16px 18px' }}>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 26, fontWeight: 600, color: c }}>{v}</div>
            <div style={{ fontSize: 12, color: 'var(--mut)', marginTop: 5 }}>{l}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <TopList title="👁 Топ по просмотрам" reels={topByViews} onOpen={onOpen}
          valueFor={r => <span style={{ fontFamily: 'var(--mono)', fontWeight: 600 }}>{fmtV(r.views)}</span>} />
        <TopList title="🔥 Топ по залётности" reels={topByEr} onOpen={onOpen}
          valueFor={r => <span className={`er ${erClass(r.er)}`}>{r.er?.toFixed(1)}×</span>} />
        <TopList title="💞 Топ по вовлечённости" reels={topByEng} onOpen={onOpen}
          valueFor={r => <span style={{ fontFamily: 'var(--mono)', fontWeight: 600, color: 'var(--iris)' }}>{fmtPct(r.eng)}</span>} />
        <TopList title="💬 Топ по комментам" reels={topByComments} onOpen={onOpen}
          valueFor={r => <span style={{ fontFamily: 'var(--mono)', fontWeight: 600 }}>{fmtV(r.comments)}</span>} />
      </div>
    </div>
  )
}

const PAGE_SIZE = 100

function ReaderView({ reels, onOpen }) {
  const [idx, setIdx] = useState(0)
  const reel = reels[idx]

  return (
    <div className="reader">
      <div className="rlist">
        {reels.map((r, i) => (
          <button key={r.id} className={`rrow ${i === idx ? 'sel' : ''}`} onClick={() => setIdx(i)}>
            <div className="top">
              <span className={`tag ${r.type === 'reel' ? 't-reel' : r.type === 'tv' ? 't-tv' : 't-post'}`}>{r.type}</span>
              <span style={{ color: 'var(--iris)', fontFamily: 'var(--mono)', fontSize: 12 }}>@{r.author_handle || '—'}</span>
              {r.er && <span className={`er ${erClass(r.er)}`} style={{ marginLeft: 'auto' }}>{r.er?.toFixed(1)}×</span>}
            </div>
            <div className="capcell">
              {r.transcript_text?.slice(0, 120) || r.caption?.slice(0, 120) || r.shortcode}
            </div>
          </button>
        ))}
      </div>

      <div className="rdetail">
        {reel ? (
          <div className="rd-wrap">
            <div className="rd-head">
              <a className="rd-thumb" href={reel.url} target="_blank" rel="noopener noreferrer">
                <div className="rd-play" />
              </a>
              <div>
                <div style={{ fontWeight: 700, marginBottom: 6 }}>@{reel.author_handle || '—'}</div>
                <div className="rd-stats">
                  <span>👁 {fmtV(reel.views)}</span>
                  <span>❤️ {fmtV(reel.likes)}</span>
                  {reel.er && <span className={`er ${erClass(reel.er)}`}>{reel.er?.toFixed(1)}×</span>}
                  <span>{fmtPct(reel.eng)} eng</span>
                </div>
                {reel.posted_at && (
                  <div style={{ fontSize: 11, color: 'var(--faint)', marginTop: 4 }}>{reel.posted_at?.slice(0, 10)}</div>
                )}
                <button className="btn sm ghost" style={{ marginTop: 10 }} onClick={() => onOpen(reel.id)}>
                  Открыть детали →
                </button>
              </div>
            </div>

            {reel.transcript_text && (
              <>
                <div className="plabel">Расшифровка</div>
                <div className="paper rd-paper">{reel.transcript_text}</div>
              </>
            )}
            {reel.transcript_text_ru && (
              <>
                <div className="plabel">Перевод</div>
                <div className="paper ru rd-paper">{reel.transcript_text_ru}</div>
              </>
            )}
            {!reel.transcript_text && (
              <div style={{ color: 'var(--faint)', marginTop: 20 }}>
                <StatusPill status={reel.transcript_status} />
              </div>
            )}

            <div className="rd-nav">
              <button className="btn ghost sm" disabled={idx === 0} onClick={() => setIdx(i => i - 1)}>‹ Предыдущий</button>
              <span style={{ color: 'var(--faint)', fontSize: 12 }}>{idx + 1} / {reels.length}</span>
              <button className="btn ghost sm" disabled={idx === reels.length - 1} onClick={() => setIdx(i => i + 1)}>Следующий ›</button>
            </div>
          </div>
        ) : (
          <div style={{ color: 'var(--faint)', textAlign: 'center', paddingTop: 60 }}>Выберите рилс</div>
        )}
      </div>
    </div>
  )
}

export default function Results() {
  const { sessionId } = useParams()
  const [mode, setMode] = useState('analytics')
  const [chip, setChip] = useState('all')
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState('views')
  const [sortDesc, setSortDesc] = useState(true)
  const [showTr, setShowTr] = useState(false)
  const [reels, setReels] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [showFilt, setShowFilt] = useState(false)
  const [showCols, setShowCols] = useState(false)
  const [cols, setCols] = useState(COLS.map(c => c.key))
  const [selected, setSelected] = useState([])
  const [drawerReelId, setDrawerReelId] = useState(null)
  const [showExport, setShowExport] = useState(false)
  const [filters, setFilters] = useState({ author: '', min_views: '', min_er: '' })
  const [dragSrc, setDragSrc] = useState(null)
  const [sessionNote, setSessionNote] = useState(null)
  const [progress, setProgress] = useState(null)
  const searchTimer = useRef(null)
  const abortRef = useRef(null)

  const load = useCallback(async (opts = {}) => {
    const { pageNum = 1, append = false, silent = false, limit } = opts
    if (abortRef.current) abortRef.current.abort()
    const controller = new AbortController()
    abortRef.current = controller
    if (!silent && !append) setLoading(true)
    if (append) setLoadingMore(true)
    try {
      const params = {
        session: sessionId,
        sort: sortKey,
        dir: sortDesc ? 'desc' : 'asc',
        page: pageNum,
        limit: limit || PAGE_SIZE,
      }
      if (chip === 'viral') params.filter = 'viral'
      else if (chip === 'done') params.filter = 'done'
      else if (chip === 'failed') params.filter = 'failed'
      if (search.trim()) params.q = search.trim()
      if (filters.author) params.author = filters.author
      if (filters.min_views) params.min_views = filters.min_views
      if (filters.min_er) params.min_er = filters.min_er
      const { items, total: t } = await getReels(params, controller.signal)
      if (controller.signal.aborted) return
      setTotal(t)
      if (append) { setReels(prev => [...prev, ...items]); setPage(pageNum) }
      else { setReels(items); setPage(Math.max(1, Math.ceil(items.length / PAGE_SIZE))) }
      setError(null)
    } catch (e) {
      if (e?.name === 'AbortError') return
      setError(e.message || 'Не удалось загрузить рилсы')
    }
    if (!silent && !append) setLoading(false)
    if (append) setLoadingMore(false)
  }, [sessionId, chip, sortKey, sortDesc, search, filters])

  useEffect(() => { load({ pageNum: 1 }) }, [load])

  useEffect(() => {
    if (sessionId) getSession(sessionId).then(s => setSessionNote(s.comment || null)).catch(() => {})
    else setSessionNote(null)
  }, [sessionId])

  const reelsLenRef = useRef(0)
  useEffect(() => { reelsLenRef.current = reels.length }, [reels.length])

  // Пока сессия обрабатывается — фоном подтягиваем новые готовые рилсы (раз в 5 c).
  useEffect(() => {
    if (!sessionId) { setProgress(null); return }
    let stop = false
    let timer
    async function tick() {
      try {
        const p = await getProgress(sessionId)
        if (stop) return
        setProgress(p)
        const running = p.total > 0 && (p.loaded + p.failed) < p.total
        if (running) {
          // тихо подтянуть новые строки (без экрана «Загрузка…»), не сбрасывая текущую пагинацию
          const amount = Math.min(Math.max(reelsLenRef.current, PAGE_SIZE), 500)
          await load({ pageNum: 1, silent: true, limit: amount })
        } else return                      // всё готово — больше не опрашиваем
      } catch (_) {}
      if (!stop) timer = setTimeout(tick, 5000)
    }
    timer = setTimeout(tick, 5000)       // первый опрос через 5 c (initial load уже отработал)
    return () => { stop = true; clearTimeout(timer) }
  }, [sessionId, load])

  const doneCount = reels.filter(r => r.transcript_status === 'done').length
  const inProgress = progress ? Math.max(0, progress.total - progress.loaded - progress.failed) : 0
  const stuckCount = reels.filter(
    r => r.transcript_status === 'failed' || r.transcript_status === 'queued'
  ).length

  async function handleRetry() {
    try {
      await retryJobs(sessionId)
      await load({ pageNum: 1 })
    } catch (e) {
      alert(e.message || 'Не удалось повторить обработку незавершённых рилсов')
    }
  }

  function handleShowMore() {
    load({ pageNum: page + 1, append: true })
  }

  function handleSearch(v) {
    clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => setSearch(v), 300)
  }

  function toggleSort(key) {
    if (sortKey === key) setSortDesc(d => !d)
    else { setSortKey(key); setSortDesc(true) }
  }

  function toggleSelect(id) {
    setSelected(s => s.includes(id) ? s.filter(x => x !== id) : [...s, id])
  }

  function onDragStart(key) { setDragSrc(key) }
  function onDragOver(e, key) { if (dragSrc && dragSrc !== key) e.preventDefault() }
  function onDrop(e, key) {
    if (!dragSrc || dragSrc === key) return
    e.preventDefault()
    setCols(prev => {
      const a = [...prev]
      const from = a.indexOf(dragSrc)
      const to = a.indexOf(key)
      a.splice(from, 1)
      a.splice(to, 0, dragSrc)
      return a
    })
    setDragSrc(null)
  }

  const visibleCols = COLS.filter(c => cols.includes(c.key))
    .sort((a, b) => cols.indexOf(a.key) - cols.indexOf(b.key))

  return (
    <div className="resview">
      <div className="rtop">
        <span className="ti">
          Расшифровки <span className="rcount">
            · {reels.length < total ? `показано ${reels.length} из ${total}` : total}
            {inProgress > 0
              ? ` · готово ${progress.loaded} · ещё ${inProgress} догружается…`
              : (doneCount < reels.length ? ` · готово ${doneCount}` : ' · всё готово ✓')}
          </span>
        </span>
        <div className="spacer" />
        <div className="toggle">
          {[['analytics', '📊 Аналитика'], ['table', '☰ Таблица'], ['reading', '📖 Чтение']].map(([m, l]) => (
            <button key={m} className={mode === m ? 'on' : ''} onClick={() => setMode(m)}>{l}</button>
          ))}
        </div>
        {stuckCount > 0 && (
          <button className="btn sm ghost" onClick={handleRetry} title="Сбросить незавершённые рилсы обратно в очередь">
            ↻ Повторить незавершённые ({stuckCount})
          </button>
        )}
        <button className="btn sm ghost" onClick={() => setShowExport(true)}>⬇ Экспорт</button>
      </div>

      {sessionNote && (
        <div className="sessnote">
          💬 <span>{sessionNote}</span>
        </div>
      )}

      {mode !== 'reading' && (
        <div className="subbar">
          <input
            className="search"
            placeholder="Поиск по подписи и расшифровке…"
            onChange={e => handleSearch(e.target.value)}
          />
          {[
            ['all', 'Все'],
            ['viral', '🔥 Залётные'],
            ['done', 'Готово'],
            ['failed', 'Ошибки'],
          ].map(([v, l]) => (
            <button
              key={v}
              className={`chip ${chip === v ? 'on' : ''} ${v === 'viral' ? 'fire' : v === 'done' ? 'tr' : ''}`}
              onClick={() => setChip(v)}
            >{l}</button>
          ))}
          <button className={`chip ${showFilt ? 'on' : ''}`} onClick={() => setShowFilt(f => !f)}>
            ⚙ Фильтры
          </button>
          {mode === 'table' && (
            <button className={`chip ${showCols ? 'on' : ''}`} onClick={() => setShowCols(f => !f)}>
              🧱 Столбцы
            </button>
          )}
          <span style={{ flex: 1 }} />
          <button className={`chip tr ${showTr ? 'on' : ''}`} onClick={() => setShowTr(t => !t)}>
            🌐 Перевод на русский
          </button>
        </div>
      )}

      <div className={`filtpanel ${showFilt ? 'on' : ''}`}>
        <div className="fgroup">
          <label>Автор</label>
          <input placeholder="@handle" value={filters.author} onChange={e => setFilters(f => ({ ...f, author: e.target.value }))} />
        </div>
        <div className="fgroup">
          <label>Мин. просмотры</label>
          <input type="number" placeholder="50000" value={filters.min_views} onChange={e => setFilters(f => ({ ...f, min_views: e.target.value }))} />
        </div>
        <div className="fgroup">
          <label>Мин. залётность</label>
          <input type="number" placeholder="5" value={filters.min_er} onChange={e => setFilters(f => ({ ...f, min_er: e.target.value }))} />
        </div>
        <div className="fgroup">
          <label>Сортировка</label>
          <select value={sortKey} onChange={e => setSortKey(e.target.value)}>
            <option value="views">Просмотры</option>
            <option value="er">Залётность</option>
            <option value="likes">Лайки</option>
            <option value="comments">Комменты</option>
            <option value="author_followers">Подписчики</option>
            <option value="lpf">Лайки/подп</option>
            <option value="cpf">Комм/подп</option>
            <option value="eng">Вовлечённость</option>
            <option value="posted_at">Дата публикации</option>
            <option value="created_at">Дата добавления</option>
          </select>
        </div>
        <div className="fgroup" style={{ justifyContent: 'flex-end' }}>
          <label className="chkline" style={{ gap: 6 }}>
            <input type="checkbox" checked={sortDesc} onChange={e => setSortDesc(e.target.checked)} />
            По убыванию
          </label>
        </div>
        <button className="btn sm ghost" onClick={() => setFilters({ author: '', min_views: '', min_er: '' })}>
          Сбросить
        </button>
      </div>

      {mode === 'table' && showCols && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', padding: '8px 18px', borderBottom: '1px solid var(--line)', background: 'var(--panel)', flexShrink: 0 }}>
          {COLS.map(c => (
            <label key={c.key} className="chkline" style={{ fontSize: 12 }}>
              <input
                type="checkbox"
                checked={cols.includes(c.key)}
                onChange={e => {
                  if (e.target.checked) setCols(p => [...p, c.key])
                  else setCols(p => p.filter(x => x !== c.key))
                }}
              />
              {c.th}
            </label>
          ))}
        </div>
      )}

      {loading && (
        <div style={{ textAlign: 'center', padding: 40, color: 'var(--faint)' }}>Загрузка…</div>
      )}

      {!loading && error && (
        <div className="errbanner" style={{ margin: '20px 18px' }}>
          ⚠ Не удалось загрузить рилсы: {error}
          <button className="btn sm ghost" onClick={() => load({ pageNum: 1 })}>Повторить</button>
        </div>
      )}

      {!loading && !error && mode === 'analytics' && <Analytics reels={reels} onOpen={setDrawerReelId} />}

      {!loading && !error && mode === 'reading' && (
        <>
          <ReaderView reels={reels} onOpen={setDrawerReelId} />
          {reels.length < total && (
            <div style={{ textAlign: 'center', padding: 16 }}>
              <button className="btn sm ghost" disabled={loadingMore} onClick={handleShowMore}>
                {loadingMore ? 'Загрузка…' : `Показать ещё (${reels.length} из ${total})`}
              </button>
            </div>
          )}
        </>
      )}

      {!loading && !error && mode === 'table' && (
        <div className="rwrap">
          <table>
            <thead>
              <tr>
                <th className="nos" style={{ width: 36, textAlign: 'center' }}>
                  <input
                    type="checkbox"
                    checked={selected.length === reels.length && reels.length > 0}
                    onChange={e => setSelected(e.target.checked ? reels.map(r => r.id) : [])}
                  />
                </th>
                {visibleCols.map(col => (
                  <th
                    key={col.key}
                    className={[col.cls || '', 'colh', col.sortKey ? '' : 'nos', dragSrc === col.key ? 'dragging' : ''].join(' ')}
                    title={col.title || col.th}
                    draggable
                    onDragStart={() => onDragStart(col.key)}
                    onDragOver={e => onDragOver(e, col.key)}
                    onDrop={e => onDrop(e, col.key)}
                    onClick={() => col.sortKey && toggleSort(col.sortKey)}
                  >
                    {col.th}
                    {col.sortKey && sortKey === col.sortKey && (
                      <span style={{ marginLeft: 4 }}>{sortDesc ? '▼' : '▲'}</span>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {reels.map(r => (
                <tr key={r.id} className="main" onClick={() => setDrawerReelId(r.id)}>
                  <td style={{ textAlign: 'center' }} onClick={e => { e.stopPropagation(); toggleSelect(r.id) }}>
                    <input type="checkbox" checked={selected.includes(r.id)} onChange={() => toggleSelect(r.id)} />
                  </td>
                  {visibleCols.map(col => (
                    <td key={col.key} className={col.cls}>{col.cell(r, { showTr })}</td>
                  ))}
                </tr>
              ))}
              {!reels.length && (
                <tr>
                  <td colSpan={visibleCols.length + 1} style={{ textAlign: 'center', padding: 40, color: 'var(--faint)' }}>
                    Ничего не найдено
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          {reels.length < total && (
            <div style={{ textAlign: 'center', padding: 16 }}>
              <button className="btn sm ghost" disabled={loadingMore} onClick={handleShowMore}>
                {loadingMore ? 'Загрузка…' : `Показать ещё (${reels.length} из ${total})`}
              </button>
            </div>
          )}
        </div>
      )}

      <ReelDrawer reelId={drawerReelId} onClose={() => setDrawerReelId(null)} />

      {showExport && (
        <ExportModal
          reels={reels}
          total={total}
          selected={selected}
          query={{
            session: sessionId,
            q: search || undefined,
            filter: chip !== 'all' ? chip : undefined,
            author: filters.author || undefined,
            min_views: filters.min_views || undefined,
            min_er: filters.min_er || undefined,
            sort: sortKey,
            dir: sortDesc ? 'desc' : 'asc',
          }}
          onClose={() => setShowExport(false)}
        />
      )}
    </div>
  )
}
