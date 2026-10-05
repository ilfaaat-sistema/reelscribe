import { useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import Feed from './radarTabs/Feed'
import Sources from './radarTabs/Sources'
import OneOff from './radarTabs/OneOff'
import RefreshBar from './radarTabs/RefreshBar'
import './radar.css'
import './radar-scout.css'

// Оболочка раздела «Радар» (ТЗ 10): три вкладки — Лента, Источники, Разовый сбор.
// Вкладка живёт в адресе (?tab=), дефолт feed в адрес не пишется.
// Старые ссылки без tab, но с ключами разового сбора, открывают «Разовый сбор».

const TABS = [
  ['feed', 'Лента'],
  ['sources', 'Источники'],
  ['oneoff', 'Разовый сбор'],
]
const LEGACY_KEYS = ['accounts', 'period', 'metric', 'sort', 'view', 'buckets', 'reel']

export default function Radar() {
  const [searchParams, setSearchParams] = useSearchParams()
  // refreshKey — «данные в БД изменились» (Лента и Источники перезагружаются),
  // sourcesKey — «набор источников изменился» (RefreshBar перечитывает оценку)
  const [refreshKey, setRefreshKey] = useState(0)
  const [sourcesKey, setSourcesKey] = useState(0)

  const tabParam = searchParams.get('tab')
  const tab = TABS.some(([id]) => id === tabParam)
    ? tabParam
    : LEGACY_KEYS.some(k => searchParams.has(k))
      ? 'oneoff'
      : 'feed'

  const goTab = id => {
    if (id === tab) return
    setSearchParams(id === 'feed' ? {} : { tab: id })
  }

  const onRefreshDone = useCallback(() => setRefreshKey(k => k + 1), [])
  const onSourcesChanged = useCallback(() => setSourcesKey(k => k + 1), [])

  return (
    <div className="radar">
      <div className="topbar">
        <div className="logo">Reels <b>Радар</b></div>
        <div className="meta">
          <span>Аналитик рилсов конкурентов</span>
        </div>
      </div>

      <nav className="rs-tabs" role="tablist">
        {TABS.map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            className={`rs-tab${tab === id ? ' on' : ''}`}
            aria-selected={tab === id}
            onClick={() => goTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab !== 'oneoff' && <RefreshBar sourcesKey={sourcesKey} onDone={onRefreshDone} />}

      {tab === 'feed' && <Feed refreshKey={refreshKey} />}
      {tab === 'sources' && <Sources refreshKey={refreshKey} onChanged={onSourcesChanged} />}
      {tab === 'oneoff' && <OneOff />}
    </div>
  )
}
