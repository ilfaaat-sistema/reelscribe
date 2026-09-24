import { useEffect } from 'react'
import { Routes, Route, Navigate, useNavigate, useLocation } from 'react-router-dom'
import Import from './pages/Import'
import Processing from './pages/Processing'
import Results from './pages/Results'
import History from './pages/History'
import Errors from './pages/Errors'
import Radar from './pages/Radar'

// Память последней позиции в каждом разделе. Основное хранилище — sessionStorage
// (ключи rs:last:parser / rs:last:radar), резерв на случай недоступности (приватный
// режим, заблокированный доступ к хранилищу) — переменная модуля.
const SECTION_KEYS = { parser: 'rs:last:parser', radar: 'rs:last:radar' }
const SECTION_DEFAULTS = { parser: '/', radar: '/radar' }
const memFallback = {}

function saveLastPath(section, path) {
  try {
    sessionStorage.setItem(SECTION_KEYS[section], path)
  } catch {
    memFallback[section] = path
  }
}

function loadLastPath(section) {
  try {
    return sessionStorage.getItem(SECTION_KEYS[section]) || memFallback[section] || SECTION_DEFAULTS[section]
  } catch {
    return memFallback[section] || SECTION_DEFAULTS[section]
  }
}

function Header() {
  const navigate = useNavigate()
  const location = useLocation()
  const { pathname } = location
  const section = pathname.startsWith('/radar') ? 'radar' : 'parser'
  const view = pathname.startsWith('/results')
    ? 'results'
    : pathname.startsWith('/processing')
      ? 'processing'
      : pathname.startsWith('/radar')
        ? 'radar'
        : 'import'

  useEffect(() => {
    saveLastPath(section, pathname + location.search)
  }, [section, pathname, location.search])

  const goToSection = (target) => {
    if (target === section) return
    navigate(loadLastPath(target))
  }

  return (
    <header>
      <div className="logo">
        <i/><i/><i/><i/><i/>
      </div>
      <h1 style={{ cursor: 'pointer' }} onClick={() => navigate('/')}>ReelScribe</h1>
      <nav className="appswitch">
        <button
          type="button"
          className={section === 'parser' ? 'on' : ''}
          aria-current={section === 'parser' ? 'page' : undefined}
          onClick={() => goToSection('parser')}
        >
          Парсер
        </button>
        <button
          type="button"
          className={section === 'radar' ? 'on' : ''}
          aria-current={section === 'radar' ? 'page' : undefined}
          onClick={() => goToSection('radar')}
        >
          Радар
        </button>
      </nav>
      <div className="spacer" />
      {view !== 'results' && view !== 'radar' && (
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
      {section === 'parser' && (
        <>
          <button className="btn ghost sm" onClick={() => navigate('/history')}>
            История
          </button>
          <button className="btn ghost sm" onClick={() => navigate('/errors')}>
            Ошибки
          </button>
        </>
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
        <Route path="/history" element={<History />} />
        <Route path="/errors" element={<Errors />} />
        <Route path="/errors/:sessionId" element={<Errors />} />
        <Route path="/processing/:sessionId" element={<Processing />} />
        <Route path="/results" element={<Results />} />
        <Route path="/results/:sessionId" element={<Results />} />
        <Route path="/radar" element={<Radar />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  )
}
