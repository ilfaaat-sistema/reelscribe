import { Routes, Route, Navigate, useNavigate, useLocation } from 'react-router-dom'
import Import from './pages/Import'
import Processing from './pages/Processing'
import Results from './pages/Results'

function Header() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const view = pathname.startsWith('/results')
    ? 'results'
    : pathname.startsWith('/processing')
      ? 'processing'
      : 'import'

  return (
    <header>
      <div className="logo">
        <i/><i/><i/><i/><i/>
      </div>
      <h1 style={{ cursor: 'pointer' }} onClick={() => navigate('/')}>ReelScribe</h1>
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
        <button className="btn ghost sm" onClick={() => navigate('/')}>
          ← Новый импорт
        </button>
      )}
    </header>
  )
}

export default function App() {
  return (
    <div className="app">
      <Header />
      <Routes>
        <Route path="/" element={<Import />} />
        <Route path="/processing/:sessionId" element={<Processing />} />
        <Route path="/results" element={<Results />} />
        <Route path="/results/:sessionId" element={<Results />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  )
}
