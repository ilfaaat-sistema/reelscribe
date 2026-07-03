import { useState } from 'react'
import { exportUrl } from '../api/client'
import { fmtV } from '../lib/utils'

const FMTS = [
  { v: 'csv', label: 'CSV', icon: '📊', desc: 'Excel, Google Sheets, любой табличный редактор', ext: '.csv' },
  { v: 'xlsx', label: 'XLSX', icon: '📗', desc: 'Excel с форматированием, заголовки на русском', ext: '.xlsx' },
  { v: 'json', label: 'JSON', icon: '{ }', desc: 'Для разработчиков, API, Make/n8n интеграции', ext: '.json' },
]

// Все столбцы таблицы для экспорта/Google Sheets (значение берётся из строки рилса).
const SHEET_COLS = [
  ['Тип', r => r.type],
  ['Дата', r => (r.posted_at ? r.posted_at.slice(0, 10) : '')],
  ['Автор', r => (r.author_handle ? '@' + r.author_handle : '')],
  ['Подписчики', r => r.author_followers ?? ''],
  ['Просмотры', r => r.views ?? ''],
  ['Залётность', r => r.er ?? ''],
  ['Лайки', r => r.likes ?? ''],
  ['Комменты', r => r.comments ?? ''],
  ['Лайки/подп %', r => r.lpf ?? ''],
  ['Комм/подп %', r => r.cpf ?? ''],
  ['Вовлечённость %', r => r.eng ?? ''],
  ['Статус', r => r.transcript_status ?? ''],
  ['Ссылка', r => r.url],
  ['Текст поста', r => r.caption || ''],
]

function cleanParams(obj) {
  const o = {}
  Object.entries(obj).forEach(([k, v]) => { if (v != null && v !== '') o[k] = v })
  return o
}

export default function ExportModal({ reels, total, selected, query = {}, onClose }) {
  const [fmt, setFmt] = useState('csv')
  const [lang, setLang] = useState('ru')
  const [scope, setScope] = useState('all')
  const [copied, setCopied] = useState(false)

  const rowsForScope = scope === 'selected'
    ? reels.filter(r => selected.includes(r.id))
    : reels
  const count = scope === 'selected' ? rowsForScope.length : (total ?? rowsForScope.length)
  const preview = rowsForScope.slice(0, 5)

  const allColLabels = [...SHEET_COLS.map(c => c[0]), 'Расшифровка', ...(lang === 'both' ? ['Расшифровка RU'] : []), 'Заметка']

  function doExport() {
    const base = cleanParams({ format: fmt, lang, ...query })
    if (scope === 'selected' && selected.length) {
      const url = new URL(exportUrl({ ...base, scope: 'selected' }), window.location.href)
      selected.forEach(id => url.searchParams.append('ids', id))
      window.open(url.toString(), '_blank')
      return
    }
    window.open(exportUrl(base), '_blank')
  }

  async function copyForSheets() {
    const txCol = lang === 'orig'
      ? ['Расшифровка', r => r.transcript_text || '']
      : ['Расшифровка', r => r.transcript_text_ru || r.transcript_text || '']
    const cols = [...SHEET_COLS, txCol, ...(lang === 'both' ? [['Расшифровка RU', r => r.transcript_text_ru || '']] : [])]
    const clean = v => String(v ?? '').replace(/[\t\n\r]+/g, ' ')
    const head = cols.map(c => c[0]).join('\t')
    const body = rowsForScope.map(r => cols.map(c => clean(c[1](r))).join('\t')).join('\n')
    try {
      await navigator.clipboard.writeText(head + '\n' + body)
      setCopied(true)
      setTimeout(() => setCopied(false), 3000)
      window.open('https://sheets.new', '_blank')
    } catch (_) {
      alert('Не удалось скопировать. Используй кнопку «Скачать CSV» и импортируй в таблицу.')
    }
  }

  return (
    <div className="modal-back" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="sheet">
        <h3>📦 Экспорт</h3>
        <p style={{ color: 'var(--mut)', fontSize: 13, margin: '4px 0 0' }}>
          {count} рилс · {allColLabels.length} столбцов · выберите формат и язык расшифровки
        </p>

        <div className="fmts">
          {FMTS.map(f => (
            <button key={f.v} className={`fmt ${fmt === f.v ? 'on' : ''}`} onClick={() => setFmt(f.v)}>
              <div className="fh">
                <span style={{ fontSize: 18 }}>{f.icon}</span>
                <span>{f.label}</span>
                <span className="ext">{f.ext}</span>
              </div>
              <div className="fd">{f.desc}</div>
            </button>
          ))}
        </div>

        <div className="scope">
          <span>Язык расшифровки:</span>
          {[['ru', 'Русский'], ['orig', 'Оригинал'], ['both', 'Оба']].map(([v, l]) => (
            <button key={v} className={`chip ${lang === v ? 'on' : ''}`} style={{ padding: '4px 10px' }} onClick={() => setLang(v)}>{l}</button>
          ))}
        </div>

        <div className="scope" style={{ marginTop: 8 }}>
          <span>Объём:</span>
          <button className={`chip ${scope === 'all' ? 'on' : ''}`} style={{ padding: '4px 10px' }} onClick={() => setScope('all')}>Все ({total ?? reels.length})</button>
          {selected.length > 0 && (
            <button className={`chip ${scope === 'selected' ? 'on' : ''}`} style={{ padding: '4px 10px' }} onClick={() => setScope('selected')}>Выбранные ({selected.length})</button>
          )}
        </div>

        <div className="plabel" style={{ marginTop: 16 }}>
          Превью · экспортируются все столбцы
        </div>
        <div style={{ fontSize: 11.5, color: 'var(--faint)', margin: '0 0 8px', lineHeight: 1.5 }}>
          {allColLabels.join(' · ')}
        </div>
        {preview.length > 0 && (
          <div className="prevwrap">
            <table>
              <thead>
                <tr>
                  {['Автор', 'Просмотры', 'ER', 'Лайки', 'Комменты', 'Ссылка', 'Расшифровка'].map(h => (
                    <th key={h} style={{ padding: '8px 10px' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.map(r => (
                  <tr key={r.id}>
                    <td style={{ padding: '7px 10px' }}>@{r.author_handle || '—'}</td>
                    <td style={{ padding: '7px 10px' }}>{fmtV(r.views)}</td>
                    <td style={{ padding: '7px 10px' }}>{r.er ? r.er.toFixed(1) + '×' : '—'}</td>
                    <td style={{ padding: '7px 10px' }}>{fmtV(r.likes)}</td>
                    <td style={{ padding: '7px 10px' }}>{fmtV(r.comments)}</td>
                    <td style={{ padding: '7px 10px', color: 'var(--iris)' }}>↗ ссылка</td>
                    <td style={{ padding: '7px 10px', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {(lang === 'orig' ? r.transcript_text : (r.transcript_text_ru || r.transcript_text)) || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="dlrow">
          <button className="btn" onClick={doExport}>
            ⬇ Скачать {fmt.toUpperCase()}
          </button>
          <button className="btn ghost" onClick={copyForSheets} title="Скопировать таблицу и открыть пустой Google Sheets — вставь через Ctrl+V">
            {copied ? '✓ скопировано — вставь в таблицу' : '📋 В Google Sheets'}
          </button>
          <button className="btn ghost" onClick={onClose}>Закрыть</button>
        </div>
      </div>
    </div>
  )
}
