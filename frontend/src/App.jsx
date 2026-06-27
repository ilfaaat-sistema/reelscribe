import { useState } from 'react'
import Import from './pages/Import'
import Processing from './pages/Processing'
import Results from './pages/Results'

export default function App() {
  const [view, setView] = useState('import')
  const [sessionId, setSessionId] = useState(null)
  const [total, setTotal] = useState(0)

  function goProcess(sid, tot) {
    setSessionId(sid)
    setTotal(tot)
    setView('processing')
  }

  return (
    <div className="app">
      <header>
        <div className="logo">
          <i/><i/><i/><i/><i/>
        </div>
        <h1>ReelScribe</h1>
        <div className="spacer" />
        {view !== 'results' && (
          <div className="stepper">
            <div className={`step ${view === 'import' ? 'on' : 'done'}`}>
              <b>{view === 'import' ? '1' : '✓'}</b>Импорт
            </div>
            <div className={`step ${view === 'processing' ? 'on' : view === 'results' ? 'done' : ''}`}>
              <b>{view === 'results' ? '✓' : '2'}</b>Обработка
            </div>
            <div className={`step ${view === 'results' ? 'on' : ''}`}>
              <b>3</b>Результаты
            </div>
          </div>
        )}
        {view === 'results' && (
          <button className="btn ghost sm" onClick={() => { setSessionId(null); setView('import') }}>
            ← Новый импорт
          </button>
        )}
      </header>

      {view === 'import' && (
        <Import onDone={goProcess} />
      )}
      {view === 'processing' && (
        <Processing sessionId={sessionId} total={total} onDone={() => setView('results')} />
      )}
      {view === 'results' && (
        <Results sessionId={sessionId} />
      )}
    </div>
  )
}
