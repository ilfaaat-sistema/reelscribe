import { useState, useEffect, useRef, useCallback } from 'react'
import { parseLinks } from '../lib/utils'
import { importLinks, getSessions } from '../api/client'

const ENGINES = [
  { v: 'faster-whisper', l: 'faster-whisper (локально)' },
  { v: 'yandex', l: 'Yandex SpeechKit' },
  { v: 'deepgram', l: 'Deepgram' },
  { v: 'openai', l: 'OpenAI Whisper' },
]

function fmtDate(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('ru', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export default function Import({ onDone }) {
  const [text, setText] = useState('')
  const [model, setModel] = useState('medium')
  const [engine, setEngine] = useState('faster-whisper')
  const [translate, setTranslate] = useState(true)
  const [pullStats, setPullStats] = useState(true)
  const [comment, setComment] = useState('')
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [dragging, setDragging] = useState(false)
  const dropRef = useRef()

  const parsed = parseLinks(text)

  useEffect(() => {
    getSessions().then(setSessions).catch(() => {})
  }, [])

  const handleFile = useCallback((file) => {
    const r = new FileReader()
    r.onload = (e) => setText((p) => p ? p + '\n' + e.target.result : e.target.result)
    r.readAsText(file, 'utf-8')
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
        source_type: 'paste',
        engine,
        model,
        translate,
        pull_stats: pullStats,
        comment: comment || null,
      })
      onDone(res.session_id, res.total)
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
      <p className="lead">Вставь ссылки или перетащи файл — сервис расшифрует речь, переведёт и покажет метрики</p>

      <div className="grid">
        <div>
          <div
            ref={dropRef}
            className={`drop ${dragging ? 'hot' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <label className="up">
              <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
              <span className="t">Перетащи файл сюда</span>
              <span className="s">.txt, .csv, .json — ссылки или экспорт из Telegram</span>
              <input type="file" accept=".txt,.csv,.json" style={{display:'none'}} onChange={e => e.target.files[0] && handleFile(e.target.files[0])} />
            </label>
            <div className="or">или</div>
            <textarea
              rows={5}
              style={{height: 104}}
              placeholder={"https://www.instagram.com/reel/ABC123/\nhttps://www.instagram.com/p/DEF456/\n…"}
              value={text}
              onChange={e => setText(e.target.value)}
            />
          </div>

          <div className="block">
            <div className="bt">🤖 Модель распознавания</div>
            <div className="cards2">
              <button className={`mcard ${model === 'medium' ? 'on' : ''}`} onClick={() => setModel('medium')}>
                {model === 'medium' && <span className="check">✓</span>}
                <div className="mh">medium</div>
                <div className="mb">Быстро, точность ~85%. Подходит для большинства задач.</div>
                <span className="mt">~10 мин / час аудио</span>
              </button>
              <button className={`mcard ${model === 'large-v3' ? 'on' : ''}`} onClick={() => setModel('large-v3')}>
                {model === 'large-v3' && <span className="check">✓</span>}
                <div className="mh">large-v3</div>
                <div className="mb">Точность ~95%. Лучше для акцентов и шума. Медленнее.</div>
                <span className="mt">~30 мин / час аудио</span>
              </button>
            </div>

            <div className="opts">
              <div className="setting">
                <label>Движок ASR</label>
                <select value={engine} onChange={e => setEngine(e.target.value)}>
                  {ENGINES.map(o => <option key={o.v} value={o.v}>{o.l}</option>)}
                </select>
              </div>
              <div className="setting" style={{justifyContent:'flex-end',paddingBottom:2}}>
                <label className="chkline">
                  <input type="checkbox" checked={translate} onChange={e => setTranslate(e.target.checked)} />
                  Переводить на русский
                </label>
                <label className="chkline" style={{marginTop:6}}>
                  <input type="checkbox" checked={pullStats} onChange={e => setPullStats(e.target.checked)} />
                  Подтягивать метрики
                </label>
              </div>
            </div>

            <div style={{marginTop:14}}>
              <div className="label">Заметка к сессии (необязательно)</div>
              <textarea
                rows={2}
                style={{height:52}}
                placeholder="Например: подборка за июнь, ниша фитнес..."
                value={comment}
                onChange={e => setComment(e.target.value)}
              />
            </div>
          </div>

          <div className="cta">
            <button
              className="btn"
              disabled={!count || loading}
              onClick={submit}
            >
              {loading ? 'Отправка…' : `Запустить обработку ${count ? `· ${count} рилс` : ''}`}
            </button>
            {err && <span style={{color:'var(--rose)',fontSize:12}}>{err}</span>}
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
                <div className="prev-list">
                  {parsed.out.slice(0, 30).map(x => (
                    <div key={x.sc}>
                      <span style={{color: x.type === 'reel' ? 'var(--iris)' : 'var(--teal)'}}>
                        [{x.type}]
                      </span>{' '}
                      {x.sc}
                    </div>
                  ))}
                  {parsed.out.length > 30 && <div>…ещё {parsed.out.length - 30}</div>}
                </div>
              </>
            )}
            {!count && <p style={{color:'var(--faint)',fontSize:12,marginTop:12}}>Вставь ссылки слева — здесь появится превью</p>}
          </div>

          {sessions.length > 0 && (
            <div style={{marginTop:16}}>
              <div className="label">История импортов</div>
              <div className="histlist">
                {sessions.slice(0, 5).map(s => (
                  <div key={s.id} className="hist">
                    <div className="hi-main">
                      <div style={{fontWeight:600,fontSize:13}}>
                        {s.total} рилс · {fmtDate(s.created_at)}
                      </div>
                      <div className="hi-sub">
                        {s.loaded ?? 0} готово · {s.failed ?? 0} ошибок
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
