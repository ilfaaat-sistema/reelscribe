import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import './carousel-note-editor.css'

const LIMIT = 3000
const countChars = value => Array.from(value).length
const storageKey = id => `reelscribe:carousel-note-draft:${id}`

function readDraft(id) {
  try {
    const raw = window.sessionStorage.getItem(storageKey(id))
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function positionFor(anchor) {
  if (typeof window === 'undefined' || window.innerWidth <= 600 || !anchor) return {}
  const width = Math.min(420, Math.max(360, window.innerWidth - 32))
  const gap = 12
  let left = anchor.right + gap
  if (left + width > window.innerWidth - 12) left = anchor.left - width - gap
  left = Math.max(12, Math.min(left, window.innerWidth - width - 12))
  const top = Math.max(12, Math.min(anchor.top ?? anchor.y ?? 12, window.innerHeight - 520))
  return { left, top, width, maxHeight: Math.max(160, window.innerHeight - top - 12) }
}

export default function CarouselNoteEditor({ item, anchorRect, onSave, onClose }) {
  const titleId = useId()
  const initialDraft = useRef(readDraft(item.id))
  const serverRef = useRef({ note: item.note || '', note_revision: item.note_revision ?? 0 })
  const [text, setText] = useState(() => typeof initialDraft.current?.note === 'string' ? initialDraft.current.note : (item.note || ''))
  const [baseRevision, setBaseRevision] = useState(() => Number(initialDraft.current?.expected_revision ?? item.note_revision ?? 0))
  const [baseNote, setBaseNote] = useState(item.note || '')
  const [status, setStatus] = useState(initialDraft.current ? 'Черновик восстановлен на этом устройстве.' : '')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [conflict, setConflict] = useState(null)
  const [storageFailed, setStorageFailed] = useState(false)
  const [position, setPosition] = useState(() => positionFor(anchorRect))
  const rootRef = useRef(null)
  const textareaRef = useRef(null)
  const headingRef = useRef(null)
  const mountedRef = useRef(false)
  const busyRef = useRef(false)
  const previousFocus = useRef(null)
  const noteRef = useRef(text)
  const hasDraft = text !== baseNote

  useEffect(() => {
    mountedRef.current = true
    previousFocus.current = document.activeElement
    const mobile = window.matchMedia('(max-width: 600px)').matches
    ;(mobile ? headingRef.current : textareaRef.current)?.focus({ preventScroll: true })
    return () => {
      mountedRef.current = false
      previousFocus.current?.focus?.({ preventScroll: true })
    }
  }, [])

  useEffect(() => { noteRef.current = text }, [text])
  useEffect(() => {
    const update = () => setPosition(positionFor(anchorRect))
    window.addEventListener('resize', update)
    window.addEventListener('scroll', update, true)
    return () => { window.removeEventListener('resize', update); window.removeEventListener('scroll', update, true) }
  }, [anchorRect])

  const keepDraft = useCallback((draftText = noteRef.current, revision = baseRevision) => {
    if (draftText === baseNote) {
      try { window.sessionStorage.removeItem(storageKey(item.id)); setStorageFailed(false); return true } catch { setStorageFailed(true); return false }
    }
    try {
      window.sessionStorage.setItem(storageKey(item.id), JSON.stringify({ note: draftText, expected_revision: revision }))
      setStorageFailed(false)
      return true
    } catch { setStorageFailed(true); return false }
  }, [baseNote, baseRevision, item.id])

  const close = useCallback(() => {
    if (busyRef.current) return
    const persisted = keepDraft()
    if (!persisted && noteRef.current !== baseNote && !window.confirm('Черновик не удалось сохранить на этом устройстве. Закрыть редактор и потерять несохранённый текст?')) return
    onClose?.()
  }, [baseNote, keepDraft, onClose])

  useEffect(() => {
    const keydown = event => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopImmediatePropagation()
        close()
      } else if (event.key === 'Tab' && rootRef.current) {
        const nodes = [...rootRef.current.querySelectorAll('button:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')]
        if (!nodes.length) { event.preventDefault(); rootRef.current.focus(); return }
        const first = nodes[0], last = nodes[nodes.length - 1]
        if (event.shiftKey && (!nodes.includes(document.activeElement) || document.activeElement === first)) { event.preventDefault(); last.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
      }
    }
    document.addEventListener('keydown', keydown, true)
    return () => document.removeEventListener('keydown', keydown, true)
  }, [close])

  function updateText(value) {
    noteRef.current = value
    setText(value)
    setError('')
    setStatus('')
    keepDraft(value, baseRevision)
  }

  async function save(revision = baseRevision, content = text) {
    if (busyRef.current || countChars(content) > LIMIT) return
    busyRef.current = true
    setBusy(true)
    setError('')
    try {
      const normalized = content.replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, '')
      const result = await onSave({ id: item.id, note: normalized, expected_revision: revision })
      if (!mountedRef.current) return
      if (result?.ok) {
        try { window.sessionStorage.removeItem(storageKey(item.id)) } catch { /* уже сохранённая заметка не зависит от очистки черновика */ }
        onClose?.()
        return
      }
      if (result?.code === 'note_conflict' && result.item) {
        serverRef.current = result.item
        setConflict({ server: result.item, mine: content })
        setStatus('Заметка изменена на другом устройстве. Выберите, какую версию сохранить.')
      } else {
        setError(result?.error || 'Не удалось сохранить заметку. Черновик сохранён.')
        keepDraft(content, revision)
      }
    } catch (cause) {
      if (mountedRef.current) {
        setError(cause?.message || 'Не удалось сохранить заметку. Проверьте соединение и повторите попытку.')
        keepDraft(content, revision)
      }
    } finally {
      busyRef.current = false
      if (mountedRef.current) setBusy(false)
    }
  }

  function takeServer() {
    const server = conflict?.server
    if (!server) return
    noteRef.current = server.note || ''
    setText(server.note || '')
    setBaseNote(server.note || '')
    setBaseRevision(server.note_revision ?? 0)
    setConflict(null)
    setStatus('Загружена сохранённая заметка.')
    try { window.sessionStorage.removeItem(storageKey(item.id)) } catch { setStorageFailed(true) }
  }

  function resetDraft() {
    const server = conflict?.server || serverRef.current
    noteRef.current = server.note || ''
    setText(server.note || '')
    setBaseNote(server.note || '')
    setBaseRevision(server.note_revision ?? 0)
    setConflict(null)
    setError('')
    setStatus('Черновик сброшен. Показана сохранённая заметка.')
    try { window.sessionStorage.removeItem(storageKey(item.id)); setStorageFailed(false) } catch { setStorageFailed(true) }
  }

  function saveMine() {
    if (!conflict?.server) return
    const latest = conflict
    setBaseRevision(latest.server.note_revision ?? 0)
    setConflict(null)
    save(latest.server.note_revision ?? 0, latest.mine)
  }

  const submit = event => { event?.preventDefault(); if (!conflict) save() }
  const onTextareaKeyDown = event => {
    if (!conflict && (event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); save() }
  }

  return createPortal(
    <div className="carousel-note-editor-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) close() }}>
      <section ref={rootRef} className="carousel-note-editor" data-carousel-note-editor role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} style={position}>
        <header className="carousel-note-editor__header">
          <div><h2 id={titleId} ref={headingRef} tabIndex={-1}>Зачем сохранил эту карусель?</h2><p>{item.snapshot?.username ? `@${item.snapshot.username}` : 'Отобранная публикация'}</p></div>
          <button type="button" className="carousel-note-editor__icon" onClick={close} aria-label="Закрыть редактор">×</button>
        </header>
        {conflict && <div className="carousel-note-editor__conflict" role="alert">
          <strong>Заметка изменена на другом устройстве</strong>
          <div><span>Сохранённый вариант</span><pre>{conflict.server.note || 'Пустая заметка'}</pre></div>
          <div><span>Ваш вариант</span><pre>{conflict.mine || 'Пустая заметка'}</pre></div>
          <div className="carousel-note-editor__actions"><button type="button" onClick={takeServer} disabled={busy}>Взять сохранённый</button><button type="button" onClick={saveMine} disabled={busy}>Сохранить мой вариант</button></div>
        </div>}
        <form onSubmit={submit}>
          <label className="carousel-note-editor__label" htmlFor={`${titleId}-text`}>Текст заметки</label>
          <textarea id={`${titleId}-text`} ref={textareaRef} value={text} placeholder="Что зацепило? Как можно использовать это в своём контенте?" onChange={event => updateText(event.target.value)} onKeyDown={onTextareaKeyDown} aria-describedby={`${titleId}-help ${titleId}-count`} disabled={busy || !!conflict} />
          <div className="carousel-note-editor__meta"><span id={`${titleId}-help`}>Обычный текст, до {LIMIT} символов</span><span id={`${titleId}-count`} aria-live="polite">{countChars(text)} / {LIMIT}</span></div>
          {status && <p className="carousel-note-editor__status" role="status">{status}</p>}
          {storageFailed && <p className="carousel-note-editor__warning">Хранилище черновика недоступно. Пока редактор открыт, текст сохранён в памяти.</p>}
          {error && <p className="carousel-note-editor__error" role="alert">{error}</p>}
          <footer className="carousel-note-editor__actions">
            {hasDraft && <button type="button" className="carousel-note-editor__secondary" onClick={resetDraft} disabled={busy}>Сбросить черновик</button>}
            <button type="button" className="carousel-note-editor__secondary" onClick={close} disabled={busy}>Не сейчас</button>
            <button type="submit" className="carousel-note-editor__primary" disabled={busy || !!conflict || countChars(text) > LIMIT}>{busy ? 'Сохранение…' : 'Сохранить заметку'}</button>
          </footer>
        </form>
      </section>
    </div>, document.body
  )
}
