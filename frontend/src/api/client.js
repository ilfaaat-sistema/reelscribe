const BASE = (import.meta.env.VITE_API_URL || '') + '/api'

async function req(path, opts = {}) {
  const r = await fetch(BASE + path, opts)
  if (!r.ok) {
    const txt = await r.text()
    throw new Error(txt || r.statusText)
  }
  const ct = r.headers.get('content-type') || ''
  return ct.includes('json') ? r.json() : r.blob()
}

export const importLinks = (body) =>
  req('/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const getSessions = () => req('/sessions')

export const getSession = (id) => req(`/sessions/${id}`)

export const updateSessionComment = (id, comment) =>
  req(`/sessions/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ comment }),
  })

export const getReels = (params = {}) => {
  const qs = new URLSearchParams()
  const mapped = { ...params }
  // map frontend desc:bool -> backend dir:asc|desc
  if ('desc' in mapped) {
    mapped.dir = mapped.desc === 'true' || mapped.desc === true ? 'desc' : 'asc'
    delete mapped.desc
  }
  // map frontend limit -> backend page (approximate)
  if (mapped.limit) delete mapped.limit
  Object.entries(mapped).forEach(([k, v]) => v != null && v !== '' && qs.set(k, v))
  return req(`/reels?${qs}`)
}

export const getReel = (id) => req(`/reels/${id}`)

export const updateNote = (id, note) =>
  req(`/reels/${id}/note`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note }),
  })

export const getProgress = (sessionId) =>
  req(`/progress?session=${sessionId}`)

export const exportUrl = (params = {}) =>
  `${BASE}/export?${new URLSearchParams(params)}`
