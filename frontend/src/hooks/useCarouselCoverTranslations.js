import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react'
import { carouselCoverTranslationStore } from '../api/carouselCoverTranslations.js'

// Чистый контроллер позволяет проверить непрерывные 500 мс без браузерной автоматизации.
export function createCoverVisibilityGate({ onVisible, isVisible, schedule = setTimeout, cancel = clearTimeout }) {
  let timer = null
  let release = null
  return {
    update(intersecting) {
      if (!intersecting || !isVisible()) {
        if (timer != null) cancel(timer)
        timer = null
        release?.()
        release = null
        return
      }
      if (release || timer != null) return
      timer = schedule(() => {
        timer = null
        if (isVisible()) release = onVisible()
      }, 500)
    },
    dispose() {
      if (timer != null) cancel(timer)
      timer = null
      release?.()
      release = null
    },
  }
}

export default function useCarouselCoverTranslations(id, period, rootRef) {
  const targetRef = useRef(null)
  const subscribe = useCallback(listener => carouselCoverTranslationStore.subscribe(listener), [])
  const getSnapshot = useCallback(() => carouselCoverTranslationStore.getSnapshot(id), [id])
  const item = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)

  useEffect(() => {
    const target = targetRef.current?.closest('.carousels-card') || targetRef.current
    if (!target || !id || !period || typeof IntersectionObserver === 'undefined') return undefined
    let intersecting = false
    const gate = createCoverVisibilityGate({
      onVisible: () => carouselCoverTranslationStore.watch(id, period),
      isVisible: () => document.visibilityState === 'visible',
    })
    const reconcile = () => gate.update(intersecting)
    const observer = new IntersectionObserver(entries => {
      intersecting = entries.some(entry => entry.isIntersecting && entry.intersectionRatio > 0)
      reconcile()
    }, { root: rootRef?.current || null, threshold: [0, 0.01] })
    observer.observe(target)
    document.addEventListener('visibilitychange', reconcile)
    return () => { gate.dispose(); observer.disconnect(); document.removeEventListener('visibilitychange', reconcile) }
  }, [id, period, rootRef])

  return { item, targetRef }
}
