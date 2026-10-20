import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../auth/useAuth'
import './SignIn.css'

export default function SignIn() {
  const { user, loading, sendCode, signIn } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [sent, setSent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  let returnTo = '/radar/carousels'
  try {
    const target = new URL(params.get('returnTo') || returnTo, window.location.origin)
    if (target.origin === window.location.origin && ['/radar/carousels', '/radar/carousels/selected'].includes(target.pathname)) returnTo = target.pathname + target.search + target.hash
  } catch { /* Некорректный адрес заменяется маршрутом каруселей. */ }

  useEffect(() => {
    if (!loading && user) navigate(returnTo, { replace: true })
  }, [user, loading, navigate, returnTo])

  const submit = async event => {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      if (sent) await signIn(email, code)
      else { await sendCode(email); setSent(true) }
    } catch (failure) { setError(failure.message || 'Не удалось войти. Повторите попытку.') }
    finally { setBusy(false) }
  }

  return <main className="sign-in">
    <section className="sign-in-card" aria-labelledby="sign-in-title">
      <div className="kicker">Личная подборка</div>
      <h1 id="sign-in-title">Вход по почте</h1>
      <p>{sent ? `Введите код из письма на ${email}.` : 'Пришлём одноразовый код для входа в вашу подборку каруселей.'}</p>
      <form onSubmit={submit}>
        <label htmlFor="sign-in-email">Электронная почта</label>
        <input id="sign-in-email" type="email" autoComplete="email" required value={email} disabled={sent || busy || loading} onChange={event => setEmail(event.target.value)} />
        {sent && <><label htmlFor="sign-in-code">Код из письма</label><input id="sign-in-code" type="text" inputMode="numeric" autoComplete="one-time-code" required autoFocus value={code} disabled={busy} onChange={event => setCode(event.target.value)} /></>}
        {error && <div className="sign-in-error" role="alert">{error}</div>}
        <button className="btn" disabled={busy || loading} type="submit">{busy ? 'Подождите…' : sent ? 'Войти' : 'Получить код'}</button>
        {sent && <button className="btn ghost" type="button" disabled={busy} onClick={() => { setSent(false); setCode(''); setError('') }}>Изменить почту или отправить код снова</button>}
        <button className="btn ghost" type="button" onClick={() => { sessionStorage.removeItem('reels-radar.pending-carousel-selection.v1'); navigate('/radar/carousels?period=14d') }}>Отмена</button>
      </form>
    </section>
  </main>
}
