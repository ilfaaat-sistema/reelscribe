import { useCallback, useEffect, useRef, useState } from 'react'
import { AuthContext } from './AuthContext'

const STORAGE_KEY = 'reels-radar.auth.session.v1'
const BASE = `${import.meta.env.VITE_API_URL || ''}/api/auth`

function readSession() {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
    return value?.access_token && value?.refresh_token && value?.user?.id && Number.isFinite(value.expires_at) ? value : null
  } catch { return null }
}

async function request(path, body) {
  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), cache: 'no-store',
    })
  } catch { throw new Error('Нет соединения с сервером. Повторите попытку.') }
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const error = new Error(response.status === 429 ? 'Слишком много попыток. Подождите и попробуйте снова.'
      : response.status === 400 || response.status === 401 ? 'Проверьте адрес и код. Возможно, код уже истёк.'
        : 'Не удалось выполнить вход. Повторите попытку.')
    error.status = response.status
    throw error
  }
  return data
}

export function AuthProvider({ children }) {
  const [session, setSession] = useState(readSession)
  const [loading, setLoading] = useState(true)
  const current = useRef(session)
  const pending = useRef(null)
  const generation = useRef(0)

  const save = useCallback((value) => {
    current.current = value
    setSession(value)
    try {
      if (value) localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
      else localStorage.removeItem(STORAGE_KEY)
    } catch { /* При запрете хранилища сессия остаётся доступна в этой вкладке. */ }
  }, [])

  const refresh = useCallback(() => {
    if (pending.current) return pending.current
    const previous = current.current
    if (!previous) return Promise.resolve()
    const version = generation.current
    pending.current = request('/refresh', { refresh_token: previous.refresh_token })
      .then(data => {
        if (version !== generation.current) return
        if (!data?.access_token || !data?.refresh_token || !data?.user?.id || !(data.expires_in > 0)) throw new Error('Некорректный ответ сервера авторизации.')
        save({ ...data, expires_at: Date.now() + data.expires_in * 1000 })
      })
      .catch(error => {
        if (version === generation.current && (error.status === 400 || error.status === 401 || error.status === 403)) save(null)
        throw error
      })
      .finally(() => { pending.current = null })
    return pending.current
  }, [save])

  useEffect(() => {
    let active = true
    const initial = current.current
    const ready = initial && initial.expires_at <= Date.now() + 60000 ? refresh() : Promise.resolve()
    ready.catch(() => {}).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [refresh])

  useEffect(() => {
    if (!session) return
    let timer
    const schedule = () => {
      const delay = Math.max(1000, Math.min(2147483647, current.current.expires_at - Date.now() - 60000))
      timer = setTimeout(() => {
        refresh().catch(() => {
          // Временная ошибка сети не удаляет действующий refresh token.
          if (current.current) timer = setTimeout(schedule, 30000)
        })
      }, delay)
    }
    schedule()
    const onFocus = () => {
      if (current.current && current.current.expires_at <= Date.now() + 60000) refresh().catch(() => {})
    }
    window.addEventListener('focus', onFocus)
    return () => { clearTimeout(timer); window.removeEventListener('focus', onFocus) }
  }, [session, refresh])

  useEffect(() => {
    const onStorage = event => {
      if (event.key === STORAGE_KEY || event.key === null) {
        generation.current += 1
        const value = readSession()
        current.current = value
        setSession(value)
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const sendCode = useCallback(email => request('/email/start', { email: email.trim() }), [])
  const signIn = useCallback(async (email, code) => {
    const version = ++generation.current
    const data = await request('/email/verify', { email: email.trim(), code: code.trim() })
    if (!data?.access_token || !data?.refresh_token || !data?.user?.id || !(data.expires_in > 0)) throw new Error('Некорректный ответ сервера авторизации.')
    if (version === generation.current) save({ ...data, expires_at: Date.now() + data.expires_in * 1000 })
    return data.user
  }, [save])
  const signOut = useCallback(() => {
    const accessToken = current.current?.access_token
    generation.current += 1
    save(null)
    if (accessToken) fetch(`${BASE}/logout`, { method: 'POST',
      headers: { Authorization: `Bearer ${accessToken}` }, cache: 'no-store' }).catch(() => {})
  }, [save])

  return <AuthContext.Provider value={{ user: session?.user || null, token: session?.access_token || null, loading, sendCode, signIn, signOut }}>{children}</AuthContext.Provider>
}
