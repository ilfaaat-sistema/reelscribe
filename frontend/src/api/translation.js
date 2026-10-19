const BASE = (import.meta.env.VITE_API_URL || '') + '/api'

// Совпадает с правилом pipeline.translate.needs_translation для кнопки в drawer.
export function needsTranslation(text) {
  if (!text) return false
  const letters = [...text.matchAll(/\p{L}/gu)].map(m => m[0])
  if (letters.length < 12) return false
  const cyrillic = letters.filter(ch => /[а-яё]/iu.test(ch)).length
  return cyrillic / letters.length < 0.3
}

export function validTranslation(source, translated) {
  if (!translated?.trim()) return false
  const lower = translated.toLowerCase()
  if (['error 500 (server error)', "that's an error", 'that’s an error', 'too many requests'].some(x => lower.includes(x))) return false
  if (/<\s*[a-z][a-z0-9]*[^>]*>/i.test(translated)) return false
  if (source?.length >= 80 && translated.length < source.length * 0.2) return false
  return translated.trim() !== source?.trim() && !needsTranslation(translated)
}

export async function translateReel(id, field) {
  let response
  try {
    response = await fetch(`${BASE}/reels/${encodeURIComponent(id)}/translate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ field }),
    })
  } catch {
    throw new Error('Нет соединения с сервером')
  }
  if (!response.ok) {
    let message = 'Не удалось перевести текст'
    try {
      const data = await response.json()
      if (typeof data?.detail === 'string') message = data.detail
    } catch { /* Сервер мог ответить без JSON. */ }
    throw new Error(message)
  }
  return response.json()
}
