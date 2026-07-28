import { useState, useMemo } from 'react'
import { useTheme, type CustomThemeData, CUSTOM_THEMES_CHANGED_EVENT } from '../hooks/useTheme'
import { Input, Btn } from './ui'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
/* ── CSS variable groups for the color picker ── */
export const VAR_GROUPS: { label: string; vars: { key: string; label: string }[] }[] = [
  {
    label: 'Backgrounds',
    vars: [
      { key: '--bg', label: 'Background' }, { key: '--bg-accent', label: 'Bg Accent' },
      { key: '--bg-elevated', label: 'Bg Elevated' }, { key: '--bg-hover', label: 'Bg Hover' },
      { key: '--card', label: 'Card' }, { key: '--card-fg', label: 'Card Text' },
      { key: '--panel', label: 'Panel' }, { key: '--panel-strong', label: 'Panel Strong' },
    ],
  },
  {
    label: 'Text & Muted',
    vars: [
      { key: '--text', label: 'Text' }, { key: '--text-strong', label: 'Text Strong' },
      { key: '--muted', label: 'Muted' }, { key: '--muted-strong', label: 'Muted Strong' },
    ],
  },
  {
    label: 'Borders',
    vars: [
      { key: '--border', label: 'Border' }, { key: '--border-strong', label: 'Border Strong' },
      { key: '--border-hover', label: 'Border Hover' },
    ],
  },
  {
    label: 'Accent',
    vars: [
      { key: '--accent', label: 'Accent' }, { key: '--accent-hover', label: 'Accent Hover' },
      { key: '--ring', label: 'Ring' },
    ],
  },
  {
    label: 'Status',
    vars: [
      { key: '--ok', label: 'OK' }, { key: '--warn', label: 'Warning' },
      { key: '--danger', label: 'Danger' }, { key: '--info', label: 'Info' },
      { key: '--aim', label: 'AIM' },
    ],
  },
]

/** Extract current CSS variable values from the active theme */
export function getCurrentThemeVars(): Record<string, string> {
  const computed = getComputedStyle(document.documentElement)
  const result: Record<string, string> = {}
  for (const group of VAR_GROUPS) {
    for (const v of group.vars) result[v.key] = computed.getPropertyValue(v.key).trim()
  }
  // Extra vars not in groups
  const extras = [
    '--card-hl', '--chrome', '--accent-subtle', '--accent-glow',
    '--ok-subtle', '--warn-subtle', '--danger-subtle', '--aim-subtle',
    '--clarify', '--clarify-subtle',
    '--diff-add', '--diff-add-text', '--diff-del', '--diff-del-text',
    '--diff-hunk', '--diff-hunk-text', '--diff-meta-text',
    '--shadow-sm', '--shadow-md', '--shadow-lg',
  ]
  for (const k of extras) result[k] = computed.getPropertyValue(k).trim()
  return result
}

export function toHex(val: string): string {
  if (!val) return '#000000'
  if (/^#[0-9a-fA-F]{6}$/.test(val)) return val
  if (/^#[0-9a-fA-F]{3}$/.test(val)) return '#' + val[1] + val[1] + val[2] + val[2] + val[3] + val[3]
  try {
    const el = document.createElement('div')
    el.style.color = val
    document.body.appendChild(el)
    try {
      const c = getComputedStyle(el).color
      const m = c.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/)
      if (m) return `#${[m[1], m[2], m[3]].map(n => parseInt(n).toString(16).padStart(2, '0')).join('')}`
    } finally { document.body.removeChild(el) }
  } catch { /* ignore */ }
  return '#000000'
}

export type CreatorMode = 'picker' | 'json'

/** Shared theme editor state and actions */
export function useThemeEditor() {
  const { addCustomTheme, deleteCustomTheme, loadCustomThemes } = useTheme()

  const [editorOpen, setEditorOpen] = useState(false)
  const [editingSlug, setEditingSlug] = useState<string | null>(null)
  const [creatorMode, setCreatorMode] = useState<CreatorMode>('picker')
  const [themeName, setThemeName] = useState('')
  const [themeEmoji, setThemeEmoji] = useState('✨')
  const [darkVars, setDarkVars] = useState<Record<string, string>>({})
  const [lightVars, setLightVars] = useState<Record<string, string>>({})
  const [jsonText, setJsonText] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const isEditing = editingSlug !== null

  const openNewTheme = () => {
    const current = getCurrentThemeVars()
    setDarkVars({ ...current }); setLightVars({ ...current })
    setThemeName(''); setThemeEmoji('✨'); setJsonText(''); setError('')
    setEditingSlug(null); setEditorOpen(true); setCreatorMode('picker')
  }

  const openEditTheme = async (slug: string) => {
    setError('')
    try {
      const data = await api.themeDetail(slug)
      setThemeName(data.name || ''); setThemeEmoji(data.emoji || '🎨')
      setDarkVars(data.dark || {}); setLightVars(data.light || {})
      setJsonText(JSON.stringify(data, null, 2))
      setEditingSlug(slug); setEditorOpen(true); setCreatorMode('picker')
    } catch {
      setError('Failed to load theme for editing')
      setEditorOpen(true)
    }
  }

  const closeEditor = () => { setEditorOpen(false); setEditingSlug(null); setError('') }

  const saveTheme = async () => {
    setError(''); setSaving(true)
    try {
      if (creatorMode === 'json') {
        let parsed: CustomThemeData
        try { parsed = JSON.parse(jsonText) } catch { throw new Error('Invalid JSON — check syntax and try again.') }
        if (!parsed.name) throw new Error('JSON must include a "name" field.')
        if (!parsed.dark || !parsed.light) throw new Error('JSON must include "dark" and "light" objects.')
        if (isEditing) {
          await api.updateTheme(editingSlug!, { name: parsed.name, emoji: parsed.emoji || '🎨', dark: parsed.dark, light: parsed.light })
          await loadCustomThemes(); window.dispatchEvent(new Event(CUSTOM_THEMES_CHANGED_EVENT))
        } else {
          await addCustomTheme({ name: parsed.name, emoji: parsed.emoji || '✨', dark: parsed.dark, light: parsed.light })
        }
      } else {
        if (!themeName.trim()) throw new Error('Theme name is required.')
        const payload = { name: themeName.trim(), emoji: themeEmoji.trim() || '✨', dark: darkVars, light: lightVars }
        if (isEditing) {
          await api.updateTheme(editingSlug!, payload)
          await loadCustomThemes(); window.dispatchEvent(new Event(CUSTOM_THEMES_CHANGED_EVENT))
        } else {
          await addCustomTheme(payload)
        }
      }
      closeEditor()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to save theme')
    } finally { setSaving(false) }
  }

  const handleDelete = async (slug?: string) => {
    const target = slug || editingSlug
    if (!target) return
    if (confirm(i18nT('components.themeEditor.delete_this_custom_theme'))) {
      try { await deleteCustomTheme(target); closeEditor() }
      catch (e: unknown) { setError(e instanceof Error ? e.message : 'Failed to delete theme') }
    }
  }

  const syncJsonToPicker = (text: string) => {
    try {
      const p = JSON.parse(text)
      if (p.name !== undefined) setThemeName(p.name)
      if (p.emoji !== undefined) setThemeEmoji(p.emoji)
      if (p.dark) setDarkVars(p.dark)
      if (p.light) setLightVars(p.light)
    } catch { /* invalid JSON */ }
  }

  const pickerToJson = useMemo(() => {
    if (!themeName && !Object.keys(darkVars).length) return ''
    return JSON.stringify({ name: themeName || 'My Theme', emoji: themeEmoji || '✨', dark: darkVars, light: lightVars }, null, 2)
  }, [themeName, themeEmoji, darkVars, lightVars])

  const updateDarkVar = (key: string, val: string) => setDarkVars(prev => ({ ...prev, [key]: val }))
  const updateLightVar = (key: string, val: string) => setLightVars(prev => ({ ...prev, [key]: val }))

  return {
    editorOpen, isEditing, editingSlug, creatorMode, setCreatorMode,
    themeName, setThemeName, themeEmoji, setThemeEmoji,
    darkVars, lightVars, updateDarkVar, updateLightVar,
    jsonText, setJsonText, saving, error,
    openNewTheme, openEditTheme, closeEditor, saveTheme, handleDelete,
    syncJsonToPicker, pickerToJson,
  }
}

/* ── Color row input ── */
export function ColorRow({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  const isSimpleColor = /^#[0-9a-fA-F]{3,8}$/.test(value) || /^[a-z]+$/i.test(value)
  return (
    <div className="flex items-center gap-2 py-1">
      <span className="text-[13px] text-muted w-28 shrink-0 truncate" title={label}>{label}</span>
      {isSimpleColor ? (
        <input type="color" aria-label={`${label} color picker`} value={toHex(value)} onChange={e => onChange(e.target.value)}
          className="w-8 h-7 rounded border border-border cursor-pointer bg-transparent shrink-0" />
      ) : (
        <div className="w-8 h-7 rounded border border-border shrink-0" style={{ background: value }} />
      )}
      <input type="text" aria-label={label} value={value} onChange={e => onChange(e.target.value)}
        className="flex-1 min-w-0 bg-bg-elevated border border-border rounded px-2 py-1 text-[13px] text-text font-mono outline-none focus-ring"
        spellCheck={false} />
    </div>
  )
}

/* ── Collapsible color group editor ── */
export function ColorModeEditor({ label, vars, onChange }: {
  label: string; vars: Record<string, string>; onChange: (key: string, val: string) => void
}) {
  const [expanded, setExpanded] = useState<string | null>(null)
  return (
    <div>
      <div className="text-[13px] font-medium text-text-strong mb-2">{label}</div>
      <div className="space-y-1">
        {VAR_GROUPS.map(group => (
          <div key={group.label} className="border border-border rounded-md overflow-hidden">
            <button
              className="w-full flex items-center justify-between px-3 py-1.5 text-[13px] text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent border-none"
              onClick={() => setExpanded(expanded === group.label ? null : group.label)}
            >
              <span>{group.label}</span>
              <span className="flex items-center gap-1">
                {group.vars.slice(0, 5).map(v => (
                  <span key={v.key} className="w-3 h-3 rounded-sm border border-border"
                    style={{ background: vars[v.key] || '#000' }} title={`${v.label}: ${vars[v.key] || '?'}`} />
                ))}
                <span className="ml-1 text-[11px]">{expanded === group.label ? '▲' : '▼'}</span>
              </span>
            </button>
            {expanded === group.label && (
              <div className="px-3 pb-2 border-t border-border bg-bg-elevated/50">
                {group.vars.map(v => (
                  <ColorRow key={v.key} label={v.label} value={vars[v.key] || ''} onChange={val => onChange(v.key, val)} />
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

/* ── Theme editor panel (used by both DisplayTab and DisplayPanel) ── */
export function ThemeEditorPanel({ editor }: { editor: ReturnType<typeof useThemeEditor> }) {
  const {
    creatorMode, setCreatorMode, isEditing, editingSlug,
    themeName, setThemeName, themeEmoji, setThemeEmoji,
    darkVars, lightVars, updateDarkVar, updateLightVar,
    jsonText, setJsonText, pickerToJson, syncJsonToPicker,
    error, saving, saveTheme, closeEditor, handleDelete,
  } = editor

  return (
    <div className="animate-rise">
      <div className="flex items-center gap-2 mb-3">
        <button
          className={`px-3 py-1 rounded-md text-[13px] transition-colors cursor-pointer border-none ${creatorMode === 'picker' ? 'bg-accent-subtle text-accent' : 'text-muted hover:text-text bg-transparent'}`}
          onClick={() => { if (creatorMode === 'json') syncJsonToPicker(jsonText); setCreatorMode('picker') }}
        >{i18nT('components.themeEditor.color_picker')}</button>
        <button
          className={`px-3 py-1 rounded-md text-[13px] transition-colors cursor-pointer border-none ${creatorMode === 'json' ? 'bg-accent-subtle text-accent' : 'text-muted hover:text-text bg-transparent'}`}
          onClick={() => { setCreatorMode('json'); setJsonText(pickerToJson) }}
        >{i18nT('components.themeEditor.paste_json')}</button>
        {isEditing && <span className="ml-auto text-[12px] text-muted">{i18nT('components.themeEditor.editing')} {themeName || editingSlug}</span>}
      </div>

      {error && <div className="mb-3 bg-danger/10 border border-danger/20 rounded-lg p-2.5 text-[13px] text-danger animate-rise">{error}</div>}

      {creatorMode === 'picker' ? (
        <div>
          <div className="flex gap-2 mb-3">
            <div className="flex-1">
              {/* Control is the custom <Input> (a forwardRef <input>) nested here
                  and linked via htmlFor+id; the deprecated label-has-for rule can't
                  see through the component wrapper, so scope-disable it. */}
              {/* eslint-disable-next-line jsx-a11y/label-has-for */}
              <label htmlFor="theme-editor-name">
                <span className="text-[12px] text-muted uppercase tracking-[.04em] mb-1 block">{i18nT('components.themeEditor.theme_name')}</span>
                <Input id="theme-editor-name" value={themeName} onChange={e => setThemeName(e.target.value)} placeholder={i18nT('components.themeEditor.my_custom_theme')} />
              </label>
            </div>
            <div className="w-16 shrink-0">
              {/* eslint-disable-next-line jsx-a11y/label-has-for */}
              <label htmlFor="theme-editor-emoji">
                <span className="text-[12px] text-muted uppercase tracking-[.04em] mb-1 block">{i18nT('components.themeEditor.emoji')}</span>
                <Input id="theme-editor-emoji" value={themeEmoji} onChange={e => setThemeEmoji(e.target.value)} placeholder="✨" className="text-center !flex-none w-full" />
              </label>
            </div>
          </div>
          <ColorModeEditor label={i18nT('components.themeEditor.dark_mode_colors')} vars={darkVars} onChange={updateDarkVar} />
          <div className="mt-3">
            <ColorModeEditor label={i18nT('components.themeEditor.light_mode_colors')} vars={lightVars} onChange={updateLightVar} />
          </div>
        </div>
      ) : (
        <div>
          <label htmlFor="theme-editor-json">
            <span className="text-[12px] text-muted uppercase tracking-[.04em] mb-1 block">{i18nT('components.themeEditor.theme_json')}</span>
            <textarea
              id="theme-editor-json"
              aria-label={i18nT('components.themeEditor.theme_json')}
              value={jsonText} onChange={e => setJsonText(e.target.value)}
              onBlur={() => syncJsonToPicker(jsonText)}
              placeholder={i18nT('components.themeEditor.name_my_theme_emoji_dark_bg_12141a_light_bg_fafa')}
              className="w-full h-56 bg-bg-elevated border border-border rounded-md px-3 py-2 text-[13px] text-text font-mono outline-none resize-y focus-ring"
              spellCheck={false}
            />
          </label>
        </div>
      )}

      <div className="flex items-center gap-2 mt-3">
        <Btn onClick={saveTheme} className="bg-accent text-accent-fg hover:bg-accent-hover" disabled={saving}>
          {saving ? 'Saving…' : isEditing ? 'Update Theme' : 'Save Theme'}
        </Btn>
        <Btn onClick={closeEditor}>{i18nT('components.themeEditor.cancel')}</Btn>
        {isEditing && (
          <button onClick={() => handleDelete()}
            className="ml-auto px-3 py-1.5 rounded-md text-[13px] font-medium cursor-pointer bg-danger/15 border border-danger/40 text-danger hover:bg-danger/25 hover:border-danger/60 transition-all">
            {i18nT('components.themeEditor.delete_theme')}
          </button>
        )}
      </div>
    </div>
  )
}
