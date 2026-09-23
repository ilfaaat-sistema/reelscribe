import { useState, useEffect, useRef, useCallback } from 'react'
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
  const [showCaptionRu, setShowCaptionRu] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [note, setNote] = useState('')
  const [loadError, setLoadError] = useState(null)
  const noteRef = useRef(null)
  const saveTimer = useRef(null)

  const fetchReel = useCallback(() => {
    if (!reelId) return
    setReel(null)
    setNote('')
    setLoadError(null)
    setPlaying(false)
    getReel(reelId).then(r => {
      setReel(r)
      setNote(r.note || '')
    }).catch(e => setLoadError(e.message || 'неизвестная ошибка'))
  }, [reelId])

  useEffect(() => { fetchReel() }, [fetchReel])

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
  const capOrig = reel?.caption || ''
  const capRu = reel?.caption_ru || ''
  const displayCap = showCaptionRu ? (capRu || capOrig) : capOrig

  return (
    <>
      <div className={`drawer-back ${open ? 'on' : ''}`} onClick={onClose} />
      <div className={`drawer ${open ? 'on' : ''}`}>
        <button className="dclose" onClick={onClose}>✕</button>

        {reel && (
          <>
            {playing ? (
              /* Публичный проигрыватель Instagram. Путь /p/ универсален: так открываются и рилсы,
                 и посты, и карусели. Удалённые и закрытые публикации показывают заглушку самого
                 Instagram — для них рядом остаётся ссылка «открыть». */
              <div className="vembed-wrap">
                <div className="vbar">
                  <button className="vcollapse" type="button" onClick={() => setPlaying(false)}>↩ свернуть</button>
                  <a href={reel.url} target="_blank" rel="noopener noreferrer">открыть в Instagram ↗</a>
                </div>
                <iframe
                  className="vembed"
                  src={`https://www.instagram.com/p/${reel.shortcode}/embed/`}
                  title={`Публикация @${reel.author_handle}`}
                  loading="lazy"
                  allow="autoplay; encrypted-media; picture-in-picture"
                  allowFullScreen
                  scrolling="no"
                />
              </div>
            ) : (
              <button className="vprev" type="button" onClick={() => setPlaying(true)} title="Смотреть здесь">
                <span className="vtag">
                  <span className={`tag ${reel.type === 'reel' ? 't-reel' : reel.type === 'tv' ? 't-tv' : 't-post'}`}>{reel.type}</span>
                </span>
                <a
                  className="vopen"
                  href={reel.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={e => e.stopPropagation()}
                >открыть ↗</a>
                <span className="play-btn" />
                <span className="vlabel">
                  @{reel.author_handle} · {fmtV(reel.views)} просмотров
                </span>
              </button>
            )}

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
                    style={{justifyContent:'space-between'}}
                    onClick={() => setShowCaption(c => !c)}
                  >
                    <span>📝 Текст поста {showCaption ? '▾' : '▸'}</span>
                    {reel.caption_ru && (
                      <div style={{display:'flex',gap:6}} onClick={e => e.stopPropagation()}>
                        <button
                          className="btn sm ghost"
                          style={{padding:'3px 9px',fontSize:11}}
                          onClick={() => setShowCaptionRu(false)}
                        >
                          Оригинал
                        </button>
                        <button
                          className="btn sm ghost"
                          style={{padding:'3px 9px',fontSize:11,background: showCaptionRu ? 'var(--teal)' : undefined, borderColor: showCaptionRu ? 'var(--teal)' : undefined}}
                          onClick={() => setShowCaptionRu(true)}
                        >
                          Русский
                        </button>
                      </div>
                    )}
                  </div>
                  {showCaption && (
                    <div className="postcard">{displayCap}</div>
                  )}
                </>
              )}

              {hasTr ? (
                <>
                  <div className="seclabel" style={{justifyContent:'space-between'}}>
                    <span>
                      🎙 Расшифровка
                      {reel.transcript_language && reel.transcript_language !== 'ru' && (
                        <span className="trbadge lang" style={{marginLeft:8}} title="язык оригинала">
                          {reel.transcript_language}
                        </span>
                      )}
                    </span>
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
                      ✗ Ошибка: {reel.fail_reason || reel.transcript_text || 'неизвестная ошибка'}
                    </div>
                  ) : (
                    <div className={`paper ${showRu ? 'ru' : ''}`}>
                      {displayTx || <span style={{color:'var(--faint)'}}>Расшифровка пуста</span>}
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="seclabel">
                    🎙 Расшифровка
                    <span style={{color:'var(--faint)',fontFamily:'var(--sans)',textTransform:'none',fontSize:12}}>
                      {reel.transcript_status === 'queued' ? '— в очереди' :
                       reel.transcript_status === 'downloading' ? '— скачиваем…' :
                       reel.transcript_status === 'transcribing' ? '— распознаём…' :
                       reel.transcript_status === 'translating' ? '— переводим…' :
                       reel.transcript_status === 'no_audio' ? '— 📷 фото/карусель, нет аудио' :
                       reel.transcript_status === 'failed' ? '— ошибка' :
                       '— нет данных'}
                    </span>
                  </div>
                  {reel.fail_reason && (
                    <div className="failtx" style={{marginTop:8}}>
                      Причина: {reel.fail_reason}
                    </div>
                  )}
                </>
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

        {!reel && reelId && loadError && (
          <div style={{padding:40,textAlign:'center',color:'var(--rose)'}}>
            Не удалось загрузить рилс: {loadError}
            <div style={{marginTop:14}}>
              <button className="btn sm ghost" onClick={fetchReel}>Повторить</button>
            </div>
          </div>
        )}

        {!reel && reelId && !loadError && (
          <div style={{padding:40,textAlign:'center',color:'var(--faint)'}}>Загрузка…</div>
        )}
      </div>
    </>
  )
}
