import { useState, useMemo } from 'react'
import { useTheme, type CustomThemeData, CUSTOM_THEMES_CHANGED_EVENT } from '../hooks/useTheme'
import { Input, Btn } from './ui'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
/* ── CSS variable groups for the color picker ── */

/**
 * Catalog KEY for each group heading and each CSS-variable row label.
 *
 * Keys, not strings: `VAR_GROUPS` below is evaluated at module load, so an
 * `i18nT()` call there would freeze whatever language was active at boot and
 * never re-resolve on a language switch. Both lookups happen in
 * `ColorModeEditor`, which runs during render.
 *
 * Shaped as flat `Record`s of full literal keys and indexed inline at the
 * `i18nT()` call, because that is the form `scripts/check-i18n-keys.mjs` can
 * resolve statically — a key it cannot resolve is a key it cannot verify exists.
 *
 * The CSS custom-property names themselves (`--bg`, `--accent-hover`) are NOT in
 * here: they are the identifiers the theme JSON, the stylesheet and
 * `ALLOWED_CSS_VARS` in `hooks/themeCss.ts` agree on, so they are data. Only
 * the human labels beside them are copy.
 */
const VAR_GROUP_LABEL_KEY: Record<string, string> = {
  backgrounds: 'components.themeEditor.group_backgrounds',
  text: 'components.themeEditor.group_text_muted',
  borders: 'components.themeEditor.group_borders',
  accent: 'components.themeEditor.group_accent',
  status: 'components.themeEditor.group_status',
}

const VAR_LABEL_KEY: Record<string, string> = {
  '--bg': 'components.themeEditor.var_bg',
  '--bg-accent': 'components.themeEditor.var_bg_accent',
  '--bg-elevated': 'components.themeEditor.var_bg_elevated',
  '--bg-hover': 'components.themeEditor.var_bg_hover',
  '--card': 'components.themeEditor.var_card',
  '--card-fg': 'components.themeEditor.var_card_text',
  '--panel': 'components.themeEditor.var_panel',
  '--panel-strong': 'components.themeEditor.var_panel_strong',
  '--text': 'components.themeEditor.var_text',
  '--text-strong': 'components.themeEditor.var_text_strong',
  '--muted': 'components.themeEditor.var_muted',
  '--muted-strong': 'components.themeEditor.var_muted_strong',
  '--border': 'components.themeEditor.var_border',
  '--border-strong': 'components.themeEditor.var_border_strong',
  '--border-hover': 'components.themeEditor.var_border_hover',
  '--accent': 'components.themeEditor.var_accent',
  '--accent-hover': 'components.themeEditor.var_accent_hover',
  '--ring': 'components.themeEditor.var_ring',
  '--ok': 'components.themeEditor.var_ok',
  '--warn': 'components.themeEditor.var_warning',
  '--danger': 'components.themeEditor.var_danger',
  '--info': 'components.themeEditor.var_info',
  '--aim': 'components.themeEditor.var_aim',
}

/**
 * Picker layout: group id plus the CSS variables that group edits, in display
 * order.
 *
 * `id` is a stable slug rather than the English heading because the heading is
 * also the accordion's expanded-state identity and its React key — keying either
 * on display copy would silently change both under a language switch.
 */
export const VAR_GROUPS: { id: string; vars: string[] }[] = [
  {
    id: 'backgrounds',
    vars: [
      '--bg', '--bg-accent', '--bg-elevated', '--bg-hover',
      '--card', '--card-fg', '--panel', '--panel-strong',
    ],
  },
  { id: 'text', vars: ['--text', '--text-strong', '--muted', '--muted-strong'] },
  { id: 'borders', vars: ['--border', '--border-strong', '--border-hover'] },
  { id: 'accent', vars: ['--accent', '--accent-hover', '--ring'] },
  { id: 'status', vars: ['--ok', '--warn', '--danger', '--info', '--aim'] },
]

/** Extract current CSS variable values from the active theme */
export function getCurrentThemeVars(): Record<string, string> {
  const computed = getComputedStyle(document.documentElement)
  const result: Record<string, string> = {}
  for (const group of VAR_GROUPS) {
    for (const key of group.vars) result[key] = computed.getPropertyValue(key).trim()
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
      setError(i18nT('components.themeEditor.failed_to_load_theme_for_editing'))
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
      setError(e instanceof Error ? e.message : i18nT('components.themeEditor.failed_to_save_theme'))
    } finally { setSaving(false) }
  }

  const handleDelete = async (slug?: string) => {
    const target = slug || editingSlug
    if (!target) return
    if (confirm(i18nT('components.themeEditor.delete_this_custom_theme'))) {
      try { await deleteCustomTheme(target); closeEditor() }
      catch (e: unknown) { setError(e instanceof Error ? e.message : i18nT('components.themeEditor.failed_to_delete_theme')) }
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
        <input type="color" aria-label={i18nT('components.themeEditor.color_picker_for_var', { label })} value={toHex(value)} onChange={e => onChange(e.target.value)}
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
          <div key={group.id} className="border border-border rounded-md overflow-hidden">
            <button
              className="w-full flex items-center justify-between px-3 py-1.5 text-[13px] text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer bg-transparent border-none"
              onClick={() => setExpanded(expanded === group.id ? null : group.id)}
            >
              <span>{i18nT(VAR_GROUP_LABEL_KEY[group.id])}</span>
              <span className="flex items-center gap-1">
                {group.vars.slice(0, 5).map(key => (
                  <span key={key} className="w-3 h-3 rounded-sm border border-border"
                    style={{ background: vars[key] || '#000' }} title={`${i18nT(VAR_LABEL_KEY[key])}: ${vars[key] || '?'}`} />
                ))}
                <span className="ml-1 text-[11px]">{expanded === group.id ? '▲' : '▼'}</span>
              </span>
            </button>
            {expanded === group.id && (
              <div className="px-3 pb-2 border-t border-border bg-bg-elevated/50">
                {group.vars.map(key => (
                  <ColorRow key={key} label={i18nT(VAR_LABEL_KEY[key])} value={vars[key] || ''} onChange={val => onChange(key, val)} />
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

      {/* No hand-off: the notice sits beside unsaved form input, and the button
          navigates away — which would discard what the user typed. */}
      <ErrorNotice message={error} className="mb-3 animate-rise" />

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
          {saving ? i18nT('components.themeEditor.saving') : isEditing ? i18nT('components.themeEditor.update_theme') : i18nT('components.themeEditor.save_theme')}
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
