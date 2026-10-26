import { useCallback, useEffect, useRef, useState } from 'react'

export default function useCarouselNotes(selection) {
  const [editor, setEditor] = useState(null)
  const [notice, setNotice] = useState('')
  const active = useRef(false)
  const sequence = useRef(0)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const close = useCallback(() => {
    sequence.current++
    setEditor(null)
    try {
      const draft = Object.keys(sessionStorage).some(key => key.startsWith('reelscribe:carousel-note-draft:'))
      setNotice(previous => previous === 'Заметка сохранена.' ? previous : draft ? 'Черновик заметки сохранён в этой вкладке.' : '')
    } catch { /* Редактор уже предупредил о недоступном хранилище. */ }
  }, [])
  const open = (item, event) => {
    if (!item) return
    sequence.current++
    setNotice('')
    setEditor({ item, anchorRect: (event?.currentTarget || document.activeElement)?.getBoundingClientRect?.().toJSON() })
  }
  const toggle = async (item, period, event) => {
    const exists = selection.items.some(entry => entry.id === String(item.id))
    const anchorRect = (event?.currentTarget || document.activeElement)?.getBoundingClientRect?.().toJSON()
    const version = ++sequence.current
    const result = await selection.setSelected(item, period, !exists)
    if (result && !exists && active.current && version === sequence.current && typeof result === 'object') {
      setNotice('')
      setEditor({ item: result, anchorRect })
    }
    return result
  }
  const save = async input => {
    const result = await selection.saveNote(input)
    if (result.ok && active.current) setNotice('Заметка сохранена.')
    return result
  }
  return { editor, notice, open, toggle, close, save }
}
