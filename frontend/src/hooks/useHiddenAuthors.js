import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchHiddenAuthors, importHiddenAuthors, normalizeAuthor, readHiddenAuthors, setAuthorHidden } from '../api/hiddenAuthors'

export default function useHiddenAuthors() {
  const legacy = useRef(null)
  if (legacy.current === null) legacy.current = readHiddenAuthors()
  const [authors, setAuthors] = useState(() => legacy.current.authors)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [warning, setWarning] = useState('')
  const [busy, setBusy] = useState(new Set())
  const active = useRef(false)
  const reading = useRef(null)
  const mutations = useRef(new Set())
  const revision = useRef(0)
  const imported = useRef(false)
  const ready = useRef(false)

  const refresh = useCallback(async () => {
    if (!active.current || reading.current || mutations.current.size) return false
    const controller = new AbortController()
    reading.current = controller
    const version = revision.current
    try {
      let data
      if (!imported.current) {
        const snapshot = legacy.current
        setWarning(snapshot.error ? 'Не удалось перенести локальную резервную копию скрытых авторов. Данные браузера недоступны или повреждены; общий список работает.' : '')
        // Импорт возвращает общий список и завершается до первого обычного чтения.
        if (snapshot.authors.length) data = await importHiddenAuthors(snapshot.authors, controller.signal)
        imported.current = true
      }
      data ||= await fetchHiddenAuthors(controller.signal)
      if (active.current && reading.current === controller && version === revision.current) {
        setAuthors(data.authors)
        ready.current = true
        setError('')
      }
      return true
    } catch (failure) {
      if (active.current && reading.current === controller && version === revision.current && failure.name !== 'AbortError') setError(failure.message)
      return false
    } finally {
      if (reading.current === controller) {
        reading.current = null
        if (active.current) setLoading(false)
      }
    }
  }, [])

  const setHidden = useCallback(async (value, hidden) => {
    let username
    try { username = normalizeAuthor(value) }
    catch (failure) { setError(failure.message); return false }
    if (!active.current || !ready.current || mutations.current.has(username)) return false
    mutations.current.add(username)
    revision.current++
    reading.current?.abort()
    reading.current = null
    setBusy(new Set(mutations.current))
    try {
      const item = await setAuthorHidden(username, hidden)
      if (!active.current) return false
      setAuthors(previous => hidden ? [item, ...previous.filter(entry => entry.username !== username)] : previous.filter(entry => entry.username !== username))
      setError('')
      return true
    } catch (failure) {
      if (active.current) setError(failure.message || 'Не удалось сохранить скрытого автора.')
      return false
    } finally {
      revision.current++
      mutations.current.delete(username)
      if (active.current) setBusy(new Set(mutations.current))
    }
  }, [])

  useEffect(() => {
    active.current = true
    refresh()
    const onFocus = () => { if (document.visibilityState === 'visible') refresh() }
    const timer = window.setInterval(onFocus, 30000)
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onFocus)
    return () => {
      active.current = false
      reading.current?.abort()
      reading.current = null
      window.clearInterval(timer)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onFocus)
    }
  }, [refresh])

  return { authors, loading, error, warning, busy, refresh, setHidden }
}
