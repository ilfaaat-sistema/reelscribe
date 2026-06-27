import { useState, useEffect, useRef } from 'react'
import { getReel, updateNote } from '../api/client'
import { fmtV, fmtPct, erClass } from '../lib/utils'

function Avatar({ handle }) {
  const ch = (handle || '?')[0].toUpperCase()
  return <div className="av">{ch}</div>
}

export default function ReelDrawer({ reelId, onClose }) {
  const [reel, setReel] = useState(null)
  const [showRu, setShowRu] = useState(false)
  const [showCaption, setShowCaption] = useState(true)
  const [note, setNote] = useState('')
  const noteRef = useRef(null)
  const saveTimer = useRef(null)

  useEffect(() => {
    if (!reelId) return
    setReel(null)
    setNote('')
    getReel(reelId).then(r => {
      setReel(r)
      setNote(r.note || '')
    }).catch(() => {})
  }, [reelId])

  function handleNote(val) {
    setNote(val)
    clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => {
      updateNote(reelId, val).catch(() => {})
    }, 700)
  }

  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const open = !!reelId
  const er = reel?.er
  const erCls = er ? erClass(er) : 'x3'
  const txOrig = reel?.transcript_text || ''
  const txRu = reel?.transcript_text_ru || ''
  const hasTr = !!txOrig || !!txRu
  const displayTx = showRu ? (txRu || txOrig) : txOrig

  return (
    <>
      <div className={`drawer-back ${open ? 'on' : ''}`} onClick={onClose} />
      <div className={`drawer ${open ? 'on' : ''}`}>
        <button className="dclose" onClick={onClose}>✕</button>

        {reel && (
          <>
            <a
              className="vprev"
              href={reel.url}
              target="_blank"
              rel="noopener noreferrer"
              style={{display:'block'}}
            >
              <span className="vtag">
                <span className={`tag ${reel.type === 'reel' ? 't-reel' : reel.type === 'tv' ? 't-tv' : 't-post'}`}>{reel.type}</span>
              </span>
              <span className="vopen">открыть ↗</span>
              <span className="play-btn" />
              <span className="vlabel">
                @{reel.author_handle} · {fmtV(reel.views)} просмотров
              </span>
            </a>

            <div className="dbody">
              <div className="author-row">
                <Avatar handle={reel.author_handle} />
                <div>
                  <div className="an">@{reel.author_handle}</div>
                  <div className="af">{fmtV(reel.author_followers)} подписчиков</div>
                </div>
              </div>

              <div className="dmetrics">
                <div className="dm">
                  <div className="dm-ic">👁</div>
                  <div className="v">{fmtV(reel.views)}</div>
                  <div className="l">просмотры</div>
                </div>
                <div className="dm">
                  <div className="dm-ic">❤️</div>
                  <div className="v">{fmtV(reel.likes)}</div>
                  <div className="l">лайки</div>
                </div>
                <div className="dm">
                  <div className="dm-ic">💬</div>
                  <div className="v">{fmtV(reel.comments)}</div>
                  <div className="l">комменты</div>
                </div>
                <div className="dm">
                  <div className="dm-ic">🔥</div>
                  <div className={`v er ${erCls}`}>{er ? er.toFixed(1) + '×' : '—'}</div>
                  <div className="l">залётность</div>
                </div>
                <div className="dm">
                  <div className="dm-ic">⚡</div>
                  <div className="v">{fmtPct(reel.lpf)}</div>
                  <div className="l">лайки/подп</div>
                </div>
                <div className="dm">
                  <div className="dm-ic">🗨️</div>
                  <div className="v">{fmtPct(reel.cpf)}</div>
                  <div className="l">комм/подп</div>
                </div>
                <div className="dm">
                  <div className="dm-ic">📊</div>
                  <div className="v">{fmtPct(reel.eng)}</div>
                  <div className="l">вовлечённость</div>
                </div>
                {reel.posted_at && (
                  <div className="dm">
                    <div className="dm-ic">📅</div>
                    <div className="v" style={{fontSize:11}}>{reel.posted_at?.slice(0,10)}</div>
                    <div className="l">дата</div>
                  </div>
                )}
              </div>

              {reel.caption && (
                <>
                  <div
                    className="seclabel posthead"
                    onClick={() => setShowCaption(c => !c)}
                  >
                    📝 Текст поста {showCaption ? '▾' : '▸'}
                  </div>
                  {showCaption && (
                    <div className="postcard">{reel.caption}</div>
                  )}
                </>
              )}

              {hasTr ? (
                <>
                  <div className="seclabel" style={{justifyContent:'space-between'}}>
                    <span>🎙 Расшифровка</span>
                    {txRu && (
                      <div style={{display:'flex',gap:6}}>
                        <button
                          className="btn sm ghost"
                          style={{padding:'3px 9px',fontSize:11}}
                          onClick={() => setShowRu(false)}
                        >
                          Оригинал
                        </button>
                        <button
                          className="btn sm ghost"
                          style={{padding:'3px 9px',fontSize:11,background: showRu ? 'var(--teal)' : undefined, borderColor: showRu ? 'var(--teal)' : undefined}}
                          onClick={() => setShowRu(true)}
                        >
                          Русский
                        </button>
                      </div>
                    )}
                  </div>
                  {reel.transcript_status === 'failed' ? (
                    <div className="failtx">
                      ✗ Ошибка: {reel.transcript_text || 'неизвестная ошибка'}
                    </div>
                  ) : (
                    <div className={`paper ${showRu ? 'ru' : ''}`}>
                      {displayTx || <span style={{color:'var(--faint)'}}>Расшифровка пуста</span>}
                    </div>
                  )}
                </>
              ) : (
                <div className="seclabel">
                  🎙 Расшифровка
                  <span style={{color:'var(--faint)',fontFamily:'var(--sans)',textTransform:'none',fontSize:12}}>
                    {reel.transcript_status === 'queued' ? '— в очереди' :
                     reel.transcript_status === 'downloading' ? '— скачиваем…' :
                     reel.transcript_status === 'transcribing' ? '— распознаём…' :
                     reel.transcript_status === 'translating' ? '— переводим…' :
                     '— нет данных'}
                  </span>
                </div>
              )}

              <div className="seclabel" style={{marginTop:20}}>✏️ Заметка</div>
              <textarea
                className="dnote"
                ref={noteRef}
                placeholder="Добавь заметку к этому рилсу…"
                value={note}
                onChange={e => handleNote(e.target.value)}
              />
            </div>
          </>
        )}

        {!reel && reelId && (
          <div style={{padding:40,textAlign:'center',color:'var(--faint)'}}>Загрузка…</div>
        )}
      </div>
    </>
  )
}
