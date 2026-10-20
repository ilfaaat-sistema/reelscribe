import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getCarousels } from '../api/carousels'
import { carouselFeedBaseKey, carouselRefreshInterval, mergeCarouselItems, readCarouselPrefix, sameCarouselSnapshot } from '../api/carouselFeed'

export default function useCarouselFeed(query) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState('')
  const [version, setVersion] = useState(0)
  const queryKey = JSON.stringify(query)
  const stableQuery = useMemo(() => JSON.parse(queryKey), [queryKey])
  const baseKey = carouselFeedBaseKey(stableQuery)
  const previousBase = useRef(null)
  const generation = useRef(0)
  const cursor = useRef(0)
  const wanted = useRef(stableQuery.limit)
  const busy = useRef(true)
  const moreBusy = useRef(false)
  const appendController = useRef(null)
  const currentData = useRef(null)
  const lastAutomaticRefresh = useRef(0)
  currentData.current = data
  const refresh = useCallback(() => setVersion(value => value + 1), [])

  useEffect(() => {
    const controller = new AbortController()
    const requestGeneration = ++generation.current
    appendController.current?.abort()
    moreBusy.current = false
    setLoadingMore(false)
    setMoreError('')
    if (previousBase.current !== baseKey) {
      wanted.current = stableQuery.limit
      cursor.current = 0
      setData(null)
    }
    previousBase.current = baseKey
    busy.current = true
    setLoading(true)
    setError('')
    readCarouselPrefix(getCarousels, stableQuery, wanted.current, controller.signal).then(result => {
      if (generation.current !== requestGeneration || controller.signal.aborted) return
      cursor.current = result.cursor
      wanted.current = Math.max(stableQuery.limit, result.cursor)
      setData(result.report)
    }).catch(e => {
      if (e.name !== 'AbortError' && generation.current === requestGeneration) setError(e.message || 'Не удалось обновить карусели')
    }).finally(() => {
      if (generation.current === requestGeneration && !controller.signal.aborted) {
        busy.current = false
        setLoading(false)
      }
    })
    return () => { controller.abort(); appendController.current?.abort() }
  }, [stableQuery, baseKey, version])

  const loadMore = useCallback(async () => {
    const current = currentData.current
    if (busy.current || moreBusy.current || !current || cursor.current >= current.total) return
    const requestGeneration = generation.current
    const offset = cursor.current
    const controller = new AbortController()
    appendController.current = controller
    moreBusy.current = true
    setLoadingMore(true)
    setMoreError('')
    try {
      const next = await getCarousels({ ...stableQuery, offset }, controller.signal)
      if (generation.current !== requestGeneration || controller.signal.aborted) return
      if (!sameCarouselSnapshot(current, next)) {
        wanted.current = Math.max(wanted.current, offset + stableQuery.limit)
        refresh()
        return
      }
      const merged = mergeCarouselItems(current.items, next.items, stableQuery.sort, stableQuery.order)
      if (merged.length !== current.items.length + next.items.length) throw new Error('Карточки повторились. Обновите список.')
      if (!next.items.length && offset < next.total) throw new Error('Пустая порция: обновите список или повторите позже.')
      cursor.current = next.items.length ? offset + next.items.length : next.total
      wanted.current = Math.max(stableQuery.limit, cursor.current)
      setData({ ...next, items: merged })
    } catch (e) {
      if (e.name !== 'AbortError' && generation.current === requestGeneration) setMoreError(e.message || 'Не удалось подгрузить карусели')
    } finally {
      if (generation.current === requestGeneration && !controller.signal.aborted) {
        moreBusy.current = false
        setLoadingMore(false)
      }
    }
  }, [stableQuery, refresh])

  useEffect(() => {
    const interval = carouselRefreshInterval(stableQuery.period, data?.collection?.status)
    if (!interval) return undefined
    const refreshVisible = () => {
      const now = Date.now()
      if (document.visibilityState === 'hidden' || busy.current || moreBusy.current || now - lastAutomaticRefresh.current < 1000) return
      lastAutomaticRefresh.current = now
      refresh()
    }
    const timer = window.setInterval(refreshVisible, interval)
    window.addEventListener('focus', refreshVisible)
    document.addEventListener('visibilitychange', refreshVisible)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshVisible)
      document.removeEventListener('visibilitychange', refreshVisible)
    }
  }, [stableQuery.period, data?.collection?.status, refresh])


  return { data, loading, error, loadingMore, moreError, loadMore, refresh,
    hasMore: Boolean(data && cursor.current < data.total) }
}
