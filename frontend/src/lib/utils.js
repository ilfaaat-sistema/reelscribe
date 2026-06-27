export const fmtV = (n) =>
  !n ? '—' :
  n >= 1e6 ? (n / 1e6).toFixed(1).replace('.', ',') + ' млн' :
  n >= 1e3 ? Math.round(n / 1e3) + ' тыс' :
  String(n)

export const fmtPct = (n) =>
  n == null || n === '' ? '—' : Number(n).toFixed(1).replace('.', ',') + '%'

export const erClass = (e) => e >= 10 ? 'x10' : e >= 5 ? 'x5' : 'x3'

const RE_URL = /instagram\.com\/(reel|p|tv)\/([A-Za-z0-9_-]+)/g

export function parseLinks(text) {
  const seen = new Set()
  const out = []
  let dup = 0
  let m
  RE_URL.lastIndex = 0
  while ((m = RE_URL.exec(text))) {
    const sc = m[2]
    const kind = m[1]
    const type = kind === 'reel' ? 'reel' : kind === 'tv' ? 'tv' : 'post'
    if (seen.has(sc)) { dup++; continue }
    seen.add(sc)
    out.push({ sc, type, url: `https://www.instagram.com/${kind}/${sc}/` })
  }
  return { out, dup }
}
