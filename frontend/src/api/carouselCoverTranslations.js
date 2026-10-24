const BASE = (import.meta.env?.VITE_API_URL || '') + '/api/radar/carousel-cover-translations'
const STATUSES = new Set(['missing', 'running', 'ready', 'not_needed', 'no_text', 'unreadable', 'image_unavailable', 'error', 'busy', 'limit_reached', 'disabled'])

const MAX_TIMER_DELAY = 2 ** 31 - 1

function normalizeUtcTimestamp(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null
  const [year, month, day] = value.slice(0, 10).split('-').map(Number)
  const days = [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  if (month < 1 || month > 12 || day < 1 || day > days[month - 1]) return null
  const milliseconds = Date.parse(value)
  return Number.isFinite(milliseconds) ? new Date(milliseconds).toISOString() : null
}

function normalizeBudget(value) {
  if (!['pilot', 'live'].includes(value?.mode) || value.pilot_limit !== 20 ||
      !Number.isInteger(value.daily_limit) || ![500, 2500].includes(value.daily_limit)) return null
  const resetAt = normalizeUtcTimestamp(value.reset_at)
  if (!resetAt) return null
  const result = { mode: value.mode, pilot_limit: 20, daily_limit: value.daily_limit, reset_at: resetAt }
  for (const key of ['attempts', 'daily_attempts', 'active_slots', 'available_slots']) {
    if (Number.isSafeInteger(value[key]) && value[key] >= 0) result[key] = value[key]
  }
  return result
}

function normalizeItem(item, id) {
  return {
    id: String(id ?? item?.id ?? ''),
    status: STATUSES.has(item?.status) ? item.status : 'error',
    source_text: typeof item?.source_text === 'string' ? item.source_text : '',
    text_ru: typeof item?.text_ru === 'string' ? item.text_ru : '',
    retry_after: normalizeUtcTimestamp(item?.retry_after),
    reason_code: typeof item?.reason_code === 'string' && /^[a-z_]{1,80}$/.test(item.reason_code) ? item.reason_code : null,
    attempt_recorded: item?.attempt_recorded === true,
  }
}

async function request(path, options = {}) {
  const response = await fetch(BASE + path, options)
  if (!response.ok) throw new Error(`Перевод обложки недоступен (${response.status})`)
  return response.json()
}

export async function getCarouselCoverTranslations(ids, signal) {
  const unique = [...new Set(ids.map(String))]
  if (!unique.length || unique.length > 100) throw new Error('Нужно от 1 до 100 обложек')
  const data = await request(`?${new URLSearchParams({ ids: unique.join(',') })}`, { signal })
  const received = new Map((data.items || []).map(item => [String(item.id), item]))
  return { enabled: data.enabled === true, budget: normalizeBudget(data.budget), items: unique.map(id => normalizeItem(received.get(id) || { status: 'error' }, id)) }
}

export async function translateCarouselCover(id, period, signal) {
  const data = await request(`/${encodeURIComponent(id)}`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ period }),
  })
  return normalizeItem(data.item, id)
}

// Очередь живёт дольше карточек: перерисовка, Drawer и смена периода используют один результат.
export function createCarouselCoverTranslationStore({
  read = getCarouselCoverTranslations, start = translateCarouselCover,
  isVisible = () => typeof document !== 'undefined' && document.visibilityState === 'visible',
  now = () => Date.now(), schedule = setTimeout, cancel = clearTimeout,
  focusTarget = typeof window === 'undefined' ? null : window,
  visibilityTarget = typeof document === 'undefined' ? null : document,
} = {}) {
  const entries = new Map()
  const listeners = new Set()
  let active = 0
  let timer = null
  let enabled = null
  let budget = null
  let listening = false
  const emit = () => listeners.forEach(listener => listener())
  const update = (entry, item) => { entry.item = normalizeItem(item, entry.id); emit() }
  const eligible = entry => entry.watchers.size > 0 && !entry.pending
  const getEntry = id => {
    id = String(id)
    if (!entries.has(id)) entries.set(id, {
      id, item: normalizeItem({ status: 'missing' }, id),
      watchers: new Map(), checked: false, canStart: true, busyCount: 0, readAt: 0, pending: false, locked: false, lastBudgetCheck: null,
    })
    return entries.get(id)
  }
  const waitingForBudget = entry => entry.item.status === 'limit_reached' && entry.item.retry_after && !entry.locked
  const needsRead = entry => !entry.checked || entry.item.status === 'running' || waitingForBudget(entry)
  const accept = (entry, item) => {
    entry.checked = true
    update(entry, item)
    const status = entry.item.status
    if (status === 'running') {
      entry.canStart = false
      entry.locked = true
      entry.readAt = now() + 2000
    } else if (status === 'busy') {
      entry.busyCount++
      entry.canStart = !entry.locked && entry.busyCount < 3
      if (entry.canStart) {
        entry.checked = false
        entry.readAt = now() + (entry.busyCount < 2 ? 2000 : 5000)
      }
    } else if (status === 'limit_reached') {
      // Отказ бюджета не является оплаченной попыткой. Повтор разрешает только новый GET.
      entry.canStart = !entry.locked
      const retryAt = entry.item.retry_after ? Date.parse(entry.item.retry_after) : Infinity
      // Просроченная дата, повторённая сервером, не создаёт бесконечный цикл чтения.
      entry.readAt = !entry.locked && (entry.lastBudgetCheck !== entry.item.retry_after || retryAt > now()) ? retryAt : Infinity
    } else if (status === 'missing' && !entry.canStart) {
      // Неопределённый исход уже начатой обработки нельзя превращать в новый платный запуск.
      entry.locked = true
      update(entry, { status: 'error' })
    } else if (status !== 'missing') {
      entry.canStart = false
      entry.locked = true
    }
  }
  const fail = entry => {
    entry.checked = true
    entry.canStart = false
    entry.locked = true
    update(entry, { status: 'error' })
  }

  function wake() {
    if (!isVisible()) { pump(); return }
    entries.forEach(entry => {
      if (eligible(entry) && waitingForBudget(entry) && Date.parse(entry.item.retry_after) <= now()) entry.readAt = now()
    })
    pump()
  }
  function listen() {
    if (listening) return
    focusTarget?.addEventListener('focus', wake)
    visibilityTarget?.addEventListener('visibilitychange', wake)
    listening = true
  }
  function unlisten() {
    if (!listening || [...entries.values()].some(entry => entry.watchers.size)) return
    focusTarget?.removeEventListener('focus', wake)
    visibilityTarget?.removeEventListener('visibilitychange', wake)
    listening = false
  }

  function pump() {
    if (timer != null) { cancel(timer); timer = null }
    if (!isVisible()) return
    while (active < 2) {
      const readable = [...entries.values()].filter(entry => eligible(entry) && needsRead(entry) && entry.readAt <= now())
      if (readable.length) {
        const batch = readable.slice(0, 100)
        batch.forEach(entry => {
          entry.pending = true
          if (waitingForBudget(entry)) entry.lastBudgetCheck = entry.item.retry_after
        })
        active++
        Promise.resolve(read(batch.map(entry => entry.id))).then(data => {
          enabled = data.enabled === true
          budget = normalizeBudget(data.budget)
          const byId = new Map(data.items.map(item => [String(item.id), item]))
          batch.forEach(entry => {
            const item = byId.get(entry.id) || { status: 'error' }
            accept(entry, !enabled && ['missing', 'busy', 'disabled'].includes(item.status) ? { status: 'disabled' } : item)
          })
          if (!enabled) entries.forEach(entry => {
            if (!entry.pending && ['missing', 'busy'].includes(entry.item.status)) update(entry, { status: 'disabled' })
          })
        }).catch(() => batch.forEach(fail)).finally(() => {
          active--; batch.forEach(entry => { entry.pending = false }); pump()
        })
        continue
      }
      const entry = [...entries.values()].find(value => eligible(value) && enabled === true && value.checked &&
        value.canStart && value.item.status === 'missing')
      if (!entry) break
      entry.pending = true
      entry.canStart = false
      const period = entry.watchers.values().next().value
      update(entry, { status: 'running' })
      active++
      Promise.resolve(start(entry.id, period)).then(item => {
        accept(entry, item)
      }).catch(() => fail(entry)).finally(() => { active--; entry.pending = false; pump() })
    }
    const due = [...entries.values()].filter(entry => eligible(entry) && needsRead(entry) && Number.isFinite(entry.readAt)).map(entry => entry.readAt)
    if (due.length && active < 2) timer = schedule(pump, Math.min(MAX_TIMER_DELAY, Math.max(1, Math.min(...due) - now())))
  }

  return {
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    getSnapshot(id) { return getEntry(id).item },
    getBudgetSnapshot() { return budget },
    watch(id, period) {
      const entry = getEntry(id)
      const token = Symbol()
      entry.watchers.set(token, period)
      listen()
      if (waitingForBudget(entry) && Date.parse(entry.item.retry_after) <= now()) entry.readAt = now()
      // Соседние видимые карточки собираются в один GET до любого POST.
      if (timer != null) cancel(timer)
      timer = schedule(pump, 0)
      return () => { entry.watchers.delete(token); unlisten(); pump() }
    },
    wake,
  }
}

export const carouselCoverTranslationStore = createCarouselCoverTranslationStore()
