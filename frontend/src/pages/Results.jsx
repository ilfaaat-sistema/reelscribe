import { useState, useEffect, useCallback, useRef } from 'react'
import { getReels } from '../api/client'
import { fmtV, fmtPct, erClass } from '../lib/utils'
import ReelDrawer from '../components/ReelDrawer'
import ExportModal from '../components/ExportModal'

const DEFAULT_COLS = [
  { key: 'author', label: 'Автор', cls: 'grp-auth', sortKey: 'author_followers', nosort: false },
  { key: 'text', label: 'Текст / Расшифровка', cls: '', nosort: true },
  { key: 'views', label: '👁', title: 'Просмотры', cls: 'grp-reach c-num', sortKey: 'views' },
  { key: 'er', label: '🔥', title: 'Залётность (просм ÷ подп)', cls: 'grp-reach c-num', sortKey: 'er' },
  { key: 'lpf', label: '⚡', title: 'Лайки / подписчики %', cls: 'grp-ratio c-num', sortKey: 'lpf' },
  { key: 'cpf', label: '💬', title: 'Комменты / подписчики %', cls: 'grp-ratio c-num', sortKey: 'cpf' },
  { key: 'eng', label: '📊', title: 'Вовлечённость %', cls: 'grp-ratio c-num', sortKey: 'eng' },
  { key: 'status', label: 'Статус', cls: '', nosort: true },
]

function StatusPill({ status }) {
  if (status === 'done') return <span className="pill p-done">✓ готово</span>
  if (status === 'failed') return <span className="pill p-fail">✗ ошибка</span>
  if (status === 'transcribing') return <span style={{color:'var(--amber)',fontSize:11}}>🎙 расшифровка</span>
  if (status === 'downloading') return <span style={{color:'var(--sky)',fontSize:11}}>⬇ скачивание</span>
  return <span style={{color:'var(--faint)',fontSize:11}}>⏳ очередь</span>
}

function Analytics({ reels }) {
  const done = reels.filter(r => r.transcript_status === 'done')
  const failed = reels.filter(r => r.transcript_status === 'failed')
  const withEr = reels.filter(r => r.er != null)
  const avgEr = withEr.length ? (withEr.reduce((s, r) => s + r.er, 0) / withEr.length) : null
  const maxEr = withEr.length ? Math.max(...withEr.map(r => r.er)) : null
  const topByViews = [...reels].sort((a, b) => (b.views || 0) - (a.views || 0)).slice(0, 5)
  const topByEr = [...withEr].sort((a, b) => b.er - a.er).slice(0, 5)

  return (
    <div style={{padding:'20px 18px',maxWidth:900}}>
      <div style={{display:'grid',gridTemplateColumns:'repeat(4,1fr)',gap:12,marginBottom:24}}>
        {[
          ['Рилсов', reels.length, 'var(--tx)'],
          ['Готово', done.length, 'var(--green)'],
          ['Ошибки', failed.length, failed.length ? 'var(--rose)' : 'var(--faint)'],
          ['Средний ER', avgEr ? avgEr.toFixed(1) + '×' : '—', 'var(--amber)'],
        ].map(([l, v, c]) => (
          <div key={l} style={{background:'var(--panel)',border:'1px solid var(--line)',borderRadius:12,padding:'16px 18px'}}>
            <div style={{fontFamily:'var(--mono)',fontSize:26,fontWeight:600,color:c}}>{v}</div>
            <div style={{fontSize:12,color:'var(--mut)',marginTop:5}}>{l}</div>
          </div>
        ))}
      </div>

      <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:16}}>
        <div style={{background:'var(--panel)',border:'1px solid var(--line)',borderRadius:12,padding:'16px 18px'}}>
          <div style={{fontFamily:'var(--mono)',fontSize:11,textTransform:'uppercase',color:'var(--mut)',marginBottom:12,letterSpacing:'.04em'}}>
            👁 Топ по просмотрам
          </div>
          {topByViews.map(r => (
            <div key={r.id} style={{display:'flex',alignItems:'center',gap:8,padding:'7px 0',borderBottom:'1px solid var(--line)'}}>
              <span style={{color:'var(--iris)',fontFamily:'var(--mono)',fontSize:12}}>@{r.author_handle || '—'}</span>
              <span style={{flex:1,fontSize:12,color:'var(--faint)',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                {r.transcript_text?.slice(0,40) || r.caption?.slice(0,40) || r.shortcode}
              </span>
              <span style={{fontFamily:'var(--mono)',fontWeight:600}}>{fmtV(r.views)}</span>
            </div>
          ))}
          {!topByViews.length && <div style={{color:'var(--faint)',fontSize:12}}>Нет данных</div>}
        </div>

        <div style={{background:'var(--panel)',border:'1px solid var(--line)',borderRadius:12,padding:'16px 18px'}}>
          <div style={{fontFamily:'var(--mono)',fontSize:11,textTransform:'uppercase',color:'var(--mut)',marginBottom:12,letterSpacing:'.04em'}}>
            🔥 Топ по залётности
          </div>
          {topByEr.map(r => (
            <div key={r.id} style={{display:'flex',alignItems:'center',gap:8,padding:'7px 0',borderBottom:'1px solid var(--line)'}}>
              <span style={{color:'var(--iris)',fontFamily:'var(--mono)',fontSize:12}}>@{r.author_handle || '—'}</span>
              <span style={{flex:1,fontSize:12,color:'var(--faint)',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                {r.transcript_text?.slice(0,40) || r.caption?.slice(0,40) || r.shortcode}
              </span>
              <span className={`er ${erClass(r.er)}`}>{r.er?.toFixed(1)}×</span>
            </div>
          ))}
          {!topByEr.length && <div style={{color:'var(--faint)',fontSize:12}}>Нет метрик</div>}
        </div>
      </div>
    </div>
  )
}

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
              <span style={{color:'var(--iris)',fontFamily:'var(--mono)',fontSize:12}}>@{r.author_handle || '—'}</span>
              {r.er && <span className={`er ${erClass(r.er)}`} style={{marginLeft:'auto'}}>{r.er?.toFixed(1)}×</span>}
            </div>
            <div className="capcell">
              {r.transcript_text?.slice(0,120) || r.caption?.slice(0,120) || r.shortcode}
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
                <div style={{fontWeight:700,marginBottom:6}}>@{reel.author_handle || '—'}</div>
                <div className="rd-stats">
                  <span>👁 {fmtV(reel.views)}</span>
                  <span>❤️ {fmtV(reel.likes)}</span>
                  {reel.er && <span className={`er ${erClass(reel.er)}`}>{reel.er?.toFixed(1)}×</span>}
                  <span>{fmtPct(reel.eng)} eng</span>
                </div>
                {reel.posted_at && (
                  <div style={{fontSize:11,color:'var(--faint)',marginTop:4}}>{reel.posted_at?.slice(0,10)}</div>
                )}
                <button
                  className="btn sm ghost"
                  style={{marginTop:10}}
                  onClick={() => onOpen(reel.id)}
                >
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
              <div style={{color:'var(--faint)',marginTop:20}}>
                <StatusPill status={reel.transcript_status} />
              </div>
            )}

            <div className="rd-nav">
              <button
                className="btn ghost sm"
                disabled={idx === 0}
                onClick={() => setIdx(i => i - 1)}
              >‹ Предыдущий</button>
              <span style={{color:'var(--faint)',fontSize:12}}>{idx + 1} / {reels.length}</span>
              <button
                className="btn ghost sm"
                disabled={idx === reels.length - 1}
                onClick={() => setIdx(i => i + 1)}
              >Следующий ›</button>
            </div>
          </div>
        ) : (
          <div style={{color:'var(--faint)',textAlign:'center',paddingTop:60}}>Выберите рилс</div>
        )}
      </div>
    </div>
  )
}

export default function Results({ sessionId }) {
  const [mode, setMode] = useState('analytics')
  const [chip, setChip] = useState('all')
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState('views')
  const [sortDesc, setSortDesc] = useState(true)
  const [reels, setReels] = useState([])
  const [loading, setLoading] = useState(true)
  const [showFilt, setShowFilt] = useState(false)
  const [showCols, setShowCols] = useState(false)
  const [cols, setCols] = useState(DEFAULT_COLS.map(c => c.key))
  const [selected, setSelected] = useState([])
  const [drawerReelId, setDrawerReelId] = useState(null)
  const [showExport, setShowExport] = useState(false)
  const [filters, setFilters] = useState({ author: '', min_views: '', min_er: '' })
  const [dragSrc, setDragSrc] = useState(null)
  const searchTimer = useRef(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = {
        session: sessionId,
        sort: sortKey,
        desc: sortDesc ? 'true' : 'false',
        limit: 200,
      }
      if (chip === 'viral') params.filter = 'viral'
      else if (chip === 'done') params.filter = 'done'
      else if (chip === 'failed') params.filter = 'failed'
      if (search.trim()) params.q = search.trim()
      if (filters.author) params.author = filters.author
      if (filters.min_views) params.min_views = filters.min_views
      if (filters.min_er) params.min_er = filters.min_er
      const data = await getReels(params)
      setReels(Array.isArray(data) ? data : (data.reels || []))
    } catch (_) {}
    setLoading(false)
  }, [sessionId, chip, sortKey, sortDesc, search, filters])

  useEffect(() => { load() }, [load])

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

  const visibleCols = DEFAULT_COLS.filter(c => cols.includes(c.key))
    .sort((a, b) => cols.indexOf(a.key) - cols.indexOf(b.key))

  const sessionNote = null

  return (
    <div className="resview">
      <div className="rtop">
        <span className="ti">
          Результаты <span className="rcount">{reels.length}</span>
        </span>
        <div className="spacer" />
        <div className="toggle">
          {[['analytics','📊 Аналитика'],['table','☰ Таблица'],['reading','📖 Чтение']].map(([m, l]) => (
            <button key={m} className={mode === m ? 'on' : ''} onClick={() => setMode(m)}>{l}</button>
          ))}
        </div>
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
            placeholder="Поиск по автору, тексту, расшифровке…"
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
              Колонки
            </button>
          )}
        </div>
      )}

      <div className={`filtpanel ${showFilt ? 'on' : ''}`}>
        <div className="fgroup">
          <label>Автор</label>
          <input
            placeholder="@handle"
            value={filters.author}
            onChange={e => setFilters(f => ({...f, author: e.target.value}))}
          />
        </div>
        <div className="fgroup">
          <label>Мин. просмотры</label>
          <input
            type="number"
            placeholder="50000"
            value={filters.min_views}
            onChange={e => setFilters(f => ({...f, min_views: e.target.value}))}
          />
        </div>
        <div className="fgroup">
          <label>Мин. залётность</label>
          <input
            type="number"
            placeholder="5"
            value={filters.min_er}
            onChange={e => setFilters(f => ({...f, min_er: e.target.value}))}
          />
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
            <option value="created_at">Дата добавления</option>
          </select>
        </div>
        <div className="fgroup" style={{justifyContent:'flex-end'}}>
          <label className="chkline" style={{gap:6}}>
            <input type="checkbox" checked={sortDesc} onChange={e => setSortDesc(e.target.checked)} />
            По убыванию
          </label>
        </div>
        <button className="btn sm ghost" onClick={() => setFilters({ author: '', min_views: '', min_er: '' })}>
          Сбросить
        </button>
      </div>

      {mode === 'table' && showCols && (
        <div style={{display:'flex',gap:8,flexWrap:'wrap',padding:'8px 18px',borderBottom:'1px solid var(--line)',background:'var(--panel)',flexShrink:0}}>
          {DEFAULT_COLS.map(c => (
            <label key={c.key} className="chkline" style={{fontSize:12}}>
              <input
                type="checkbox"
                checked={cols.includes(c.key)}
                onChange={e => {
                  if (e.target.checked) setCols(p => [...p, c.key])
                  else setCols(p => p.filter(x => x !== c.key))
                }}
              />
              {c.title || c.label}
            </label>
          ))}
        </div>
      )}

      {loading && (
        <div style={{textAlign:'center',padding:40,color:'var(--faint)'}}>Загрузка…</div>
      )}

      {!loading && mode === 'analytics' && <Analytics reels={reels} />}

      {!loading && mode === 'reading' && (
        <ReaderView reels={reels} onOpen={setDrawerReelId} />
      )}

      {!loading && mode === 'table' && (
        <div className="rwrap">
          <table>
            <thead>
              <tr>
                <th className="nos" style={{width:36,textAlign:'center'}}>
                  <input
                    type="checkbox"
                    checked={selected.length === reels.length && reels.length > 0}
                    onChange={e => setSelected(e.target.checked ? reels.map(r => r.id) : [])}
                  />
                </th>
                {visibleCols.map(col => (
                  <th
                    key={col.key}
                    className={[
                      col.cls || '',
                      'colh',
                      col.nosort ? 'nos' : '',
                      dragSrc === col.key ? 'dragging' : '',
                    ].join(' ')}
                    title={col.title || col.label}
                    draggable
                    onDragStart={() => onDragStart(col.key)}
                    onDragOver={e => onDragOver(e, col.key)}
                    onDrop={e => onDrop(e, col.key)}
                    onClick={() => !col.nosort && col.sortKey && toggleSort(col.sortKey)}
                  >
                    {col.label}
                    {!col.nosort && col.sortKey && sortKey === col.sortKey && (
                      <span style={{marginLeft:4}}>{sortDesc ? '▼' : '▲'}</span>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {reels.map(r => (
                <tr key={r.id} className="main" onClick={() => setDrawerReelId(r.id)}>
                  <td style={{textAlign:'center'}} onClick={e => { e.stopPropagation(); toggleSelect(r.id) }}>
                    <input type="checkbox" checked={selected.includes(r.id)} onChange={() => toggleSelect(r.id)} />
                  </td>
                  {visibleCols.map(col => {
                    if (col.key === 'author') return (
                      <td key="author" className={col.cls}>
                        <a className="authlink" href={r.url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}>
                          @{r.author_handle || r.shortcode}
                        </a>
                        <span className={`tag ${r.type === 'reel' ? 't-reel' : r.type === 'tv' ? 't-tv' : 't-post'}`} style={{marginLeft:6}}>
                          {r.type}
                        </span>
                        {r.has_note && <span className="noteic" title="Есть заметка">📌</span>}
                      </td>
                    )
                    if (col.key === 'text') return (
                      <td key="text">
                        {r.transcript_status === 'done' && r.transcript_text && (
                          <span className="trbadge">ASR</span>
                        )}
                        <span className="txcell">
                          {r.transcript_text?.slice(0,100) || r.caption?.slice(0,100) || <span style={{color:'var(--faint)'}}>—</span>}
                        </span>
                      </td>
                    )
                    if (col.key === 'views') return (
                      <td key="views" className={col.cls}>
                        <span className="views">{fmtV(r.views)}</span>
                      </td>
                    )
                    if (col.key === 'er') return (
                      <td key="er" className={col.cls}>
                        {r.er ? <span className={`er ${erClass(r.er)}`}>{r.er.toFixed(1)}×</span> : <span className="mono">—</span>}
                      </td>
                    )
                    if (col.key === 'lpf') return <td key="lpf" className={col.cls}><span className="mono">{fmtPct(r.lpf)}</span></td>
                    if (col.key === 'cpf') return <td key="cpf" className={col.cls}><span className="mono">{fmtPct(r.cpf)}</span></td>
                    if (col.key === 'eng') return <td key="eng" className={col.cls}><span className="mono">{fmtPct(r.eng)}</span></td>
                    if (col.key === 'status') return (
                      <td key="status"><StatusPill status={r.transcript_status} /></td>
                    )
                    return <td key={col.key} />
                  })}
                </tr>
              ))}
              {!reels.length && (
                <tr>
                  <td colSpan={visibleCols.length + 1} style={{textAlign:'center',padding:40,color:'var(--faint)'}}>
                    Ничего не найдено
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <ReelDrawer reelId={drawerReelId} onClose={() => setDrawerReelId(null)} />

      {showExport && (
        <ExportModal
          reels={reels}
          selected={selected}
          onClose={() => setShowExport(false)}
        />
      )}
    </div>
  )
}
