import { useState } from 'react'
import { exportUrl } from '../api/client'
import { fmtV } from '../lib/utils'

const FMTS = [
  { v: 'csv', label: 'CSV', icon: '📊', desc: 'Excel, Google Sheets, любой табличный редактор', ext: '.csv' },
  { v: 'xlsx', label: 'XLSX', icon: '📗', desc: 'Excel с форматированием, заголовки на русском', ext: '.xlsx' },
  { v: 'json', label: 'JSON', icon: '{ }', desc: 'Для разработчиков, API, Make/n8n интеграции', ext: '.json' },
]

export default function ExportModal({ reels, selected, onClose }) {
  const [fmt, setFmt] = useState('csv')
  const [lang, setLang] = useState('ru')
  const [scope, setScope] = useState('all')

  const count = scope === 'selected' ? selected.length : reels.length
  const preview = (scope === 'selected'
    ? reels.filter(r => selected.includes(r.id))
    : reels
  ).slice(0, 5)

  function doExport() {
    if (scope === 'selected' && selected.length) {
      const url = new URL(exportUrl({ format: fmt, lang, scope }), window.location.href)
      selected.forEach(id => url.searchParams.append('ids', id))
      window.open(url.toString(), '_blank')
      return
    }
    window.open(exportUrl({ format: fmt, lang }), '_blank')
  }

  return (
    <div className="modal-back" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="sheet">
        <h3>📦 Экспорт</h3>
        <p style={{color:'var(--mut)',fontSize:13,margin:'4px 0 0'}}>
          {count} рилс · выберите формат и язык расшифровки
        </p>

        <div className="fmts">
          {FMTS.map(f => (
            <button key={f.v} className={`fmt ${fmt === f.v ? 'on' : ''}`} onClick={() => setFmt(f.v)}>
              <div className="fh">
                <span style={{fontSize:18}}>{f.icon}</span>
                <span>{f.label}</span>
                <span className="ext">{f.ext}</span>
              </div>
              <div className="fd">{f.desc}</div>
            </button>
          ))}
        </div>

        <div className="scope">
          <span>Язык расшифровки:</span>
          {[['ru','Русский'],['orig','Оригинал'],['both','Оба']].map(([v,l]) => (
            <button
              key={v}
              className={`chip ${lang === v ? 'on' : ''}`}
              style={{padding:'4px 10px'}}
              onClick={() => setLang(v)}
            >{l}</button>
          ))}
        </div>

        <div className="scope" style={{marginTop:8}}>
          <span>Объём:</span>
          <button
            className={`chip ${scope === 'all' ? 'on' : ''}`}
            style={{padding:'4px 10px'}}
            onClick={() => setScope('all')}
          >Все ({reels.length})</button>
          {selected.length > 0 && (
            <button
              className={`chip ${scope === 'selected' ? 'on' : ''}`}
              style={{padding:'4px 10px'}}
              onClick={() => setScope('selected')}
            >Выбранные ({selected.length})</button>
          )}
        </div>

        {preview.length > 0 && (
          <>
            <div className="plabel" style={{marginTop:16}}>Превью</div>
            <div className="prevwrap">
              <table>
                <thead>
                  <tr>
                    <th style={{padding:'8px 10px'}}>Автор</th>
                    <th style={{padding:'8px 10px'}}>Просмотры</th>
                    <th style={{padding:'8px 10px'}}>ER</th>
                    <th style={{padding:'8px 10px',maxWidth:200}}>Расшифровка</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.map(r => (
                    <tr key={r.id}>
                      <td style={{padding:'7px 10px'}}>@{r.author_handle || '—'}</td>
                      <td style={{padding:'7px 10px'}}>{fmtV(r.views)}</td>
                      <td style={{padding:'7px 10px'}}>{r.er ? r.er.toFixed(1) + '×' : '—'}</td>
                      <td style={{padding:'7px 10px',maxWidth:200,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                        {(lang === 'ru' ? (r.transcript_text_ru || r.transcript_text) : r.transcript_text) || '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        <div className="dlrow">
          <button className="btn" onClick={doExport}>
            ⬇ Скачать {fmt.toUpperCase()}
          </button>
          <button className="btn ghost" onClick={onClose}>Закрыть</button>
        </div>
      </div>
    </div>
  )
}
