import { Loader2 } from 'lucide-react'
import { useState, useMemo } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { useZoomCtx } from '../../hooks/ZoomProvider'
import type { FontFamily } from '../../hooks/useZoom'
import { useTheme } from '../../hooks/useTheme'
import type { ColorTheme } from '../../hooks/useTheme'
import { useUIMode } from '../../hooks/useUIMode'
import { SettingsSection, SettingsCard, SettingsSelect, SettingsStepper, SettingsButtonGroup, SettingsToggle, SettingsInput, SettingsCombobox } from '../../components/settings'
import { usePlainDiff } from '../../hooks/usePlainDiff'
import SimpleSelect from '../../components/SimpleSelect'
import { Input } from '../../components/ui'
import { useThemeEditor, ThemeEditorPanel } from '../../components/themeEditor'
import Modal from '../../components/Modal'
import { useAppSelector, useAppDispatch } from '../../store'
import { setSessionDefaultColor, setSessionColorsMode, setSessionColorsPalette, setSessionColorsIntensity } from '../../store/dashboardSlice'
import { useSessionPalette } from '../../hooks/useSessionPalette'
import { PALETTE_NAMES, INTENSITY_NAMES } from '../../utils/sessionColors'
import type { DefaultColorSetting, PaletteName, IntensityName, SessionColorMode } from '../../utils/sessionColors'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import { useOptimisticConfigPaths, setConfigPathValue } from './useOptimisticConfigPaths'
import { parseErrorCode } from '../../utils/errorReport'
import { clampTintCount, RECENT_TINT_COUNT } from '../../utils/recencyTint'
import { useLanguage } from '../../i18n/LanguageProvider'
import { AUTO_LANGUAGE, PICKABLE_LANGUAGES, languageLabel } from '../../i18n/languages'
import {
  useTerminalFont,
  setTerminalFontFamily,
  setTerminalFontSize,
  DEFAULT_TERMINAL_FONT_SIZE,
} from '../../hooks/useTerminalFont'
import { FONT_FAMILY_OPTIONS, OPENDYSLEXIC_MONO_FAMILY_NAME } from '../../utils/fontFamilyOptions'
import { useFontOptions } from '../../hooks/useFontOptions'
import { isFontInstalled, monospaceFontStack } from '../../utils/fontDetect'

import { i18nT } from '../../i18n/t'
import { ThemeDroppedRulesNotice } from './ThemeDroppedRulesNotice'
import ErrorNotice from '../../components/ErrorNotice'
import { useImeGuard } from '../../hooks/useImeGuard'
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
  const ime = useImeGuard()
  const { language, detected: detectedLanguage, setLanguage, syncFailed: langSyncFailed } = useLanguage()
  const { zoom, zoomSupported, zoomIn, zoomOut, reset, family, setFontFamily } = useZoomCtx()
  // Shortcut label for the zoom hint/description: ⌘ on macOS, Ctrl elsewhere.
  const modKey = /mac/i.test(navigator.platform) ? '⌘' : 'Ctrl'
  const { preference, setTheme, colorTheme, setColorTheme, allThemes, loadCustomThemes, themeSwitching, overridesDropReport } = useTheme()
  const { uiMode, setUIMode } = useUIMode()
  // Browser-local like the theme and terminal font above: the toggle below has no
  // `configKey` because nothing about it reaches the server config.
  const [plainDiff, setPlainDiff] = usePlainDiff()
  const editor = useThemeEditor()
  const termFont = useTerminalFont()
  // Probed families become picker rows previewed in their own family, so the
  // Powerline sample answers "will my prompt theme render" before the choice is
  // committed. The default row's value is the empty string the store already uses
  // for "no explicit family" — see useTerminalFont.
  const { families: fontFamilies, accessSupported: fontAccessSupported, lastResult: fontDetectResult, enumerate: enumerateFonts } = useFontOptions()
  // The row's own name, rendered in its own family, IS the preview. No specimen
  // string beside it: a 5-glyph sample is the part a reader would have to
  // scrutinise, yet it sits in the sublabel slot the component styles as
  // recede-into-the-background metadata — and the trigger folds a sublabel into
  // "<family> (<sample>)" once a font is picked.
  const fontPreview = (family: string) => ({
    previewFontFamily: monospaceFontStack(family),
  })
  // Only the font names are memoized. The default row's label is a CATALOG
  // string, and caching one across renders is what makes it stick in the wrong
  // language: i18next resolves its resources after the first paint, so a label
  // captured in a memo keeps the English fallback that first render returned and
  // no dep change ever invalidates it. Resolved inline instead, like the
  // description beside it.
  const fontRows = useMemo(
    () => fontFamilies.map(family => ({ value: family, label: family, ...fontPreview(family) })),
    [fontFamilies],
  )
  // The bundled OpenDyslexicMono row sits between the Default row and the OS-
  // detected list. Always selectable regardless of what Local Font Access
  // reports, because the browser has the family loaded from the page's own
  // @font-face declaration. Users who pick OpenDyslexic for the dashboard can
  // apply OpenDyslexicMono to the terminal without having to type the family
  // name. A future bundled mono face becomes a second inlined row.
  const fontOptions = [
    { value: '', label: i18nT('pages.settings.displayPanel.terminal_font_default') },
    {
      value: OPENDYSLEXIC_MONO_FAMILY_NAME,
      label: OPENDYSLEXIC_MONO_FAMILY_NAME,
      ...fontPreview(OPENDYSLEXIC_MONO_FAMILY_NAME),
    },
    ...fontRows,
  ]
  // A literal key per branch, never an assembled one: a key built from parts is
  // invisible to the catalog reference scanner, so a missing translation would
  // ship as a raw identifier instead of failing the gate.
  //
  // `added` reports too, even though the list grows: the user most likely to run
  // this action is the one whose filter matched nothing, and a filter that also
  // hides every newly added family leaves the popup pixel-identical after a
  // permission grant — which reads as the grant having failed.
  const fontDetectStatus = fontDetectResult === 'checking'
    ? i18nT('pages.settings.displayPanel.terminal_font_detect_checking')
    : fontDetectResult === 'added'
      ? i18nT('pages.settings.displayPanel.terminal_font_detect_added')
      : fontDetectResult === 'denied'
        ? i18nT('pages.settings.displayPanel.terminal_font_detect_denied')
        : fontDetectResult === 'none'
          ? i18nT('pages.settings.displayPanel.terminal_font_detect_none')
          : undefined

  const dispatch = useAppDispatch()
  const { paletteColors: colors, colorMode, paletteName, intensity, boost } = useSessionPalette()
  const defaultColor = useAppSelector(s => s.dashboard.sessionDefaultColor) as DefaultColorSetting

  // Recency-tint count is persisted server-side (dashboard.recent_tint_count) via the shared
  // kirocrewConfig query, so the choice follows the user across browsers/restarts.
  const qc = useQueryClient()
  type KirocrewCfg = { dashboard?: { recent_tint_count?: number; terminal?: { shell?: string } } }
  const mcQ = useQuery<KirocrewCfg>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  // Per-path optimistic display shared by the tint and shell saves below.
  // Both PATCH the same ['kirocrewConfig'] object, so a whole-object
  // onMutate snapshot here is a live race: a tint rollback would restore a
  // pre-shell-save snapshot, transiently reverting an in-flight shell save
  // (and vice versa). Each control instead renders `shown(path, server)`;
  // full lifecycle contract in useOptimisticConfigPaths.ts. The sidebar tint
  // (which reads the same query) re-ranks when the save is accepted rather
  // than at click time — the stepper itself stays instant via the overlay.
  const overlay = useOptimisticConfigPaths(qc)
  const recentTintCount = clampTintCount(mcQ.data?.dashboard?.recent_tint_count)
  const shownTintCount = overlay.shown('dashboard.recent_tint_count', recentTintCount)
  const tintMut = useMutation(overlay.mutationOpts<number>({
    queryKey: ['kirocrewConfig'],
    mutationFn: (value: number) => api.patchConfig('dashboard.recent_tint_count', value),
    path: () => 'dashboard.recent_tint_count',
    displayValue: v => v,
    applyToCache: (cached, value) => setConfigPathValue(cached as KirocrewCfg, 'dashboard.recent_tint_count', value),
  }))
  // Steps are computed from the SHOWN count so rapid clicks stack on the
  // in-flight value instead of re-incrementing a stale server value.
  const setTintCount = (n: number) => tintMut.mutate(clampTintCount(n))

  // Default shell for the built-in terminal — persisted server-side
  // (dashboard.terminal.shell) because the SHELL is spawned by the gateway
  // host, unlike the terminal font above, which is a per-client rendering
  // choice and stays in localStorage. Drafted locally and committed on blur so
  // a half-typed path is never persisted. The overlay keeps the just-saved
  // value shown after onSuccess clears the draft, because React Query serves
  // the stale cache while refetching — without it the value would blink back
  // to the previous one for a round-trip, which reads as a failed save.
  // Errors are mapped from the response's machine-readable `code` to catalog
  // keys: the backend's English sentence must never render verbatim in a
  // 12-language dashboard.
  const serverShell = mcQ.data?.dashboard?.terminal?.shell ?? ''
  const shownShell = overlay.shown('dashboard.terminal.shell', serverShell)
  const [shellDraft, setShellDraft] = useState<string | null>(null)
  const [shellError, setShellError] = useState<string | null>(null)
  const shellOpts = overlay.mutationOpts<string>({
    queryKey: ['kirocrewConfig'],
    mutationFn: (value: string) => api.patchConfig('dashboard.terminal.shell', value),
    path: () => 'dashboard.terminal.shell',
    displayValue: v => v,
    applyToCache: (cached, value) => setConfigPathValue(cached as KirocrewCfg, 'dashboard.terminal.shell', value),
    onFailure: e => {
      const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
      setShellError(i18nT(
        code === 'shell_not_executable'
          ? 'pages.settings.displayPanel.terminal_shell_not_executable'
          : 'pages.settings.displayPanel.terminal_shell_save_failed',
      ))
    },
    onSupersede: () => setShellError(null),
  })
  const shellMut = useMutation({
    ...shellOpts,
    onSuccess: (data: unknown, value: string, token: number) => {
      setShellDraft(null)
      setShellError(null)
      return shellOpts.onSuccess(data, value, token)
    },
  })
  const commitShell = () => {
    if (shellDraft === null) return
    const value = shellDraft.trim()
    if (value === shownShell) {
      setShellDraft(null)
      setShellError(null)
      return
    }
    shellMut.mutate(value)
  }

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
          {/* Phrased as "plain diffs ON" rather than "highlighting OFF" so the
              switch position matches the stored value — no inverted checkbox.
              Browser-local, hence no `configKey`. */}
          <SettingsToggle
            label={i18nT('settings.display.plainDiff.label')}
            description={i18nT('settings.display.plainDiff.description')}
            checked={plainDiff}
            onChange={setPlainDiff}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.displayPanel.zoom_font')}>
        <SettingsCard index={1}>
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
          <SettingsButtonGroup label={i18nT('pages.settings.displayPanel.font_family')} description={i18nT('pages.settings.displayPanel.ui_font_family_for_the_dashboard_code_font_follo')} value={family}
            options={FONT_FAMILY_OPTIONS.map(o => ({ value: o.value, label: o.labelKey ? i18nT(o.labelKey) : o.label! }))}
            onChange={v => setFontFamily(v as FontFamily)} />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.displayPanel.terminal')}>
        <SettingsCard index={2}>
          {/* Detected families, not free text alone: the fonts that matter are the
              ones installed on the machine RENDERING the terminal, which is the
              browser's machine — xterm rasterizes client-side while the pty lives on
              the gateway host, so a host-side enumeration would list the wrong
              computer whenever the dashboard is reached over a tunnel. Hence probing
              in the browser (see useFontOptions), with the typed value still
              committable because the probe can only confirm names it is handed.
              resolveTerminalFontFamily quotes multi-word names and appends a
              monospace fallback, and the change is pushed live onto open terminals
              by CliPanel's font subscription. */}
          <SettingsCombobox
            label={i18nT('pages.settings.displayPanel.terminal_font_family')}
            description={i18nT('pages.settings.displayPanel.terminal_font_family_desc')}
            value={termFont.fontFamily}
            options={fontOptions}
            onChange={setTerminalFontFamily}
            triggerFallback={termFont.fontFamily || i18nT('pages.settings.displayPanel.terminal_font_default')}
            searchPlaceholder={i18nT('pages.settings.displayPanel.terminal_font_search')}
            customValueOption={typed => (isFontInstalled(typed)
              ? {
                label: i18nT('pages.settings.displayPanel.terminal_font_use_typed', { value: typed }),
                ...fontPreview(typed),
              }
              // No preview for a name that did not resolve. Styling the sample
              // with an uninstalled family renders it in the fallback, which is
              // indistinguishable from a confirmed font — the row would quietly
              // confirm a typo, which is the failure this picker exists to end.
              // Still committable: the family may be installed later, or on
              // another machine this preference is not synced to.
              : {
                label: i18nT('pages.settings.displayPanel.terminal_font_use_typed', { value: typed }),
                sublabel: i18nT('pages.settings.displayPanel.terminal_font_not_detected'),
              })}
            action={fontAccessSupported
              ? { label: i18nT('pages.settings.displayPanel.terminal_font_detect'), onSelect: enumerateFonts }
              : undefined}
            actionStatus={fontDetectStatus}
          />
          <SettingsStepper
            label={i18nT('pages.settings.displayPanel.terminal_font_size')}
            description={i18nT('pages.settings.displayPanel.terminal_font_size_desc')}
            value={termFont.fontSize}
            onIncrement={() => setTerminalFontSize(termFont.fontSize + 1)}
            onDecrement={() => setTerminalFontSize(termFont.fontSize - 1)}
            onReset={() => setTerminalFontSize(DEFAULT_TERMINAL_FONT_SIZE)}
          />
          {/* Free text with commit-on-blur, mirroring the font field above: the
              gateway host's installed shells cannot be enumerated from the
              browser (reading /etc/shells is host-side and absent on some
              systems), so the user names the shell and the backend validates
              it is an executable before persisting. No placeholder: a raw
              path is Latin the en-XA render gate flags, and it is not
              translatable copy — the description carries the guidance. */}
          {/* Disabled while the save is in flight: a slow PATCH's onSuccess
              clears the draft, and text typed in the meantime would vanish
              with it. Locking the field for the round-trip makes that
              interleaving unrepresentable (same pattern as SttSettings). */}
          <SettingsInput
            label={i18nT('pages.settings.displayPanel.terminal_shell')}
            description={i18nT('pages.settings.displayPanel.terminal_shell_desc')}
            value={shellDraft ?? shownShell}
            onChange={setShellDraft}
            onBlur={commitShell}
            disabled={shellMut.isPending || !mcQ.isSuccess}
            configKey="dashboard.terminal.shell"
            aria-label={i18nT('pages.settings.displayPanel.terminal_shell')}
          />
          <ErrorNotice message={shellError} variant="inline" />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.displayPanel.theme')}>
        <SettingsCard index={3}>
          <div className="flex items-center gap-2">
            <div className="flex-1 min-w-0">
              <SettingsSelect label={i18nT('pages.settings.displayPanel.theme')} description={i18nT('pages.settings.displayPanel.select_a_theme_for_the_dashboard')} value={colorTheme}
                options={allThemes.map(t => t.value)} optionLabels={allThemes.map(t => t.label)}
                onChange={v => setColorTheme(v as ColorTheme)} />
            </div>
            {themeSwitching && <StatusIndicator label={i18nT('pages.settings.displayPanel.applying')} />}
          </div>
          {/* Surface scoper-dropped overrides.css rules for the ACTIVE
              theme. The slug guard is belt-and-braces for the switch race — the
              provider clears the report on theme change, but a stale report must
              never be attributed to the wrong pack. */}
          {overridesDropReport && colorTheme === `custom-${overridesDropReport.slug}` && (
            <ThemeDroppedRulesNotice report={overridesDropReport} />
          )}
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
                {...ime.bindEnter({ onEnter: () => handleInstall() })}
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

      {/* The shared Modal owns the backdrop, Escape dismissal, the focus
          trap/restore, the scroll lock and the keyboard isolation the
          hand-rolled overlay lacked, and it portals to document.body so the
          editor still escapes the SettingsCard's card-glow stacking context.
          The panel rises from z-[49] to Modal's own z-[100]/[101] layer, which
          is what puts it above the floating theme-experience toggle instead of
          under it. The dialog keeps its accessible name from its own title.

          `guardAccidentalDismiss` is gated on the editor being dirty, because
          Escape dismissal is a path this conversion ADDS and `closeEditor`
          discards the draft unconditionally: on an untouched form both
          accidental exits still work (Escape is the capability the issue asks
          for), and once a name or a colour has been entered only the explicit
          exits — the header close button and the panel's own Cancel — close it. */}
      <Modal
        open={editor.editorOpen}
        onClose={editor.closeEditor}
        title={editor.isEditing ? i18nT('pages.settings.displayPanel.edit_theme') : i18nT('pages.settings.displayPanel.create_theme')}
        maxWidth={672}
        guardAccidentalDismiss={editor.isDirty}
      >
        <ThemeEditorPanel editor={editor} />
      </Modal>

      {/* Sidebar Colors */}
      <SettingsSection title={i18nT('pages.settings.displayPanel.sidebar_colors')}>
        <SettingsCard index={4}>
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
            value={shownTintCount}
            onIncrement={() => setTintCount(shownTintCount + 1)}
            onDecrement={() => setTintCount(shownTintCount - 1)}
            onReset={() => setTintCount(RECENT_TINT_COUNT)}
            disabled={!mcQ.isSuccess}
          />
          {/* Color swatches use raw buttons — circular color dots don't fit SettingsButtonGroup's text-button pattern */}
          <div className="flex flex-col gap-1.5 py-1.5" data-setting-label={i18nT('pages.settings.displayPanel.default_for_new_sessions')}>
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
