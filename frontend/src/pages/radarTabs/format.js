// Форматирование чисел и дат для вкладок разведки Радара (русский формат).

const dec = (n, digits) => n.toFixed(digits).replace('.', ',')

// 12345 → «12,3к», 1200000 → «1,2 млн», null → «—»
export const fmtNum = n => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—'
  const v = Number(n)
  const a = Math.abs(v)
  if (a >= 1e6) return dec(v / 1e6, 1).replace(/,0$/, '') + ' млн'
  if (a >= 1e5) return Math.round(v / 1e3) + 'к'
  if (a >= 1e3) return dec(v / 1e3, 1).replace(/,0$/, '') + 'к'
  return String(Math.round(v))
}

// 4.2 → «×4,2»
export const fmtX = n => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—'
  return '×' + dec(Number(n), 1)
}

// ISO → «05.10.2026»
export const fmtDate = iso => {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })
}

// ISO → «3 ч назад» / «2 дн назад» / «только что»
export const fmtAgo = iso => {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return '—'
  const min = Math.floor((Date.now() - t) / 60000)
  if (min < 1) return 'только что'
  if (min < 60) return `${min} мин назад`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} ч назад`
  return `${Math.floor(h / 24)} дн назад`
}

// 94 → «94 ₽»
export const fmtRub = n => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—'
  return Math.round(Number(n)).toLocaleString('ru-RU') + ' ₽'
}

// 28571 → «28,6к/день»
export const fmtPerDay = n => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—'
  return fmtNum(n) + '/день'
}
