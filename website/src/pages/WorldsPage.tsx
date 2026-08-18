import { safeSetItem } from '../utils/safeStorage'
import { useState, useEffect } from 'react'
import { Sparkles, PictureInPicture2 } from 'lucide-react'
import { useAgentSync } from '../hooks/useAgentSync'
import { usePopoutSync } from '../hooks/usePopoutSync'
import { SCENES, SCENE_STORAGE_KEY, SCENE_LAYOUT_SCALE, type SceneKey } from './scenes/config'
import { SCENE_COMPONENTS } from './scenes/components'

import { i18nT } from '../i18n/t'
export default function WorldsPage() {
  const [scene, setScene] = useState<SceneKey>(() => {
    const saved = localStorage.getItem(SCENE_STORAGE_KEY)
    return (saved as SceneKey) || 'office'
  })
  const [collapsed, setCollapsed] = useState(false)
  const { agents, maxAgents } = useAgentSync()
  const { popoutActive, broadcastScene, openPopout } = usePopoutSync(false, s => setScene(s as SceneKey))
  const activeCount = agents.filter(a => a.running).length

  useEffect(() => {
    safeSetItem(SCENE_STORAGE_KEY, scene)
  }, [scene])

  const changeScene = (key: SceneKey) => { setScene(key); broadcastScene(key) }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Collapsible header + tabs — CSS grid 0fr/1fr for smooth height transition */}
      <div
        id="worlds-collapse-panel"
        data-testid="collapse-panel"
        style={{
          display: 'grid',
          gridTemplateRows: collapsed ? '0fr' : '1fr',
          opacity: collapsed ? 0 : 1,
          transition: 'grid-template-rows 0.5s ease, opacity 0.3s ease',
          flexShrink: 0,
          position: 'relative',
          zIndex: 10,
        }}
        {...(collapsed ? { inert: '' } : {})}
      >
        <div style={{ overflow: 'hidden' }}>
        <div className="px-4 md:px-6 pt-2 pb-2">
          <div className="text-lg font-semibold text-text-strong"><Sparkles className="lucide-inline" /> {i18nT('pages.worldsPage.agent_worlds')}</div>
          <div className="text-sm text-muted">
            {i18nT('pages.worldsPage.agent', { count: agents.length })} {i18nT('pages.worldsPage.present')}{' '}
            {activeCount} {i18nT('pages.worldsPage.active')} {i18nT('pages.worldsPage.slot', { count: maxAgents - agents.length })} {i18nT('pages.worldsPage.open')}
          </div>
        </div>

        <div className="px-4 md:px-6 pb-2" style={{
          display: 'flex', gap: 6,
          flexWrap: 'wrap',
        }}>
          {SCENES.map(s => (
            <button
              key={s.key}
              onClick={() => changeScene(s.key)}
              title={s.desc}
              aria-pressed={scene === s.key}
              aria-label={`${s.label} scene: ${s.desc}`}
              style={{
                padding: '5px 12px',
                borderRadius: 6,
                border: scene === s.key ? '1.5px solid var(--accent, #f90)' : '1.5px solid var(--border, #333)',
                background: scene === s.key ? 'var(--accent, #f90)' : 'var(--bg-elevated, #1a1a2e)',
                color: scene === s.key ? '#000' : 'var(--text-muted, #999)',
                cursor: 'pointer',
                fontSize: 13,
                fontFamily: 'var(--font-body, inherit)',
                transition: 'all 0.15s ease',
                display: 'flex',
                alignItems: 'center',
                gap: 5,
              }}
            >
              <span>{s.icon}</span>
              <span>{s.label}</span>
            </button>
          ))}
        </div>
        </div>
      </div>

      {/* Scene canvas — fixed aspect wrapper so all scenes occupy the same space */}
      <div className="px-4 md:px-6 pb-5" style={{ flex: 1, display: 'flex', justifyContent: 'center', alignItems: 'start', minHeight: 0 }}>
        <div style={{ position: 'relative', width: '100%', maxWidth: 480 * SCENE_LAYOUT_SCALE, aspectRatio: '3/2' }}>
          <div style={{ position: 'absolute', top: 8, right: 8, zIndex: 20, display: 'flex', gap: 4 }}>
            <button
              onClick={() => setCollapsed(c => !c)}
              title={collapsed ? i18nT('pages.worldsPage.show_controls') : i18nT('pages.worldsPage.hide_controls')}
              aria-expanded={!collapsed}
              aria-controls="worlds-collapse-panel"
              aria-label={collapsed ? i18nT('pages.worldsPage.show_controls') : i18nT('pages.worldsPage.hide_controls')}
              className="bg-black/55 hover:bg-black/75 transition-colors"
              style={{
                border: '1px solid var(--border, #333)',
                borderRadius: 6, padding: '3px 8px', cursor: 'pointer',
                color: 'var(--text-muted, #999)', fontSize: 16, lineHeight: 1,
              }}
            >
              {collapsed ? '▼' : '▲'}
            </button>
            <button
              onClick={openPopout}
              title={i18nT('pages.worldsPage.pop_out_to_separate_window')}
              aria-label={i18nT('pages.worldsPage.pop_out_to_separate_window')}
              className="bg-black/55 hover:bg-black/75 transition-colors"
              style={{
                border: '1px solid var(--border, #333)',
                borderRadius: 6, padding: '3px 6px', cursor: 'pointer',
                color: 'var(--text-muted, #999)', lineHeight: 1, display: 'flex', alignItems: 'center',
              }}
            >
              <PictureInPicture2 size={14} />
            </button>
          </div>

          {popoutActive && (
            <div style={{
              position: 'absolute', inset: 0, zIndex: 15,
              display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
              background: 'var(--bg, #0a0a1a)', borderRadius: 8, gap: 12,
            }}>
              <PictureInPicture2 size={32} style={{ color: 'var(--accent, #f90)', opacity: 0.7 }} />
              <div style={{ color: 'var(--text-muted, #999)', fontSize: 14 }}>{i18nT('pages.worldsPage.playing_in_popout_window')}</div>
              <button
                onClick={openPopout}
                style={{
                  padding: '6px 16px', borderRadius: 6, fontSize: 13, cursor: 'pointer',
                  border: '1px solid var(--border, #333)', background: 'var(--bg-elevated, #1a1a2e)',
                  color: 'var(--text, #eee)', fontFamily: 'var(--font-body, inherit)',
                }}
              >
                {i18nT('pages.worldsPage.focus_popout')}
              </button>
            </div>
          )}

          {SCENES.map(s => {
            const C = SCENE_COMPONENTS[s.key]
            return (
              <div key={s.key} style={{ display: scene === s.key ? 'contents' : 'none' }}>
                <C agents={agents} visible={scene === s.key && !popoutActive} />
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
