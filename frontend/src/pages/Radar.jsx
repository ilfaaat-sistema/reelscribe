import { useState, useEffect, useRef, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { thumbUrl } from '../api/client'
import {
  getSummary,
  getReels,
  getCostEstimate,
  startScrape as apiStartScrape,
  getScrapeStatus,
  startAnalyze as apiStartAnalyze,
  runJob,
  cancelAnalyze as apiCancelAnalyze,
  getAnalyzeStatus,
  getReelAnalysis,
  reportUrl,
  exportUrl,
} from '../api/radar'
import './radar.css'

// Перенос Радар/frontend/src/App.jsx разделом /radar (см. docs/specs/08-radar.md).
// Убрано относительно оригинала: переключатель Whisper (whisperMode/whisperOpenaiAvailable,
// бэкенд теперь всегда делает один вызов Gemini), /api/config, /api/thumbs (заменён на
// thumbUrl из api/client.js — общий прокси превью ReelScribe), PARSER_URL/топбар со
// своим лого (шапка теперь общая, из App.jsx ReelScribe), локальная оценка стоимости
// сбора (заменена вызовом GET /scrape/cost-estimate).

const MAX_USERNAMES = 5
const MAX_PERIOD = 12
const GEMINI_COST_PER_SEC = 0.01 / 60
const SORT_METRICS = ['views', 'comments', 'likes']

const STATUS_LABELS = {
  queued: 'В очереди',
  downloading: '⬇ Скачиваю…',
  analyzing: '🎬 Анализирую…',
  rate_limited: '⏳ Ждём снятия лимита Gemini',
  done: '✓ Готово',
  error: '✗ Ошибка',
  cancelled: '⏹ Отменено',
}

const fmt = n => {
  if (n === null || n === undefined) return '0'
  return n >= 1000 ? (n / 1000).toFixed(n >= 1e5 ? 0 : 1).replace('.', ',') + 'к' : '' + n
}

const fmtPeriod = m =>
  m < 12 ? m + ' мес' :
  m % 12 === 0 ? (m / 12) + (m === 12 ? ' год' : ' года') :
  m + ' мес'

const fmtSec = s => {
  if (!s) return '0:00'
  const sec = Math.round(s)
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
}

const fmtDateRu = iso => {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })
  } catch { return '' }
}

export default function Radar() {
  const [searchParams, setSearchParams] = useSearchParams()

  // ── Состояние, живущее в адресе (дефолты в URL не пишутся) ──
  const [accounts, setAccounts] = useState(() => {
    const raw = searchParams.get('accounts')
    if (!raw) return []
    return raw.split(',').map(s => s.trim()).filter(Boolean).slice(0, MAX_USERNAMES)
  })
  const [period, setPeriod] = useState(() => {
    const n = parseInt(searchParams.get('period'), 10)
    return Number.isFinite(n) ? Math.min(MAX_PERIOD, Math.max(1, n)) : 3
  })
  const [metric, setMetric] = useState(() => {
    const m = searchParams.get('metric')
    return SORT_METRICS.includes(m) ? m : 'views'
  })
  const [sortKey, setSortKey] = useState(() => {
    const s = searchParams.get('sort')
    return SORT_METRICS.includes(s) ? s : 'views'
  })
  const [viewMode, setViewMode] = useState(() => (searchParams.get('view') === 'list' ? 'list' : 'grid'))
  const [activeBuckets, setActiveBuckets] = useState(() => {
    const raw = searchParams.get('buckets')
    if (!raw) return new Set()
    return new Set(raw.split(',').map(s => parseInt(s, 10)).filter(Number.isFinite))
  })
  const reelParam = searchParams.get('reel')

  const [inputVal, setInputVal] = useState('')

  const [scraping, setScraping] = useState(false)
  const [runId, setRunId] = useState(null)
  const [runStatus, setRunStatus] = useState(null)

  const [summary, setSummary] = useState(null)
  const [reels, setReels] = useState([])
  const [costEstimateData, setCostEstimateData] = useState(null)

  const [selections, setSelections] = useState({})

  const [analyzing, setAnalyzing] = useState(false)
  const [analyzingIds, setAnalyzingIds] = useState([])
  const [analysisStatuses, setAnalysisStatuses] = useState({})

  const [reportReel, setReportReel] = useState(null)
  const [reportAnalysis, setReportAnalysis] = useState(null)

  const [scrapeErrorMsg, setScrapeErrorMsg] = useState(null)
  const [dataErrorMsg, setDataErrorMsg] = useState(null)
  const [analyzeErrorMsg, setAnalyzeErrorMsg] = useState(null)
  const [notice, setNotice] = useState(null)

  const pollRef = useRef(null)
  const analysisRef = useRef(null)
  const cancelRef = useRef(false)
  const mountedLoadRef = useRef(false)

  // ── Загрузка ленты/сводки из БД (без платного сбора) ──────────
  const loadData = useCallback(async (usernames, sortMetric) => {
    setDataErrorMsg(null)
    try {
      const [sum, reelsData] = await Promise.all([
        getSummary(usernames),
        getReels(usernames, sortMetric, { limit: 200 }),
      ])
      setSummary(sum)
      setReels(reelsData)
      // статусы разборов — из analysis_status/analysis_mode ответа /reels
      const seeded = {}
      reelsData.forEach(r => {
        if (r.analysis_status) seeded[r.id] = { status: r.analysis_status, mode: r.analysis_mode }
      })
      setAnalysisStatuses(prev => ({ ...prev, ...seeded }))
      setSortKey(sortMetric)
    } catch (e) {
      setDataErrorMsg(e.message)
    }
  }, [])

  // ── Синхронизация состояния в адрес: replace, дефолты не пишем ──
  useEffect(() => {
    const next = new URLSearchParams(searchParams)
    const setOrDelete = (key, val, def) => {
      if (val === def || val == null || val === '') next.delete(key)
      else next.set(key, val)
    }
    setOrDelete('accounts', accounts.join(','), '')
    setOrDelete('period', String(period), '3')
    setOrDelete('metric', metric, 'views')
    setOrDelete('sort', sortKey, 'views')
    setOrDelete('view', viewMode, 'grid')
    setOrDelete('buckets', [...activeBuckets].sort((a, b) => a - b).join(','), '')
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts, period, metric, sortKey, viewMode, activeBuckets])

  // ── При загрузке: если в адресе есть аккаунты — тянем ленту из БД ──
  useEffect(() => {
    if (mountedLoadRef.current) return
    mountedLoadRef.current = true
    if (accounts.length) loadData(accounts, sortKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Отчёт открывается/закрывается по параметру reel в адресе ──
  // (в том числе кнопкой «Назад» браузера — она меняет searchParams сама)
  useEffect(() => {
    if (!reelParam) {
      setReportReel(null)
      setReportAnalysis(null)
      return
    }
    let cancelled = false
    setAnalyzeErrorMsg(null)
    getReelAnalysis(reelParam)
      .then(data => {
        if (cancelled) return
        setReportReel(data.reel)
        setReportAnalysis(data.analysis)
      })
      .catch(e => {
        if (cancelled) return
        setAnalyzeErrorMsg(e.message)
      })
    return () => { cancelled = true }
  }, [reelParam])

  // ── Оценка стоимости сбора — с сервера, не локальной формулой ──
  useEffect(() => {
    if (!accounts.length) { setCostEstimateData(null); return }
    let cancelled = false
    getCostEstimate(accounts, period)
      .then(data => { if (!cancelled) setCostEstimateData(data) })
      .catch(() => { if (!cancelled) setCostEstimateData(null) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts.join(','), period])

  // ── Scrape ──────────────────────────────────────────────────

  const addAccount = () => {
    const val = inputVal.trim().replace(/^@/, '')
    if (!val || accounts.includes(val) || accounts.length >= MAX_USERNAMES) { setInputVal(''); return }
    setAccounts(prev => [...prev, val])
    setInputVal('')
  }

  const handleInputKeyDown = e => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addAccount() }
  }

  const startScrape = async () => {
    if (!accounts.length) return
    setScraping(true)
    setRunStatus(null)
    setSummary(null)
    setReels([])
    setActiveBuckets(new Set())
    setSelections({})
    setAnalysisStatuses({})
    setScrapeErrorMsg(null)
    try {
      const data = await apiStartScrape(accounts, period)
      setRunId(data.run_id)
    } catch (e) {
      setScraping(false)
      setScrapeErrorMsg(e.message)
    }
  }

  useEffect(() => {
    if (!runId) return
    const poll = async () => {
      try {
        const data = await getScrapeStatus(runId)
        setRunStatus(data)
        if (data.status === 'done' || data.status === 'error') {
          clearInterval(pollRef.current)
          setScraping(false)
          if (data.status === 'done') loadData(accounts, metric)
          if (data.status === 'error') setScrapeErrorMsg(data.error || 'Сбор не удался')
        }
      } catch (e) {
        clearInterval(pollRef.current)
        setScraping(false)
        setScrapeErrorMsg(e.message)
      }
    }
    poll()
    pollRef.current = setInterval(poll, 2000)
    return () => clearInterval(pollRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])

  // ── Analysis ─────────────────────────────────────────────────

  // Строго последовательный прогон очереди: один POST /jobs/{id}/run за раз
  // (может идти до ~90 с). Ошибка одного задания не останавливает остальные —
  // актуальный статус в любом случае подтянет поллинг /analyze/status.
  const runQueue = async ids => {
    for (const id of ids) {
      if (cancelRef.current) break
      try {
        await runJob(id)
      } catch {
        // статус ошибки покажет ближайший тик поллинга
      }
    }
    setAnalyzing(false)
  }

  const startAnalysis = async () => {
    const items = selectedReels.map(r => ({ reel_id: r.id, mode: selections[r.id] }))
    if (!items.length) return
    setAnalyzeErrorMsg(null)
    setNotice(null)
    try {
      const data = await apiStartAnalyze(items)
      const queued = data.queued || []
      const skipped = data.skipped || []

      if (queued.length) {
        const initial = {}
        queued.forEach(id => { initial[id] = { status: 'queued' } })
        setAnalysisStatuses(prev => ({ ...prev, ...initial }))
        setAnalyzingIds(queued)
        setAnalyzing(true)
        cancelRef.current = false
        runQueue(queued)
      }
      if (skipped.length) {
        const doneUpdates = {}
        skipped.forEach(id => { doneUpdates[id] = { status: 'done' } })
        setAnalysisStatuses(prev => ({ ...prev, ...doneUpdates }))
        setNotice(
          `${skipped.length} ${skipped.length === 1 ? 'рилс уже был разобран' : 'рилсов уже были разобраны'} ранее — заново не оплачивали`
        )
      }
    } catch (e) {
      setAnalyzeErrorMsg(e.message)
    }
  }

  // Поллинг статусов — только отображение, цикл разбора им не управляется
  useEffect(() => {
    if (!analyzing || !analyzingIds.length) return
    let stopped = false
    const poll = async () => {
      try {
        const data = await getAnalyzeStatus(analyzingIds)
        if (!stopped) setAnalysisStatuses(prev => ({ ...prev, ...data }))
      } catch (e) {
        if (!stopped) setAnalyzeErrorMsg(e.message)
      }
    }
    poll()
    analysisRef.current = setInterval(poll, 2000)
    return () => { stopped = true; clearInterval(analysisRef.current) }
  }, [analyzing, analyzingIds])

  const cancelAnalysis = async () => {
    const idsInProgress = analyzingIds.filter(id => {
      const s = analysisStatuses[id]?.status
      return s && s !== 'done' && s !== 'error' && s !== 'cancelled'
    })
    cancelRef.current = true
    if (!idsInProgress.length) { setAnalyzing(false); return }
    try {
      await apiCancelAnalyze(idsInProgress)
    } catch (e) {
      setAnalyzeErrorMsg(e.message)
    } finally {
      setAnalyzing(false)
    }
  }

  // ── Отчёт: открытие — push, закрытие — replace ──────────────
  const openReport = reel => {
    const next = new URLSearchParams(searchParams)
    next.set('reel', reel.id)
    setSearchParams(next)
  }

  const closeReport = () => {
    const next = new URLSearchParams(searchParams)
    next.delete('reel')
    setSearchParams(next, { replace: true })
  }

  // ── Grid / selection ─────────────────────────────────────────

  const visibleReels = (() => {
    let v = reels
    if (activeBuckets.size && summary?.buckets) {
      v = v.filter(r =>
        [...activeBuckets].some(i => {
          const b = summary.buckets[i]
          return b && r.views >= b.min && (b.max === null ? true : r.views < b.max)
        })
      )
    }
    return [...v].sort((a, b) => (b[sortKey] || 0) - (a[sortKey] || 0))
  })()

  const toggleBucket = i => {
    setActiveBuckets(prev => {
      const n = new Set(prev)
      if (n.has(i)) n.delete(i); else n.add(i)
      return n
    })
  }

  const cardClick = reel => {
    if (analysisStatuses[reel.id]?.status === 'done') { openReport(reel); return }
    setSelections(prev => ({ ...prev, [reel.id]: prev[reel.id] ? null : 'tv' }))
  }

  const setMode = (id, mode) => {
    setSelections(prev => ({ ...prev, [id]: prev[id] === mode ? null : mode }))
  }

  const selectAll = () => {
    setSelections(prev => {
      const n = { ...prev }
      visibleReels.forEach(r => { n[r.id] = 'tv' })
      return n
    })
  }
  const clearAll = () => setSelections({})

  const selectedReels = reels.filter(r => selections[r.id])
  const nText = selectedReels.filter(r => selections[r.id] !== 'v').length
  const nVideo = selectedReels.filter(r => selections[r.id] !== 't').length
  const totalSecs = selectedReels.reduce((s, r) => s + (r.duration_sec || 0), 0)
  const geminiCost = selectedReels
    .filter(r => selections[r.id] !== 't')
    .reduce((s, r) => s + (r.duration_sec || 0), 0) * GEMINI_COST_PER_SEC

  const nAnalyzingDone = analyzingIds.filter(id => {
    const s = analysisStatuses[id]?.status
    return s === 'done' || s === 'error' || s === 'cancelled'
  }).length

  const activeStatusLabel = analyzingIds
    .map(id => analysisStatuses[id]?.status)
    .filter(s => s && !['done', 'error', 'cancelled', 'queued'].includes(s))
    .map(s => STATUS_LABELS[s])
    .filter(Boolean)[0] || ''

  // ── Progress bar (scrape) ──────────────────────────────────
  const scrapeRunning = runStatus?.status === 'running'
  const scrapeError = runStatus?.status === 'error'

  const progressPct =
    !runStatus ? 0 :
    runStatus.status === 'done' ? 100 :
    runStatus.status === 'pending' ? 8 : 0

  const progressText =
    !runStatus ? '' :
    runStatus.status === 'pending' ? `Запускаю задачу на Apify для @${accounts.join(', @')}…` :
    runStatus.status === 'running'
      ? `Apify собирает рилсы — это займёт 1–2 мин…${runStatus.results_count ? ` Найдено: ${runStatus.results_count}` : ''}` :
    runStatus.status === 'done' ? `✓ Готово: ${runStatus.results_count} рилсов, потрачено $${runStatus.cost_estimate_usd?.toFixed(2)}` :
    `Ошибка: ${runStatus.error || 'что-то пошло не так'}`

  const bucketLabel = i => summary?.buckets?.[i]?.label ?? ''

  const followersMap = summary?.accounts?.reduce((m, a) => {
    m[a.username] = { followers: a.followers, updatedAt: a.followers_updated_at }
    return m
  }, {}) || {}

  // ── Render ───────────────────────────────────────────────────
  return (
    <div className="radar">
      <div className="wrap">

        {/* ШАГ 1 */}
        <section className="card">
          <div className="step-tag">Шаг 1 — что собираем</div>
          <h2>Конкуренты, период, метрика</h2>

          <div className="field">
            <label>Аккаунты конкурентов</label>
            <div className="chips">
              {accounts.map(acc => (
                <span key={acc} className="chip">
                  @{acc}
                  <span className="x" onClick={() => setAccounts(accounts.filter(a => a !== acc))}>×</span>
                </span>
              ))}
              {accounts.length < MAX_USERNAMES && (
                <input
                  placeholder="добавить @username и Enter"
                  value={inputVal}
                  onChange={e => setInputVal(e.target.value)}
                  onKeyDown={handleInputKeyDown}
                  onBlur={addAccount}
                />
              )}
            </div>
            {accounts.length >= MAX_USERNAMES && (
              <div className="hint">Максимум {MAX_USERNAMES} конкурентов за раз.</div>
            )}
          </div>

          <div className="field">
            <label>Период анализа</label>
            <div className="slider-row">
              <input type="range" min="1" max={MAX_PERIOD} value={period} onChange={e => setPeriod(+e.target.value)} />
              <div className="slider-val">{fmtPeriod(period)}</div>
            </div>
          </div>

          <div className="field">
            <label>Что важнее? Лента отсортируется по этой метрике</label>
            <div className="seg">
              {[['views', 'Просмотры'], ['comments', 'Комментарии'], ['likes', 'Лайки']].map(([m, label]) => (
                <button key={m} className={metric === m ? 'on' : ''} onClick={() => setMetric(m)}>{label}</button>
              ))}
            </div>
          </div>

          <div className="field">
            <button className="btn btn-primary" onClick={startScrape} disabled={!accounts.length || scraping}>
              {scraping ? 'Собираю…' : 'Собрать данные'}
            </button>
            {accounts.length > 0 && (
              <div className="hint">
                Только метаданные (просмотры, лайки, комментарии, превью, хэштеги, музыка) — примерно{' '}
                {costEstimateData
                  ? <span className="num">{costEstimateData.n_reels} рилсов ≈ ${costEstimateData.estimate_usd?.toFixed(2)}</span>
                  : <span className="num">…</span>
                }{' '}на Apify. Видео на этом шаге не скачиваются.
              </div>
            )}
          </div>

          {(scraping || runStatus) && (
            <div className="progress">
              <div className="p-track">
                {scrapeRunning
                  ? <div className="p-fill p-fill-running" />
                  : <div className="p-fill" style={{ width: progressPct + '%', background: scrapeError ? 'var(--rose)' : undefined }} />
                }
              </div>
              <div className="p-text" style={{ color: scrapeError ? 'var(--rose)' : undefined }}>{progressText}</div>
            </div>
          )}

          <ErrorBanner text={scrapeErrorMsg} onClose={() => setScrapeErrorMsg(null)} />
        </section>

        <ErrorBanner text={dataErrorMsg} onClose={() => setDataErrorMsg(null)} />

        {/* ШАГ 2 */}
        {summary && (
          <section className="card">
            <div className="step-tag">Шаг 2 — картина целиком</div>
            <h2>Распределение по просмотрам</h2>
            <Buckets buckets={summary.buckets} activeBuckets={activeBuckets} onToggle={toggleBucket} />
            <div className="legend">
              <span>Всего: <span className="num">{summary.total_reels} рилсов</span></span>
              <span>Медиана просмотров: <span className="num">{fmt(Math.round(summary.median_views || 0))}</span></span>
              {summary.best_account && (
                <span>Лучший по медиане: <span className="num">@{summary.best_account}</span></span>
              )}
              {activeBuckets.size === 0 && (
                <span style={{ color: 'var(--iris)', fontWeight: 700 }}>Клик по строке — фильтр ленты ниже</span>
              )}
            </div>

            {summary.accounts?.length > 0 && (
              <div style={{ marginTop: 16, borderTop: '1px solid var(--line)', paddingTop: 14 }}>
                <h3 style={{ fontSize: '.76rem', textTransform: 'uppercase', letterSpacing: '.07em', color: 'var(--mut)', marginBottom: 8 }}>
                  По аккаунтам · просмотры относительно подписчиков
                </h3>
                <div style={{ display: 'grid', gap: 6, fontSize: '.82rem' }}>
                  {summary.accounts.map(a => (
                    <div key={a.username} style={{ display: 'flex', flexWrap: 'wrap', gap: 14, alignItems: 'baseline' }}>
                      <b style={{ minWidth: 130 }}>@{a.username}</b>
                      {a.followers
                        ? <span className="num" title={a.followers_updated_at ? `Спарсено ${fmtDateRu(a.followers_updated_at)}` : undefined}>{fmt(a.followers)} подписчиков</span>
                        : <span style={{ color: 'var(--mut)' }}>подписчики: н/д</span>
                      }
                      <span>медиана <span className="num">{fmt(Math.round(a.median_views || 0))}</span></span>
                      {a.reach != null
                        ? <span style={{ color: a.reach >= 0.5 ? 'var(--green)' : 'var(--mut)', fontWeight: 700 }}>
                            охват ×{a.reach.toFixed(2)}{a.reach >= 0.5 ? ' 🔥' : ''}
                          </span>
                        : <span style={{ color: 'var(--mut)' }}>охват: н/д</span>
                      }
                    </div>
                  ))}
                </div>
                <div className="hint" style={{ marginTop: 10 }}>
                  Охват = медиана просмотров ÷ подписчики. Чем выше — тем чаще рилсы вылетают за пределы своей аудитории.
                </div>
              </div>
            )}
          </section>
        )}

        {/* ШАГ 3 */}
        {reels.length > 0 && (
          <section className="card">
            <div className="step-tag">Шаг 3 — выбор рилсов</div>

            <ErrorBanner text={analyzeErrorMsg} onClose={() => setAnalyzeErrorMsg(null)} />
            <NoticeBanner text={notice} onClose={() => setNotice(null)} />

            <div className="toolbar">
              <h2 style={{ margin: 0 }}>
                {visibleReels.length} рилсов
                {activeBuckets.size > 0
                  ? ` · фильтр: ${[...activeBuckets].map(i => bucketLabel(i)).join(', ')}`
                  : ''}
              </h2>
              <div className="toolbar-r">
                <div className="seg">
                  {[['views', 'Просмотры'], ['comments', 'Комменты'], ['likes', 'Лайки']].map(([s, label]) => (
                    <button key={s} className={sortKey === s ? 'on' : ''} onClick={() => setSortKey(s)}>{label}</button>
                  ))}
                </div>
                <div className="seg">
                  <button className={viewMode === 'grid' ? 'on' : ''} onClick={() => setViewMode('grid')}>⊞ Сетка</button>
                  <button className={viewMode === 'list' ? 'on' : ''} onClick={() => setViewMode('list')}>☰ Список</button>
                </div>
                <button className="btn btn-ghost" onClick={selectAll}>Выбрать все 📝🎬</button>
                <button className="btn btn-ghost" onClick={clearAll}>Сбросить</button>
              </div>
            </div>

            <div className="hint" style={{ margin: '0 0 12px' }}>
              <b>Клик по карточке</b> — выбрать рилс. Кнопки внизу уточняют режим: <b>📝 текст</b> — транскрипт,{' '}
              <b>🎬 кадр</b> — видеоанализ Gemini, <b>📝🎬 всё</b> — оба. После анализа — повторный клик открывает отчёт.
            </div>

            {viewMode === 'grid' ? (
              <div className="grid">
                {visibleReels.map(r => (
                  <ReelCard
                    key={r.id}
                    reel={r}
                    mode={selections[r.id] || null}
                    analysisStatus={analysisStatuses[r.id]?.status || null}
                    onCardClick={() => cardClick(r)}
                    onSetMode={m => setMode(r.id, m)}
                  />
                ))}
              </div>
            ) : (
              <ReelTable
                reels={visibleReels}
                followersMap={followersMap}
                selections={selections}
                analysisStatuses={analysisStatuses}
                onCardClick={cardClick}
                onSetMode={setMode}
              />
            )}
          </section>
        )}

        {/* ШАГ 4 — Отчёт */}
        {reportReel && (
          <ReportPanel
            reel={reportReel}
            analysis={reportAnalysis}
            allSelectedIds={selectedReels.map(r => r.id)}
            onClose={closeReport}
          />
        )}

      </div>

      {/* НИЖНЯЯ ПАНЕЛЬ */}
      {(selectedReels.length > 0 || analyzing) && (
        <div className="selbar">
          <div>
            <div className="info">
              {analyzing
                ? <>
                    Анализ: <span className="num">{nAnalyzingDone}/{analyzingIds.length}</span>
                    {activeStatusLabel ? <span style={{ color: 'var(--mut)', marginLeft: 8 }}>{activeStatusLabel}</span> : null}
                  </>
                : <>
                    Выбрано: <span className="num">{selectedReels.length}</span>
                    {' · '}транскрипт: {nText}
                    {' · '}видеоанализ: {nVideo}
                    {' · '}~<span className="num">{fmtSec(totalSecs)}</span> видео
                  </>
              }
            </div>
            {!analyzing && (
              <div className="cost">
                Gemini: ≈ <span className="num">${geminiCost.toFixed(2)}</span>
              </div>
            )}
          </div>
          <div className="actions">
            {analyzing && (
              <button className="btn btn-ghost" onClick={cancelAnalysis}>
                Отменить
              </button>
            )}
            <button
              className="btn btn-primary"
              onClick={startAnalysis}
              disabled={analyzing || !selectedReels.length}
            >
              {analyzing
                ? `Анализ ${nAnalyzingDone}/${analyzingIds.length}…`
                : 'Запустить анализ'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Баннеры ошибок и уведомлений ──────────────────────────────

function ErrorBanner({ text, onClose }) {
  if (!text) return null
  return (
    <div className="banner banner-error">
      <span>⚠ {text}</span>
      <button className="banner-close" onClick={onClose} aria-label="Скрыть">✕</button>
    </div>
  )
}

function NoticeBanner({ text, onClose }) {
  if (!text) return null
  return (
    <div className="banner banner-notice">
      <span>ℹ {text}</span>
      <button className="banner-close" onClick={onClose} aria-label="Скрыть">✕</button>
    </div>
  )
}

// ── Buckets ──────────────────────────────────────────────────

function Buckets({ buckets, activeBuckets, onToggle }) {
  if (!buckets?.length) return null
  const max = Math.max(...buckets.map(b => b.count), 1)
  return (
    <div>
      {buckets.map((b, i) => (
        <div key={i} className={`bucket ${activeBuckets.has(i) ? 'on' : ''}`} onClick={() => onToggle(i)}>
          <span className="range">{b.label}</span>
          <div className="bar-track">
            <div className="bar-fill" style={{ width: Math.max(8, b.count / max * 100) + '%' }}>{b.count}</div>
          </div>
          <span className="count"><b className="num">{b.count}</b> видео</span>
        </div>
      ))}
    </div>
  )
}

// ── ReelTable ────────────────────────────────────────────────

const TH = ({ children, style }) => (
  <th style={{
    textAlign: 'left', padding: '8px 10px', fontWeight: 700,
    fontSize: '.72rem', textTransform: 'uppercase', letterSpacing: '.05em',
    color: 'var(--mut)', borderBottom: '1px solid var(--line)',
    whiteSpace: 'nowrap', ...style,
  }}>{children}</th>
)

const TD = ({ children, style }) => (
  <td style={{ padding: '8px 10px', verticalAlign: 'middle', ...style }}>{children}</td>
)

function ReelTable({ reels, followersMap, selections, analysisStatuses, onCardClick, onSetMode }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '.82rem' }}>
        <thead>
          <tr>
            <TH>Превью</TH>
            <TH>Аккаунт</TH>
            <TH style={{ textAlign: 'right' }}>Просмотры</TH>
            <TH style={{ textAlign: 'right' }}>Подписчики</TH>
            <TH style={{ textAlign: 'right' }}>Охват×</TH>
            <TH style={{ textAlign: 'right' }}>Лайки</TH>
            <TH style={{ textAlign: 'right' }}>1 лайк / N просм.</TH>
            <TH style={{ textAlign: 'right' }}>Комменты</TH>
            <TH>Режим</TH>
          </tr>
        </thead>
        <tbody>
          {reels.map(r => {
            const followers = followersMap[r.username]?.followers || null
            const followersUpdatedAt = followersMap[r.username]?.updatedAt || null
            const reach = followers && r.views ? (r.views / followers) : null
            const perLike = r.likes && r.views ? Math.round(r.views / r.likes) : null
            const mode = selections[r.id] || null
            const status = analysisStatuses[r.id]?.status || null
            const isDone = status === 'done'
            const isError = status === 'error' || status === 'cancelled'
            return (
              <tr
                key={r.id}
                onClick={() => onCardClick(r)}
                style={{
                  cursor: 'pointer',
                  background: mode ? 'color-mix(in srgb, var(--iris) 12%, transparent)' : undefined,
                  borderBottom: '1px solid var(--line)',
                  transition: 'background .15s',
                }}
                onMouseEnter={e => { if (!mode) e.currentTarget.style.background = 'rgba(255,255,255,.04)' }}
                onMouseLeave={e => { if (!mode) e.currentTarget.style.background = '' }}
              >
                <TD>
                  <div style={{ position: 'relative', width: 48, height: 64, borderRadius: 6, overflow: 'hidden', background: '#111', flexShrink: 0 }}>
                    <img
                      src={thumbUrl(r.id)}
                      alt=""
                      style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                      onError={e => { e.target.style.display = 'none' }}
                    />
                    {(isDone || isError) && (
                      <div style={{
                        position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
                        fontSize: '.6rem', fontWeight: 700, textAlign: 'center',
                        background: isDone ? 'color-mix(in srgb, var(--green) 70%, transparent)' : 'color-mix(in srgb, var(--rose) 70%, transparent)',
                        color: '#fff', padding: 2,
                      }}>
                        {isDone ? '✓' : '✗'}
                      </div>
                    )}
                  </div>
                </TD>
                <TD>
                  <div style={{ fontWeight: 700, whiteSpace: 'nowrap' }}>@{r.username}</div>
                  {r.url && (
                    <a href={r.url} target="_blank" rel="noopener noreferrer"
                      style={{ fontSize: '.72rem', color: 'var(--iris)' }}
                      onClick={e => e.stopPropagation()}>↗ Instagram</a>
                  )}
                  {r.duration_sec && (
                    <div style={{ fontSize: '.72rem', color: 'var(--mut)' }}>{fmtSec(r.duration_sec)}</div>
                  )}
                </TD>
                <TD style={{ textAlign: 'right', fontWeight: 700, whiteSpace: 'nowrap' }}>
                  <span className="num">{fmt(r.views)}</span>
                </TD>
                <TD style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  {followers
                    ? <span className="num" title={followersUpdatedAt ? `Спарсено ${fmtDateRu(followersUpdatedAt)}` : undefined}>{fmt(followers)}</span>
                    : <span style={{ color: 'var(--mut)' }}>—</span>
                  }
                </TD>
                <TD style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  {reach != null
                    ? <span style={{ fontWeight: 700, color: reach >= 0.5 ? 'var(--green)' : undefined }}>
                        ×{reach >= 10 ? Math.round(reach) : reach.toFixed(2)}{reach >= 0.5 ? ' 🔥' : ''}
                      </span>
                    : <span style={{ color: 'var(--mut)' }}>—</span>
                  }
                </TD>
                <TD style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <span className="num">{fmt(r.likes)}</span>
                </TD>
                <TD style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  {perLike != null
                    ? <span style={{ color: perLike <= 20 ? 'var(--green)' : undefined }}>
                        1 : {perLike.toLocaleString('ru')}
                      </span>
                    : <span style={{ color: 'var(--mut)' }}>—</span>
                  }
                </TD>
                <TD style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <span className="num">{(r.comments || 0).toLocaleString('ru')}</span>
                </TD>
                <TD onClick={e => e.stopPropagation()}>
                  {isDone
                    ? <div style={{ fontSize: '.72rem', color: 'var(--green)', fontWeight: 700, whiteSpace: 'nowrap' }}>
                        Кликни → отчёт
                      </div>
                    : <div className="modes" style={{ flexDirection: 'column', gap: 4 }}>
                        {[['t', '📝 текст'], ['v', '🎬 кадр'], ['tv', '📝🎬 всё']].map(([m, label]) => (
                          <button
                            key={m}
                            className={mode === m ? 'on' : ''}
                            style={{ fontSize: '.7rem', padding: '3px 8px' }}
                            onClick={() => onSetMode(r.id, mode === m ? null : m)}
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                  }
                </TD>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ── ReelCard ─────────────────────────────────────────────────

function ReelCard({ reel, mode, analysisStatus, onCardClick, onSetMode }) {
  const isDone = analysisStatus === 'done'
  const isError = analysisStatus === 'error' || analysisStatus === 'cancelled'
  const isActive = analysisStatus && !isDone && !isError && analysisStatus !== 'not_started'

  return (
    <div
      className={`reel ${mode ? 'sel' : ''} ${isDone ? 'reel-done' : ''}`}
      onClick={onCardClick}
      style={{ cursor: 'pointer' }}
    >
      <div className="thumb">
        <img src={thumbUrl(reel.id)} alt="" onError={e => { e.target.style.display = 'none' }} />
        {reel.duration_sec && <span className="dur">{fmtSec(reel.duration_sec)}</span>}
        <span className="views">▶ {fmt(reel.views)}</span>
        {(isDone || isError || isActive) && (
          <span className={`done-badge ${isError ? 'done-badge-err' : ''}`}>
            {STATUS_LABELS[analysisStatus] || analysisStatus}
          </span>
        )}
      </div>
      <div className="reel-meta">
        <div className="acc">@{reel.username}</div>
        <div className="reel-stats">
          <span>💬 <span className="num">{(reel.comments || 0).toLocaleString('ru')}</span></span>
          <span>❤ <span className="num">{fmt(reel.likes)}</span></span>
        </div>
        {reel.url && (
          <a href={reel.url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}>
            Открыть в Instagram ↗
          </a>
        )}
        {isDone
          ? <div style={{ fontSize: '.72rem', color: 'var(--green)', fontWeight: 700, marginTop: 6 }}>
              Кликни — открыть отчёт
            </div>
          : <div className="modes">
              {[['t', '📝 текст'], ['v', '🎬 кадр'], ['tv', '📝🎬 всё']].map(([m, label]) => (
                <button
                  key={m}
                  className={mode === m ? 'on' : ''}
                  onClick={e => { e.stopPropagation(); onSetMode(m) }}
                >
                  {label}
                </button>
              ))}
            </div>
        }
      </div>
    </div>
  )
}

// ── ReportPanel (Шаг 4) ──────────────────────────────────────

function ReportPanel({ reel, analysis, allSelectedIds, onClose }) {
  const panelRef = useRef(null)

  useEffect(() => {
    panelRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  const views = reel.views || 0
  const likes = reel.likes || 0
  const comments = reel.comments || 0
  const er = views ? ((likes + comments) / views * 100).toFixed(2) : '0'

  const segments = (() => {
    const s = analysis?.transcript_segments
    if (!s) return null
    if (Array.isArray(s)) return s
    try { return JSON.parse(s) } catch { return null }
  })()

  const timeline = (() => {
    const t = analysis?.visual_timeline
    if (!t) return null
    if (Array.isArray(t)) return t
    try { return JSON.parse(t) } catch { return null }
  })()

  const singleUrl = reportUrl(reel.id)
  const bulkUrl = allSelectedIds.length > 1 ? exportUrl(allSelectedIds) : null

  return (
    <section className="card" ref={panelRef}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div className="step-tag">Шаг 4 — результат по рилсу</div>
        <button className="btn btn-ghost" onClick={onClose} style={{ padding: '6px 14px', fontSize: '.8rem' }}>
          ← Закрыть
        </button>
      </div>
      <h2>@{reel.username}</h2>

      <div className="report">
        {/* Превью */}
        <div className="report-thumb">
          <div className="thumb" style={{ borderRadius: 12, overflow: 'hidden' }}>
            <img src={thumbUrl(reel.id)} alt="" onError={e => { e.target.style.display = 'none' }} />
            {reel.duration_sec && <span className="dur">{fmtSec(reel.duration_sec)}</span>}
            <span className="views">▶ {fmt(views)}</span>
          </div>
        </div>

        {/* Контент */}
        <div>
          {/* Метрики */}
          <div className="r-block">
            <h3>Метрики</h3>
            <span className="badge badge-ok">ER {er}%</span>
            <span className="badge">▶ {fmt(views)} просм.</span>
            <span className="badge">❤ {fmt(likes)} лайков</span>
            <span className="badge">💬 {comments.toLocaleString('ru')} комм.</span>
            {reel.url && (
              <div style={{ marginTop: 8 }}>
                <a href={reel.url} target="_blank" rel="noopener noreferrer"
                  style={{ fontSize: '.8rem', color: 'var(--iris)', fontWeight: 700 }}>
                  Открыть в Instagram ↗
                </a>
              </div>
            )}
          </div>

          {/* Транскрипт */}
          {segments && segments.length > 0 && (
            <div className="r-block">
              <h3>Транскрипт дословно (с таймкодами)</h3>
              <div style={{ fontSize: '.84rem', lineHeight: 1.8 }}>
                {segments.map((seg, i) => (
                  <div key={i}>
                    <span className="num" style={{ color: 'var(--iris)' }}>{fmtSec(seg.start)}</span>
                    {' — «'}{seg.text}{'»'}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Что в кадре */}
          {timeline && timeline.length > 0 && (
            <div className="r-block">
              <h3>Что в кадре (Gemini, с таймкодами)</h3>
              <div style={{ fontSize: '.84rem', lineHeight: 1.8 }}>
                {timeline.map((item, i) => (
                  <div key={i}>
                    <span className="num" style={{ color: 'var(--iris)' }}>{item.t}</span>
                    {' — '}{item.text}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Структура */}
          {(analysis?.hook || analysis?.structure || analysis?.cta || analysis?.format_idea) && (
            <div className="r-block">
              <h3>Структура</h3>
              {analysis.hook && (
                <p style={{ fontSize: '.86rem' }}><b>Хук:</b> {analysis.hook}</p>
              )}
              {analysis.structure && (
                <p style={{ fontSize: '.86rem', marginTop: 6 }}>{analysis.structure}</p>
              )}
              {analysis.cta && (
                <p style={{ fontSize: '.86rem', marginTop: 6 }}><b>CTA:</b> {analysis.cta}</p>
              )}
              {analysis.format_idea && (
                <p style={{ fontSize: '.86rem', marginTop: 6 }}><b>Идея формата:</b> {analysis.format_idea}</p>
              )}
            </div>
          )}

          {/* Пустое состояние */}
          {!segments && !timeline && !analysis?.hook && (
            <div className="hint" style={{ marginTop: 16 }}>
              Данных анализа нет. Запустите анализ с нужным режимом (📝 или 🎬).
            </div>
          )}

          {/* Экспорт */}
          <div style={{ display: 'flex', gap: 10, marginTop: 16, flexWrap: 'wrap' }}>
            <a href={singleUrl} download className="btn btn-ghost">⬇ Скачать отчёт .md</a>
            {bulkUrl && (
              <a href={bulkUrl} download className="btn btn-ghost">⬇ Сводный отчёт по всем выбранным</a>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
