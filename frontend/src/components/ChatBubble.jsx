import { useState, useEffect, useRef, useCallback } from 'react'
import { sendChat } from '../api/client'
import { fmtV, erClass } from '../lib/utils'
import ReelDrawer from './ReelDrawer'
import './chat.css'

// История чата — только для удобства пользователя между визитами, не прод-данные
// (сами транскрипты/метрики живут в Supabase). Ключ фиксирован ТЗ 09.
const STORE_KEY = 'rs:chat:v1'
const HISTORY_LIMIT = 10

function loadMessages() {
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (!raw) return []
    const data = JSON.parse(raw)
    return Array.isArray(data) ? data : []
  } catch {
    return []
  }
}

function saveMessages(list) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(list))
  } catch {
    // приватный режим / переполненное хранилище — история просто не переживёт перезагрузку
  }
}

function stepsWord(n) {
  const mod10 = n % 10
  const mod100 = n % 100
  if (mod10 === 1 && mod100 !== 11) return 'шаг'
  if (mod10 >= 2 && mod10 <= 4 && !(mod100 >= 12 && mod100 <= 14)) return 'шага'
  return 'шагов'
}

function fmtCost(v) {
  const n = Number(v) || 0
  return n.toFixed(2).replace('.', ',')
}

// Рендер ответа модели: только переносы строк и **жирный**, без markdown-библиотек
// и без dangerouslySetInnerHTML — собираем обычные React-узлы.
function renderAnswer(text) {
  const lines = String(text || '').split('\n')
  return lines.map((line, i) => {
    const parts = line.split(/(\*\*[^*]+\*\*)/g).filter((p) => p !== '')
    return (
      <div key={i}>
        {parts.length === 0
          ? ' '
          : parts.map((part, j) =>
              part.startsWith('**') && part.endsWith('**') ? (
                <strong key={j}>{part.slice(2, -2)}</strong>
              ) : (
                <span key={j}>{part}</span>
              )
            )}
      </div>
    )
  })
}

function CloudIcon({ open }) {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      {open ? (
        <path d="M6 6L18 18M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      ) : (
        <>
          <path
            d="M7 15.5C4.79 15.5 3 13.71 3 11.5C3 9.42 4.58 7.72 6.6 7.53C7.14 5.46 9.02 4 11.2 4C13.55 4 15.5 5.71 15.93 8.01C17.94 8.24 19.5 9.95 19.5 12C19.5 14.21 17.71 16 15.5 16H7V15.5Z"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinejoin="round"
          />
          <circle cx="9" cy="10.8" r="0.9" fill="currentColor" />
          <circle cx="12" cy="10.8" r="0.9" fill="currentColor" />
          <circle cx="15" cy="10.8" r="0.9" fill="currentColor" />
        </>
      )}
    </svg>
  )
}

function ChatCard({ reel, onOpen }) {
  const er = reel.er
  return (
    <button type="button" className="chat-card" onClick={() => onOpen(reel)}>
      <span className="chat-card-author">@{reel.author || '?'}</span>
      <span className="chat-card-meta">
        {fmtV(reel.views)}
        {er != null && (
          <>
            {' · '}
            <span className={`er ${erClass(er)}`}>{Number(er).toFixed(1)}×</span>
          </>
        )}
      </span>
      {reel.snippet && <span className="chat-card-snippet">{reel.snippet}</span>}
    </button>
  )
}

export default function ChatBubble() {
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState(loadMessages)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [drawerReelId, setDrawerReelId] = useState(null)
  const logRef = useRef(null)
  const drawerReelIdRef = useRef(null)

  useEffect(() => {
    drawerReelIdRef.current = drawerReelId
  }, [drawerReelId])

  useEffect(() => {
    saveMessages(messages)
  }, [messages])

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [messages, sending])

  // Esc закрывает панель чата, только если поверх неё не открыт ReelDrawer —
  // тогда Escape должен закрыть именно карточку рилса (её собственный обработчик).
  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape' && open && !drawerReelIdRef.current) {
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const handleOpenReel = useCallback((reel) => {
    if (reel.source === 'parser') {
      setDrawerReelId(reel.id)
    } else {
      window.open(reel.url, '_blank', 'noopener,noreferrer')
    }
  }, [])

  // Персист-эффект (saveMessages) сам запишет пустой список при следующем рендере —
  // отдельный localStorage.removeItem тут не нужен и создавал бы гонку с этим эффектом.
  const handleClear = useCallback(() => {
    setMessages([])
  }, [])

  async function handleSubmit(e) {
    e.preventDefault()
    const text = input.trim()
    if (!text || sending) return
    const history = messages.slice(-HISTORY_LIMIT).map((m) => ({ role: m.role, text: m.text }))
    setMessages((prev) => [...prev, { role: 'user', text }])
    setInput('')
    setSending(true)
    try {
      const data = await sendChat({ message: text, history })
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', text: data.answer, reels: data.reels || [], usage: data.usage || null },
      ])
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', error: true, text: err.message || 'Не удалось получить ответ' },
      ])
    } finally {
      setSending(false)
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit(e)
    }
  }

  return (
    <>
      <button
        type="button"
        className={`chat-fab ${open ? 'is-open' : ''}`}
        onClick={() => setOpen((o) => !o)}
        aria-label={open ? 'Закрыть ИИ-чат' : 'Открыть ИИ-чат'}
      >
        <CloudIcon open={open} />
      </button>

      <div className={`chat-panel ${open ? 'is-open' : ''}`}>
        <div className="chat-head">
          <span>ИИ-чат по базе</span>
          <div className="chat-head-actions">
            <button type="button" className="chat-clear" onClick={handleClear}>
              Очистить
            </button>
            <button type="button" className="chat-close" onClick={() => setOpen(false)} aria-label="Закрыть">
              ✕
            </button>
          </div>
        </div>

        <div className="chat-log" ref={logRef}>
          {messages.length === 0 && (
            <div className="chat-empty">
              Спросите что-нибудь про базу рилсов — например «кто рассказывал про очки с Гермесом».
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`chat-msg ${m.role}${m.error ? ' error' : ''}`}>
              {renderAnswer(m.text)}
              {Array.isArray(m.reels) && m.reels.length > 0 && (
                <div className="chat-cards">
                  {m.reels.map((r) => (
                    <ChatCard key={`${r.source}-${r.id}`} reel={r} onOpen={handleOpenReel} />
                  ))}
                </div>
              )}
              {m.usage && (
                <div className="chat-cost">
                  {fmtCost(m.usage.cost_rub)} ₽ · {m.usage.steps} {stepsWord(m.usage.steps)}
                </div>
              )}
            </div>
          ))}
          {sending && <div className="chat-typing">Ищу в базе…</div>}
        </div>

        <form className="chat-form" onSubmit={handleSubmit}>
          <textarea
            className="chat-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Спросите про базу рилсов…"
            rows={1}
          />
          <button type="submit" className="chat-send" disabled={sending || !input.trim()} aria-label="Отправить">
            ➤
          </button>
        </form>
      </div>

      <ReelDrawer reelId={drawerReelId} onClose={() => setDrawerReelId(null)} />
    </>
  )
}
