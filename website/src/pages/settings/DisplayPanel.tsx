import { X, Loader2 } from 'lucide-react'
import { useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { useZoomCtx } from '../../hooks/ZoomProvider'
import { useTheme } from '../../hooks/useTheme'
import type { ColorTheme } from '../../hooks/useTheme'
import { useUIMode } from '../../hooks/useUIMode'
import { SettingsSection, SettingsCard, SettingsSelect, SettingsStepper, SettingsButtonGroup, SettingsInput } from '../../components/settings'
import SimpleSelect from '../../components/SimpleSelect'
import { Input } from '../../components/ui'
import { useThemeEditor, ThemeEditorPanel } from '../../components/themeEditor'
import Clickable from '../../components/Clickable'
import { useAppSelector, useAppDispatch } from '../../store'
import { setSessionDefaultColor, setSessionColorsMode, setSessionColorsPalette, setSessionColorsIntensity } from '../../store/dashboardSlice'
import { useSessionPalette } from '../../hooks/useSessionPalette'
import { PALETTE_NAMES, INTENSITY_NAMES } from '../../utils/sessionColors'
import type { DefaultColorSetting, PaletteName, IntensityName, SessionColorMode } from '../../utils/sessionColors'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { clampTintCount, RECENT_TINT_COUNT } from '../../utils/recencyTint'
import { useLanguage } from '../../i18n/LanguageProvider'
import { AUTO_LANGUAGE, PICKABLE_LANGUAGES, languageLabel } from '../../i18n/languages'
import {
  useTerminalFont,
  setTerminalFontFamily,
  setTerminalFontSize,
  DEFAULT_TERMINAL_FONT_SIZE,
} from '../../hooks/useTerminalFont'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
/**
 * Lightweight inline spinner (no modal / progress bar — matches the "status,
 * not ceremony" preference). Colors come from theme CSS vars via Tailwind
 * (`text-muted`), never hardcoded. Under prefers-reduced-motion the rotating
 * glyph is replaced by a static "…" so nothing animates.
 */
function StatusSpinner() {
  const reduce = useReducedMotion()
  if (reduce) {
    return <span className="text-[13px] leading-none text-muted" aria-hidden="true">…</span>
  }
  return (
    <motion.span
      className="inline-flex text-muted"
      aria-hidden="true"
      animate={{ rotate: 360 }}
      transition={{ repeat: Infinity, ease: 'linear', duration: 0.8 }}
    >
      <Loader2 className="w-3.5 h-3.5" />
    </motion.span>
  )
}

/** Spinner + label pair with a polite live region for screen readers. */
function StatusIndicator({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-muted" role="status" aria-live="polite">
      <StatusSpinner />
      {label}
    </span>
  )
}

export function DisplayPanel() {
  const { language, detected: detectedLanguage, setLanguage, syncFailed: langSyncFailed } = useLanguage()
  const { zoom, zoomSupported, zoomIn, zoomOut, reset, family, setFontFamily } = useZoomCtx()
  // Shortcut label for the zoom hint/description: ⌘ on macOS, Ctrl elsewhere.
  const modKey = /mac/i.test(navigator.platform) ? '⌘' : 'Ctrl'
  const { preference, setTheme, colorTheme, setColorTheme, allThemes, loadCustomThemes, themeSwitching } = useTheme()
  const { uiMode, setUIMode } = useUIMode()
  const editor = useThemeEditor()
  const termFont = useTerminalFont()

  const dispatch = useAppDispatch()
  const { paletteColors: colors, colorMode, paletteName, intensity, boost } = useSessionPalette()
  const defaultColor = useAppSelector(s => s.dashboard.sessionDefaultColor) as DefaultColorSetting

  // Recency-tint count is persisted server-side (dashboard.recent_tint_count) via the shared
  // kirocrewConfig query, so the choice follows the user across browsers/restarts. Optimistic
  // cache write makes the sidebar tint (which reads the same query) re-rank instantly.
  const qc = useQueryClient()
  const mcQ = useQuery<{ dashboard?: { recent_tint_count?: number } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const recentTintCount = clampTintCount(mcQ.data?.dashboard?.recent_tint_count)
  const tintMut = useMutation({
    mutationFn: (value: number) => api.patchConfig('dashboard.recent_tint_count', value),
    onMutate: async (value: number) => {
      await qc.cancelQueries({ queryKey: ['kirocrewConfig'] })
      const prev = qc.getQueryData<{ dashboard?: { recent_tint_count?: number } }>(['kirocrewConfig'])
      const next = structuredClone(prev ?? {})
      next.dashboard = { ...(next.dashboard ?? {}), recent_tint_count: value }
      qc.setQueryData(['kirocrewConfig'], next)
      return { prev }
    },
    onError: (_e, _v, ctx) => { if (ctx?.prev) qc.setQueryData(['kirocrewConfig'], ctx.prev) },
    onSettled: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })
  const setTintCount = (n: number) => tintMut.mutate(clampTintCount(n))

  // ── Install theme (Level 0) from a local folder or a GitHub repo ──
  const [installType, setInstallType] = useState<'github' | 'local'>('github')
  const [installValue, setInstallValue] = useState('')
  const [installBusy, setInstallBusy] = useState(false)
  const [installError, setInstallError] = useState<string | null>(null)
  // Phase for the install status indicator: fetching (api.installTheme in
  // flight) → applying (auto-selecting the freshly installed theme).
  const [installPhase, setInstallPhase] = useState<'fetching' | 'applying' | null>(null)

  const handleInstall = async () => {
    const v = installValue.trim()
    if (!v || installBusy) return
    setInstallBusy(true)
    setInstallError(null)
    setInstallPhase('fetching')
    try {
      const source =
        installType === 'github'
          ? ({ type: 'github', url: v } as const)
          : ({ type: 'local', path: v } as const)
      const res = await api.installTheme(source)
      if (!res?.ok) {
        setInstallError(res?.error || i18nT('pages.settings.displayPanel.install_failed'))
        return
      }
      setInstallPhase('applying')
      await loadCustomThemes()
      if (res.slug) setColorTheme(`custom-${res.slug}` as ColorTheme)
      setInstallValue('')
    } catch (e) {
      setInstallError(e instanceof Error ? e.message : i18nT('pages.settings.displayPanel.install_failed'))
    } finally {
      setInstallBusy(false)
      setInstallPhase(null)
    }
  }

  return (
    <>
      <SettingsSection title={i18nT('pages.settings.displayPanel.view')}>
        <SettingsCard>
          {/* Options are built from SUPPORTED_LANGUAGES, so shipping a new
              language needs no change here. The Auto entry names what the host's
              own preferences resolve to ("Auto — 简体中文"), so the user can see
              what picking Auto gets them. The suffix comes from `detected`, not
              the active language, so it shows what the host asks for instead of
              echoing the current selection.

              The label is plain "Auto", NOT "Auto (follow browser)": in the
              desktop app there is no browser preference to follow — the locale
              comes from the OS — so the resolved language after the em dash is
              what answers the question, on every surface. */}
          <SettingsSelect
            label={i18nT('settings.display.language.label')}
            description={i18nT('settings.display.language.description')}
            value={language}
            options={[AUTO_LANGUAGE, ...PICKABLE_LANGUAGES.map(l => l.code)]}
            optionLabels={[
              `${i18nT('settings.display.language.auto')} — ${languageLabel(detectedLanguage)}`,
              ...PICKABLE_LANGUAGES.map(l => l.label),
            ]}
            onChange={setLanguage}
          />
          {/* A failed write means the choice is browser-local only, and the next
              load will silently revert it to the server's value. Say so rather
              than letting the user discover it on reload. */}
          {langSyncFailed && (
            <span className="text-[12px] text-danger" role="status" aria-live="polite">
              {i18nT('settings.display.language.sync_failed')}
            </span>
          )}
          <SettingsButtonGroup label={i18nT('pages.settings.displayPanel.interface')} description={i18nT('pages.settings.displayPanel.chat_bubbles_or_cli_style_line_by_line_output')} value={uiMode}
            options={[
              { value: 'chat', label: 'Chat' },
              { value: 'cli', label: 'CLI' },
            ]}
            onChange={v => setUIMode(v as 'chat' | 'cli')} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.displayPanel.zoom_font')}>
        <SettingsCard>
          {zoomSupported ? (
            <SettingsStepper label={i18nT('pages.settings.displayPanel.zoom_level')} description={i18nT('pages.settings.displayPanel.native_window_zoom_tip', { mod: modKey })} value={zoom} suffix="%" onIncrement={zoomIn} onDecrement={zoomOut} onReset={reset} />
          ) : (
            <div className="flex items-center justify-between gap-4 py-1.5">
              <div className="flex flex-col gap-0.5">
                <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.displayPanel.zoom_level')}</span>
                <span className="text-[12px] text-muted">{i18nT('pages.settings.displayPanel.use_your_browser_s_zoom_your_browser_remembers_i')}</span>
              </div>
              <span className="flex items-center gap-1 text-[12px] text-muted whitespace-nowrap">
                <kbd className="px-1.5 py-0.5 rounded border border-border bg-bg-elevated text-text font-mono text-[11px]">{modKey}</kbd>
                <kbd className="px-1.5 py-0.5 rounded border border-border bg-bg-elevated text-text font-mono text-[11px]">+</kbd>
                <span>/</span>
                <kbd className="px-1.5 py-0.5 rounded border border-border bg-bg-elevated text-text font-mono text-[11px]">−</kbd>
              </span>
            </div>
          )}
          <SettingsButtonGroup label={i18nT('pages.settings.displayPanel.font_family')} description={i18nT('pages.settings.displayPanel.ui_font_family_for_the_dashboard')} value={family}
            options={[{ value: 'sans', label: 'Sans' }, { value: 'mono', label: 'Mono' }, { value: 'system', label: 'System' }]}
            onChange={v => setFontFamily(v as 'sans' | 'mono' | 'system')} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.displayPanel.terminal')}>
        <SettingsCard>
          {/* Free-text family: the browser cannot enumerate OS-installed fonts, so
              the user names the font (a monospace / Nerd Font they have installed).
              resolveTerminalFontFamily quotes multi-word names and appends a
              monospace fallback, and the change is pushed live onto open terminals
              by CliPanel's font subscription. No placeholder or unit suffix: a raw
              font stack / "px" is Latin the en-XA i18n-render gate flags as
              untranslated, and neither is translatable copy that could be a catalog
              value — the descriptions carry the guidance and the unit instead. */}
          <SettingsInput
            label={i18nT('pages.settings.displayPanel.terminal_font_family')}
            description={i18nT('pages.settings.displayPanel.terminal_font_family_desc')}
            value={termFont.fontFamily}
            onChange={setTerminalFontFamily}
            aria-label={i18nT('pages.settings.displayPanel.terminal_font_family')}
          />
          <SettingsStepper
            label={i18nT('pages.settings.displayPanel.terminal_font_size')}
            description={i18nT('pages.settings.displayPanel.terminal_font_size_desc')}
            value={termFont.fontSize}
            onIncrement={() => setTerminalFontSize(termFont.fontSize + 1)}
            onDecrement={() => setTerminalFontSize(termFont.fontSize - 1)}
            onReset={() => setTerminalFontSize(DEFAULT_TERMINAL_FONT_SIZE)}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.displayPanel.theme')}>
        <SettingsCard>
          <div className="flex items-center gap-2">
            <div className="flex-1 min-w-0">
              <SettingsSelect label={i18nT('pages.settings.displayPanel.theme')} description={i18nT('pages.settings.displayPanel.select_a_theme_for_the_dashboard')} value={colorTheme}
                options={allThemes.map(t => t.value)} optionLabels={allThemes.map(t => t.label)}
                onChange={v => setColorTheme(v as ColorTheme)} />
            </div>
            {themeSwitching && <StatusIndicator label={i18nT('pages.settings.displayPanel.applying')} />}
          </div>
          <SettingsButtonGroup label={i18nT('pages.settings.displayPanel.mode')} description={i18nT('pages.settings.displayPanel.light_or_dark_appearance_for_the_dashboard')} value={preference}
            options={[
              { value: 'system', label: 'Auto', icon: <svg className="w-3.5 h-3.5 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg> },
              { value: 'light', label: 'Light', icon: <svg className="w-3.5 h-3.5 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg> },
              { value: 'dark', label: 'Dark', icon: <svg className="w-3.5 h-3.5 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg> },
            ]}
            onChange={v => setTheme(v as 'system' | 'light' | 'dark')} />

          {allThemes.filter(t => t.custom).length > 0 && (
            <div className="flex flex-col gap-1.5 pt-2">
              <span className="text-[12px] text-muted font-medium uppercase tracking-[.04em]">{i18nT('pages.settings.displayPanel.custom_installed_themes')}</span>
              {allThemes.filter(t => t.custom).map(t => (
                <div key={t.value} className="flex items-center justify-between px-3 py-2 rounded-md bg-bg-elevated border border-border">
                  <span className="text-[13px] text-text font-medium">{t.label}</span>
                  <div className="flex items-center gap-2">
                    {!t.installed && (
                      <button className="text-[13px] text-muted hover:text-text cursor-pointer bg-transparent border-none transition-colors" onClick={() => editor.openEditTheme(t.value.replace('custom-', ''))}>{i18nT('pages.settings.displayPanel.edit')}</button>
                    )}
                    <button className="text-[13px] text-muted hover:text-danger cursor-pointer bg-transparent border-none transition-colors" onClick={() => editor.handleDelete(t.value.replace('custom-', ''))}>{i18nT('pages.settings.displayPanel.delete')}</button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="pt-1">
            <button className="px-2.5 py-1 rounded-md text-[13px] font-medium border border-dashed border-border-strong text-muted hover:text-accent hover:border-accent cursor-pointer transition-all bg-transparent" onClick={editor.openNewTheme}>{i18nT('pages.settings.displayPanel.new_theme')}</button>
          </div>

          <div className="flex flex-col gap-1.5 pt-2">
            <span className="text-[12px] text-muted font-medium uppercase tracking-[.04em]">{i18nT('pages.settings.displayPanel.install_theme')}</span>
            <div className="flex items-center gap-2">
              {/* minWidth floors the trigger so the row does not reflow when the
                  value flips to the wider "Local folder" — the native select it
                  replaced sized itself to its widest option, and the location
                  input beside it is `flex-1`, so an auto-width trigger would
                  resize the input on every change. */}
              <SimpleSelect
                options={['github', 'local']}
                optionLabels={[i18nT('pages.settings.displayPanel.github'), i18nT('pages.settings.displayPanel.local_folder')]}
                value={installType}
                onChange={v => setInstallType(v as 'github' | 'local')}
                aria-label={i18nT('pages.settings.displayPanel.theme_source')}
                style={{ minWidth: 140 }}
              />
              {/* The shared `Input`, not a hand-styled one: it carries the same
                  `px-3 py-2 text-sm bg-bg-elevated` recipe as the dropdown
                  trigger beside it, so the row's three controls line up. The
                  raw input this replaces ran `px-2.5 py-1.5 text-[13px] bg-bg`
                  and sat visibly shorter and darker than the picker. */}
              <Input aria-label={i18nT('pages.settings.displayPanel.theme_source_location')} value={installValue}
                onChange={e => setInstallValue(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleInstall() }}
                placeholder={installType === 'github' ? 'https://github.com/user/theme' : '/path/to/theme'}
                className="min-w-0" />
              <button onClick={handleInstall} disabled={installBusy || !installValue.trim()}
                aria-live="polite"
                className="inline-flex items-center gap-1.5 text-sm px-3 py-2 rounded-md border border-border-strong text-muted hover:text-accent hover:border-accent cursor-pointer transition-all bg-transparent disabled:opacity-50 disabled:cursor-not-allowed">
                {installBusy && <StatusSpinner />}
                {installBusy ? (installPhase === 'applying' ? i18nT('pages.settings.displayPanel.applying') : i18nT('pages.settings.displayPanel.fetching')) : i18nT('pages.settings.displayPanel.install')}
              </button>
            </div>
            <ErrorNotice message={installError} variant="inline" />
          </div>
        </SettingsCard>
      </SettingsSection>

      {editor.editorOpen && (
        <Clickable className="fixed inset-0 z-[49] flex items-center justify-center bg-black/50 backdrop-blur-sm" onClick={e => { if (!e || e.target === e.currentTarget) editor.closeEditor() }}>
          <div role="dialog" aria-modal="true" aria-label={editor.isEditing ? i18nT('pages.settings.displayPanel.edit_theme') : i18nT('pages.settings.displayPanel.create_theme')} className="relative z-10 w-full max-w-2xl max-h-[85vh] overflow-y-auto mx-4 bg-card border border-border rounded-xl p-6 shadow-xl animate-rise">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-bold text-text-strong">{editor.isEditing ? i18nT('pages.settings.displayPanel.edit_theme') : i18nT('pages.settings.displayPanel.create_theme')}</h3>
              <button className="text-muted text-[13px] cursor-pointer hover:text-text bg-transparent border-none" onClick={editor.closeEditor}><X className="lucide-inline" /></button>
            </div>
            <ThemeEditorPanel editor={editor} />
          </div>
        </Clickable>
      )}

      {/* Sidebar Colors */}
      <SettingsSection title={i18nT('pages.settings.displayPanel.sidebar_colors')}>
        <SettingsCard>
          <SettingsButtonGroup
            label={i18nT('pages.settings.displayPanel.palette')}
            description={i18nT('pages.settings.displayPanel.choose_a_color_palette_for_your_sidebar_sessions')}
            value={paletteName}
            options={PALETTE_NAMES.map(p => ({ value: p, label: p.charAt(0).toUpperCase() + p.slice(1) }))}
            onChange={v => dispatch(setSessionColorsPalette(v as PaletteName))}
          />
          <SettingsButtonGroup
            label={i18nT('pages.settings.displayPanel.intensity')}
            description={i18nT('pages.settings.displayPanel.how_visible_the_color_tint_is_on_sidebar_rows')}
            value={intensity}
            options={INTENSITY_NAMES.map(n => ({ value: n, label: n.charAt(0).toUpperCase() + n.slice(1) }))}
            onChange={v => dispatch(setSessionColorsIntensity(v as IntensityName))}
          />
          <SettingsButtonGroup
            label={i18nT('pages.settings.displayPanel.display_mode')}
            description={i18nT('pages.settings.displayPanel.how_the_session_color_is_applied_to_the_row')}
            value={colorMode}
            options={[{ value: 'tint', label: 'Solid Tint' }, { value: 'gradient', label: 'Gradient' }]}
            onChange={v => dispatch(setSessionColorsMode(v as SessionColorMode))}
          />
          <SettingsStepper
            label={i18nT('pages.settings.displayPanel.highlight_recent_sessions')}
            description={i18nT('pages.settings.displayPanel.highlight_the_n_most_recently_active_sessions_wi')}
            value={recentTintCount}
            onIncrement={() => setTintCount(recentTintCount + 1)}
            onDecrement={() => setTintCount(recentTintCount - 1)}
            onReset={() => setTintCount(RECENT_TINT_COUNT)}
          />
          {/* Color swatches use raw buttons — circular color dots don't fit SettingsButtonGroup's text-button pattern */}
          <div className="flex flex-col gap-1.5 py-1.5">
            <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.displayPanel.default_for_new_sessions')}</span>
            <div className="text-[12px] text-muted">{i18nT('pages.settings.displayPanel.none_auto_cycle_or_pick_a_fixed_color')}</div>
            <div className="flex flex-wrap items-center gap-1.5">
              <button type="button" aria-label={i18nT('pages.settings.displayPanel.no_color')} aria-pressed={defaultColor === null} className={`w-7 h-7 rounded-full border-2 cursor-pointer transition-transform hover:scale-110 ${defaultColor === null ? 'border-accent scale-110' : 'border-border'}`} style={{ background: 'var(--bg-accent)', backgroundImage: 'linear-gradient(135deg, transparent 45%, var(--danger) 45%, var(--danger) 55%, transparent 55%)' }} onClick={() => dispatch(setSessionDefaultColor(null))} title={i18nT('pages.settings.displayPanel.no_color')} />
              {colors.map((c, i) => (
                <button type="button" key={i} aria-label={i18nT('pages.settings.displayPanel.color', { n: i + 1 })} aria-pressed={defaultColor === i} className={`w-7 h-7 rounded-full border-2 cursor-pointer transition-transform hover:scale-110 ${defaultColor === i ? 'border-accent scale-110' : 'border-border'}`} style={{ background: `linear-gradient(135deg, color-mix(in srgb, ${c} ${boost.activePct[i]}%, var(--bg-accent)) 50%, color-mix(in srgb, ${c} ${boost.idlePct[i]}%, var(--bg-accent)) 50%)` }} onClick={() => dispatch(setSessionDefaultColor(i))} title={i18nT('pages.settings.displayPanel.color', { n: i + 1 })} />
              ))}
              <button type="button" className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium cursor-pointer border transition-all ${defaultColor === 'auto' ? 'bg-accent-subtle text-accent border-accent' : 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'}`} onClick={() => dispatch(setSessionDefaultColor('auto'))}>{i18nT('pages.settings.displayPanel.auto')}</button>
            </div>
          </div>
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
