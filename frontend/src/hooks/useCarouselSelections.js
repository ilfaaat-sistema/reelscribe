import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getCarouselSelectionStatus, putCarouselSelection, deleteCarouselSelection } from '../api/carouselSelections'
import { useAuth } from '../auth/useAuth'

const PENDING_KEY = 'reels-radar.pending-carousel-selection.v1'

export default function useCarouselSelections(ids, period, navigate, location) {
  const { user, token, loading: authLoading, signOut } = useAuth()
  const [value, setValue] = useState({ owner: null, states: {}, total: null, loading: false, error: '', busy: {} })
  const generation = useRef(0)
  const mutationGeneration = useRef(0)
  const mutationsInFlight = useRef(0)
  const readVersion = useRef(0)
  const account = useRef(null)
  const refreshRef = useRef(null)
  const idsKey = useMemo(() => [...new Set(ids.filter(Boolean).map(String))].join(','), [ids])
  const identity = user?.id || null
  account.current = { identity, token }
  const current = value.owner === identity ? value : { owner: identity, states: {}, total: null, loading: !!token, error: '', busy: {} }

  useEffect(() => {
    mutationGeneration.current += 1
    mutationsInFlight.current = 0
  }, [identity, token])

  const refresh = useCallback(async (signal) => {
    if (!token || !identity) return null
    if (mutationsInFlight.current > 0) return null
    const version = generation.current
    const read = ++readVersion.current
    const mutationVersion = mutationGeneration.current
    const selectedIds = idsKey ? idsKey.split(',') : []
    const batches = selectedIds.length ? Array.from({ length: Math.ceil(selectedIds.length / 100) }, (_, i) => selectedIds.slice(i * 100, i * 100 + 100)) : [[]]
    const responses = []
    for (const batch of batches) responses.push(await getCarouselSelectionStatus(batch, token, signal))
    if (version !== generation.current || read !== readVersion.current || mutationVersion !== mutationGeneration.current || signal?.aborted
        || account.current.identity !== identity || account.current.token !== token) return null
    const states = Object.fromEntries(responses.flatMap(response => response.states || []).map(state => [String(state.id), state]))
    setValue(previous => previous.owner !== identity ? previous : { ...previous, states, total: responses[0]?.selected_total ?? null, loading: false, error: '' })
    return states
  }, [token, identity, idsKey])
  refreshRef.current = refresh

  useEffect(() => {
    generation.current += 1
    const effectGeneration = generation.current
    const controller = new AbortController()
    if (!token || !identity) {
      setValue({ owner: identity, states: {}, total: null, loading: false, error: '', busy: {} })
      return () => controller.abort()
    }
    setValue(previous => ({ ...previous, owner: identity, loading: true, error: '', states: {}, busy: {} }))
    refresh(controller.signal).catch(error => {
      if (!controller.signal.aborted && account.current.identity === identity && account.current.token === token && generation.current === effectGeneration) {
        if (error.status === 401) signOut()
        setValue(previous => previous.owner !== identity ? previous : { ...previous, loading: false, error: error.message })
      }
    })
    const onFocus = () => refresh(controller.signal).catch(() => {})
    window.addEventListener('focus', onFocus)
    const timer = window.setInterval(() => {
      if (!document.hidden) refresh(controller.signal).catch(() => {})
    }, 30000)
    return () => { controller.abort(); window.removeEventListener('focus', onFocus); window.clearInterval(timer) }
  }, [token, identity, refresh, signOut])

  const perform = useCallback(async (id, sourcePeriod, forceSelect = false) => {
    id = String(id)
    if (!token || !identity) {
      try { sessionStorage.setItem(PENDING_KEY, JSON.stringify({ id, period: sourcePeriod, path: location.pathname + location.search, createdAt: Date.now() })) } catch { /* навигация всё равно откроет вход */ }
      navigate(`/sign-in?${new URLSearchParams({ returnTo: location.pathname + location.search })}`)
      return
    }
    if (current.loading || current.busy[id]) return
    mutationGeneration.current += 1
    mutationsInFlight.current += 1
    let conflict = false
    let saved = false
    setValue(previous => ({ ...previous, busy: { ...previous.busy, [id]: true }, error: '' }))
    try {
      let state = current.states[id]
      if (!state) {
        const result = await getCarouselSelectionStatus([id], token)
        state = result.states?.[0] || { id, selected: false, revision: 0 }
      }
      if (account.current.identity !== identity || account.current.token !== token) return
      const result = forceSelect || !state.selected
        ? await putCarouselSelection(id, { source_period: sourcePeriod, expected_revision: state.revision }, token)
        : await deleteCarouselSelection(id, state.revision, token)
      if (account.current.identity !== identity || account.current.token !== token) return
      setValue(previous => previous.owner !== identity ? previous : ({ ...previous,
        states: { ...previous.states, [id]: { id, selected: !!(result.item?.selected), revision: result.item?.revision ?? result.revision ?? state.revision } },
        total: result.selected_total, error: '' }))
      saved = true
    } catch (error) {
      if (account.current.identity !== identity || account.current.token !== token) return
      if (error.status === 401) signOut()
      setValue(previous => previous.owner !== identity ? previous : { ...previous, error: error.message || 'Не удалось сохранить отметку' })
      if (error.status === 409) conflict = true
    } finally {
      if (account.current.identity === identity && account.current.token === token) {
        mutationsInFlight.current = Math.max(0, mutationsInFlight.current - 1)
        mutationGeneration.current += 1
        setValue(previous => previous.owner !== identity ? previous : { ...previous, busy: { ...previous.busy, [id]: false } })
        if ((conflict || saved) && mutationsInFlight.current === 0) refreshRef.current?.().catch(() => {})
      }
    }
  }, [token, identity, current.loading, current.busy, current.states, navigate, location.pathname, location.search, signOut])

  useEffect(() => {
    if (authLoading || !token || !identity || current.loading) return
    let pending
    try { pending = JSON.parse(sessionStorage.getItem(PENDING_KEY) || 'null') } catch { return }
    if (!pending || pending.path !== location.pathname + location.search || !/^\d{1,40}$/.test(pending.id)
        || !Number.isFinite(pending.createdAt) || Date.now() - pending.createdAt > 10 * 60 * 1000) {
      if (pending) sessionStorage.removeItem(PENDING_KEY)
      return
    }
    sessionStorage.removeItem(PENDING_KEY)
    perform(pending.id, pending.period || period || '14d', true)
  }, [authLoading, token, identity, current.loading, perform, period, location.pathname, location.search])

  return { ...current, toggle: perform, refresh }
}
