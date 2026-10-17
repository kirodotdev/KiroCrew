import { safeSetItem } from '../utils/safeStorage'
import { useState, useEffect } from 'react'
import { useAgentSync } from '../hooks/useAgentSync'
import { usePopoutSync } from '../hooks/usePopoutSync'
import { SCENES, SCENE_STORAGE_KEY, SCENE_LAYOUT_SCALE, type SceneKey } from './scenes/config'
import { SCENE_COMPONENTS } from './scenes/components'
import { useAppDispatch } from '../store'
import { fetchSlots } from '../store/dashboardSlice'

import { i18nT } from '../i18n/t'

export default function WorldsPopout() {
  const [scene, setScene] = useState<SceneKey>(() => (localStorage.getItem(SCENE_STORAGE_KEY) as SceneKey) || 'office')
  const [collapsed, setCollapsed] = useState(false)
  // The toggle renders only a ▼/▲ glyph, so it needs an explicit accessible
  // name: `title` alone is an unreliable accessible-name source across screen
  // readers. Shared with `title` so the two can never drift apart.
  const collapseLabel = collapsed
    ? i18nT('pages.worldsPopout.show_controls')
    : i18nT('pages.worldsPopout.hide_controls')
  const dispatch = useAppDispatch()
  const { agents } = useAgentSync()
  const { broadcastScene } = usePopoutSync(true, s => setScene(s as SceneKey))

  useEffect(() => {
    dispatch(fetchSlots())
    const iv = setInterval(() => dispatch(fetchSlots()), 5000)
    return () => clearInterval(iv)
  }, [dispatch])

  useEffect(() => { safeSetItem(SCENE_STORAGE_KEY, scene) }, [scene])

  const changeScene = (key: SceneKey) => { setScene(key); broadcastScene(key) }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', background: 'var(--bg, #0a0a1a)' }}>
      <div
        id="popout-collapse-panel"
        style={{
          display: 'grid',
          gridTemplateRows: collapsed ? '0fr' : '1fr',
          opacity: collapsed ? 0 : 1,
          transition: 'grid-template-rows 0.5s ease, opacity 0.3s ease',
          flexShrink: 0,
        }}
        {...(collapsed ? { inert: '' } : {})}
      >
        <div style={{ overflow: 'hidden' }}>
          <div style={{ display: 'flex', gap: 4, padding: '8px 12px', flexWrap: 'wrap', alignItems: 'center' }}>
            {SCENES.map(s => (
              <button
                key={s.key}
                onClick={() => changeScene(s.key)}
                aria-pressed={scene === s.key}
                style={{
                  padding: '4px 10px', borderRadius: 6, fontSize: 12,
                  border: scene === s.key ? '1.5px solid var(--accent, #f90)' : '1.5px solid var(--border, #333)',
                  background: scene === s.key ? 'var(--accent, #f90)' : 'var(--bg-elevated, #1a1a2e)',
                  color: scene === s.key ? '#000' : 'var(--text-muted, #999)',
                  cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
                  fontFamily: 'var(--font-body, inherit)', transition: 'all 0.15s ease',
                }}
              >
                <span>{s.icon}</span><span>{s.label}</span>
              </button>
            ))}
          </div>
        </div>
      </div>

      <div style={{ flex: 1, display: 'flex', justifyContent: 'center', alignItems: 'start', padding: '0 12px 12px', minHeight: 0 }}>
        <div style={{ position: 'relative', width: '100%', maxWidth: 480 * SCENE_LAYOUT_SCALE, aspectRatio: '3/2' }}>
          <button
            onClick={() => setCollapsed(c => !c)}
            title={collapseLabel}
            aria-label={collapseLabel}
            aria-expanded={!collapsed}
            aria-controls="popout-collapse-panel"
            className="bg-black/55 hover:bg-black/75 transition-colors"
            style={{
              position: 'absolute', top: 8, right: 8, zIndex: 20,
              border: '1px solid var(--border, #333)', borderRadius: 6,
              padding: '3px 8px', cursor: 'pointer',
              color: 'var(--text-muted, #999)', fontSize: 16, lineHeight: 1,
            }}
          >
            {collapsed ? '▼' : '▲'}
          </button>

          {SCENES.map(s => {
            const C = SCENE_COMPONENTS[s.key]
            return (
              <div key={s.key} style={{ display: scene === s.key ? 'contents' : 'none' }}>
                <C agents={agents} visible={scene === s.key} />
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
