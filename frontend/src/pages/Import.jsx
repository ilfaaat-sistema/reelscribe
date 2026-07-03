import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { parseLinks } from '../lib/utils'
import { importLinks, getSessions, previewImport } from '../api/client'

function fmtDate(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('ru', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

const MODELS = [
  {
    v: 'medium',
    label: 'medium',
    desc: 'MLX Whisper на Apple Silicon. Быстро, точность ~85%.',
    badge: '~1 мин / рилс',
    tip: 'Работает локально через mlx-whisper — использует Neural Engine M1/M2. Не требует интернета и внешних сервисов.',
  },
  {
    v: 'large-v3',
    label: 'large-v3',
    desc: 'Запускается на Kaggle GPU. Точность ~95%, акценты и шум.',
    badge: '~2 мин / рилс (GPU)',
    tip: 'Обрабатывается на бесплатном T4 GPU в Kaggle. Требует настроенного KAGGLE_KEY и KAGGLE_NOTEBOOK_ID в .env. Запускается автоматически при нажатии «Запустить».',
  },
]

function detectSourceType(filename) {
  const name = filename.toLowerCase()
  if (name.endsWith('.xlsx') || name.endsWith('.xls')) return 'csv'
  if (name.endsWith('.csv')) return 'csv'
  if (name.endsWith('.json')) return 'json'
  if (name.endsWith('.txt')) return 'txt'
  return 'paste'
}

async function readFileAsText(file) {
  const name = file.name.toLowerCase()
  if (name.endsWith('.xlsx') || name.endsWith('.xls')) {
    const { read, utils } = await import('xlsx')
    const buf = await file.arrayBuffer()
    const wb = read(buf, { type: 'array' })
    const lines = []
    for (const sheetName of wb.SheetNames) {
      const rows = utils.sheet_to_json(wb.Sheets[sheetName], { header: 1 })
      for (const row of rows) {
        for (const cell of row) {
          if (typeof cell === 'string' && cell.includes('instagram.com')) lines.push(cell)
        }
      }
    }
    return lines.join('\n')
  }
  return new Promise((res, rej) => {
    const r = new FileReader()
    r.onload = e => res(e.target.result)
    r.onerror = rej
    r.readAsText(file, 'utf-8')
  })
}

export default function Import() {
  const navigate = useNavigate()
  const [text, setText] = useState('')
  const [model, setModel] = useState('medium')
  const [translate, setTranslate] = useState(true)
  const [pullStats, setPullStats] = useState(true)
  const [comment, setComment] = useState('')
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [dragging, setDragging] = useState(false)
  const [tooltip, setTooltip] = useState(null)
  const [preview, setPreview] = useState(null)
  const [sourceType, setSourceType] = useState('paste')
  const [fileErr, setFileErr] = useState('')
  const fileRef = useRef()
  const previewTimer = useRef(null)

  const parsed = parseLinks(text)

  useEffect(() => {
    getSessions().then(setSessions).catch(() => {})
  }, [])

  // Дебаунс: сколько вставленных ссылок уже распознаны ранее (подтянутся из кэша).
  useEffect(() => {
    clearTimeout(previewTimer.current)
    if (!parsed.out.length) { setPreview(null); return }
    previewTimer.current = setTimeout(() => {
      previewImport(text).then(setPreview).catch(() => setPreview(null))
    }, 500)
    return () => clearTimeout(previewTimer.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text])

  const handleFile = useCallback(async (file) => {
    setFileErr('')
    try {
      const content = await readFileAsText(file)
      setSourceType(detectSourceType(file.name))
      setText(p => p ? p + '\n' + content : content)
    } catch (e) {
      console.error('Ошибка чтения файла', e)
      setFileErr('Не удалось прочитать файл. Проверь формат (.txt, .csv, .json, .xlsx) и попробуй снова.')
    }
  }, [])

  function onDrop(e) {
    e.preventDefault()
    setDragging(false)
    const f = e.dataTransfer.files[0]
    if (f) handleFile(f)
  }

  async function submit() {
    if (!parsed.out.length) return
    setLoading(true)
    setErr('')
    try {
      const res = await importLinks({
        links_text: text,
        source_type: sourceType,
        engine: 'faster-whisper',
        model,
        translate,
        pull_stats: pullStats,
        comment: comment || null,
      })
      navigate(`/processing/${res.session_id}`)
    } catch (e) {
      setErr(e.message)
      setLoading(false)
    }
  }

  const count = parsed.out.length
  const reels = parsed.out.filter(x => x.type === 'reel').length
  const posts = parsed.out.filter(x => x.type !== 'reel').length

  return (
    <div className="imp">
      <p className="kicker">Шаг 1 из 3</p>
      <h2 className="hero">Загрузи ссылки<br/>на Instagram Reels</h2>
      <p className="lead">Вставь ссылки или прикрепи файл — сервис расшифрует речь, переведёт и покажет метрики</p>

      <div className="grid">
        <div>
          {/* Textarea с drag-and-drop и скрепкой */}
          <div
            className={`textarea-wrap ${dragging ? 'hot' : ''}`}
            onDragOver={e => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <textarea
              rows={6}
              placeholder={"https://www.instagram.com/reel/ABC123/\nhttps://www.instagram.com/p/DEF456/\n\nИли перетащи сюда файл (.txt, .csv, .json, .xlsx)"}
              value={text}
              onChange={e => { setText(e.target.value); setSourceType('paste') }}
            />
            <button
              className="clip-btn"
              title="Прикрепить файл (.txt, .csv, .json, .xlsx)"
              onClick={() => fileRef.current?.click()}
            >
              📎
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.csv,.json,.xlsx,.xls"
              style={{ display: 'none' }}
              onChange={e => e.target.files[0] && handleFile(e.target.files[0])}
            />
          </div>
          {fileErr && <p style={{ color: 'var(--rose)', fontSize: 12, marginTop: 6 }}>{fileErr}</p>}

          <div className="block">
            <div className="bt">🤖 Модель распознавания</div>
            <div className="cards2">
              {MODELS.map(m => (
                <button
                  key={m.v}
                  className={`mcard ${model === m.v ? 'on' : ''}`}
                  onClick={() => setModel(m.v)}
                  onMouseEnter={() => setTooltip(m.v)}
                  onMouseLeave={() => setTooltip(null)}
                  style={{ position: 'relative' }}
                >
                  {model === m.v && <span className="check">✓</span>}
                  <div className="mh">{m.label}</div>
                  <div className="mb">{m.desc}</div>
                  <span className="mt">{m.badge}</span>
                  {tooltip === m.v && (
                    <div className="model-tip">{m.tip}</div>
                  )}
                </button>
              ))}
            </div>

            <div className="opts">
              <div className="setting" style={{ justifyContent: 'flex-end', paddingBottom: 2 }}>
                <label className="chkline">
                  <input type="checkbox" checked={translate} onChange={e => setTranslate(e.target.checked)} />
                  Переводить на русский
                </label>
                <label className="chkline" style={{ marginTop: 6 }}>
                  <input type="checkbox" checked={pullStats} onChange={e => setPullStats(e.target.checked)} />
                  Подтягивать метрики
                </label>
              </div>
            </div>

            <div style={{ marginTop: 14 }}>
              <div className="label">Заметка к сессии (необязательно)</div>
              <textarea
                rows={2}
                style={{ height: 52 }}
                placeholder="Например: подборка за июнь, ниша фитнес..."
                value={comment}
                onChange={e => setComment(e.target.value)}
              />
            </div>
          </div>

          <div className="cta">
            <button className="btn" disabled={!count || loading} onClick={submit}>
              {loading ? 'Отправка…' : `Запустить обработку${count ? ` · ${count} рилс` : ''}`}
            </button>
            {err && <span style={{ color: 'var(--rose)', fontSize: 12 }}>{err}</span>}
          </div>
        </div>

        <div>
          <div className="detected">
            <div className="det-num">{count}</div>
            <div className="det-sub">ссылок обнаружено</div>
            {count > 0 && (
              <>
                <div className="det-break">
                  <div className="minib"><div className="v">{reels}</div><div className="l">Reels</div></div>
                  <div className="minib"><div className="v">{posts}</div><div className="l">Posts</div></div>
                  <div className="minib"><div className="v">{parsed.dup}</div><div className="l">Дубли</div></div>
                </div>
                {preview && preview.already_done > 0 && (
                  <div style={{ fontSize: 12, color: 'var(--teal)', marginTop: 10 }}>
                    ✓ уже распознано ранее: {preview.already_done} из {preview.total} — подтянем из кэша,
                    распознаём только новые ({preview.new})
                  </div>
                )}
                {posts > 0 && (
                  <div style={{ fontSize: 12, color: 'var(--faint)', marginTop: 6 }}>
                    📷 постов (/p/): {posts} — у многих нет аудио, расшифровка будет только у видео
                  </div>
                )}
                <div className="prev-list">
                  {parsed.out.slice(0, 30).map(x => (
                    <div key={x.sc}>
                      <span style={{ color: x.type === 'reel' ? 'var(--iris)' : 'var(--teal)' }}>
                        [{x.type}]
                      </span>{' '}
                      {x.sc}
                    </div>
                  ))}
                  {parsed.out.length > 30 && <div>…ещё {parsed.out.length - 30}</div>}
                </div>
              </>
            )}
            {!count && <p style={{ color: 'var(--faint)', fontSize: 12, marginTop: 12 }}>Вставь ссылки слева — здесь появится превью</p>}
          </div>

          {sessions.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <button
                className="btn sm ghost"
                style={{ width: '100%', marginBottom: 10 }}
                onClick={() => navigate('/results')}
              >
                📊 Все распознавания →
              </button>
              <div className="label">История импортов</div>
              <div className="histlist">
                {sessions.slice(0, 5).map(s => (
                  <div
                    key={s.id}
                    className="hist"
                    style={{ cursor: 'pointer' }}
                    onClick={() => navigate(`/results/${s.id}`)}
                  >
                    <div className="hi-main">
                      <div style={{ fontWeight: 600, fontSize: 13 }}>
                        {s.total} рилс · {fmtDate(s.created_at)}
                      </div>
                      <div className="hi-sub">
                        {s.loaded ?? 0} готово · {s.failed ?? 0} ошибок
                        {(s.queued ?? 0) > 0 ? ` · ${s.queued} в очереди` : ''}
                      </div>
                      {s.comment && <div className="hi-note">{s.comment}</div>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
