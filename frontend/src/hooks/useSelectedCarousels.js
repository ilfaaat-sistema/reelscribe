import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchSelectedCarousels, setCarouselSelected } from '../api/selectedCarousels'

export default function useSelectedCarousels() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(new Set())
  const active = useRef(false)
  const reading = useRef(null)
  const mutations = useRef(new Set())
  const revision = useRef(0)

  const refresh = useCallback(async () => {
    if (!active.current || reading.current || mutations.current.size) return false
    const controller = new AbortController()
    reading.current = controller
    const version = revision.current
    try {
      const data = await fetchSelectedCarousels(controller.signal)
      if (active.current && reading.current === controller && version === revision.current) {
        setItems(data.items)
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

  const setSelected = useCallback(async (item, period, selected) => {
    const id = String(item?.id ?? '')
    if (!active.current || mutations.current.has(id)) return false
    mutations.current.add(id)
    revision.current++
    // Чтение, начатое до записи, больше не может заменить подтверждённый список.
    reading.current?.abort()
    reading.current = null
    setBusy(new Set(mutations.current))
    try {
      const saved = await setCarouselSelected(id, period, selected)
      if (!active.current) return false
      setItems(previous => selected ? [...previous.filter(entry => entry.id !== id), saved] : previous.filter(entry => entry.id !== id))
      setError('')
      return true
    } catch (failure) {
      if (active.current) setError(failure.message || 'Не удалось сохранить отметку.')
      return false
    } finally {
      revision.current++
      mutations.current.delete(id)
      if (active.current) {
        setBusy(new Set(mutations.current))
        setLoading(false)
      }
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

  return { items, loading, error, busy, refresh, setSelected }
}
