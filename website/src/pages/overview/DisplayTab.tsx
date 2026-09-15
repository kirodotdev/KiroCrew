import { Monitor, Sun, Moon, Pencil } from 'lucide-react'
import { useZoomCtx } from '../../hooks/ZoomProvider'
import { useTheme } from '../../hooks/useTheme'
import { Card, CardTitle } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { useThemeEditor, ThemeEditorPanel } from '../../components/themeEditor'
import { FONT_FAMILY_OPTIONS } from '../../utils/fontFamilyOptions'
import type { FontFamily } from '../../hooks/useZoom'

import { i18nT } from '../../i18n/t'
const BTN = 'px-3 py-1 rounded-full text-[13px] cursor-pointer border transition-all'
const active = (on: boolean) => on ? 'bg-accent-subtle text-accent border-accent' : 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'

/**
 * Font Family button labels for this tab.
 *
 * The Sans / Mono / System keys are pre-existing DisplayTab-scoped translations;
 * OpenDyslexic reuses the DisplayPanel catalog key so the label is a single
 * source of truth. Kept as a `Record<FontFamily, string>` so every FontFamily
 * union member is proven to have a label at compile time — a new family added
 * to `FONT_FAMILY_OPTIONS` without a label here fails typecheck.
 */
const FONT_FAMILY_LABEL_KEY: Record<FontFamily, string> = {
  sans: 'pages.overview.displayTab.sans',
  mono: 'pages.overview.displayTab.mono',
  system: 'pages.overview.displayTab.system',
  opendyslexic: 'pages.settings.displayPanel.font_family_option_opendyslexic',
}

export default function DisplayTab() {
  const { zoom, zoomSupported, zoomIn, zoomOut, reset, family, setFontFamily } = useZoomCtx()
  const { preference, setTheme, colorTheme, setColorTheme, allThemes } = useTheme()
  const editor = useThemeEditor()
  const modKey = /mac/i.test(navigator.platform) ? '⌘' : 'Ctrl'

  return (
    <div className="grid grid-cols-2 gap-4 max-[900px]:grid-cols-1">
      <Card>
        <CardTitle>{i18nT('pages.overview.displayTab.zoom')} <InfoTip text={zoomSupported ? i18nT('pages.overview.displayTab.native_window_zoom_tip', { mod: modKey }) : i18nT('pages.overview.displayTab.use_your_browser_s_zoom_your_browser_remembers_i')} /></CardTitle>
        {zoomSupported ? (
          <div className="flex items-center gap-2">
            <button className={BTN + ' ' + active(false)} onClick={zoomOut}>−</button>
            <button className={BTN + ' ' + active(false)} onClick={reset}>{zoom}%</button>
            <button className={BTN + ' ' + active(false)} onClick={zoomIn}>+</button>
          </div>
        ) : (
          <div className="text-[13px] text-muted">{i18nT('pages.overview.displayTab.zoom_with')} {modKey} + / {modKey} −</div>
        )}
      </Card>
      <Card>
        <CardTitle>{i18nT('pages.overview.displayTab.font')} <InfoTip text={i18nT('pages.overview.displayTab.change_the_dashboard_font_family_persists_across')} /></CardTitle>
        <div className="flex items-center gap-2">
          {FONT_FAMILY_OPTIONS.map(({ value: f }) => (
            <button key={f} className={BTN + ' ' + active(family === f)} onClick={() => setFontFamily(f)}>
              {i18nT(FONT_FAMILY_LABEL_KEY[f])}
            </button>
          ))}
        </div>
      </Card>
      <Card>
        <CardTitle>{i18nT('pages.overview.displayTab.mode')} <InfoTip text={i18nT('pages.overview.displayTab.switch_between_color_schemes_auto_follows_your_o')} /></CardTitle>
        <div className="flex items-center gap-2">
          {(['system', 'light', 'dark'] as const).map(t => (
            <button key={t} className={BTN + ' ' + active(preference === t)} onClick={() => setTheme(t)}>
              {t === 'system' ? <><Monitor className="lucide-inline" /> {i18nT('pages.overview.displayTab.auto')}</> : t === 'light' ? <><Sun className="lucide-inline" /> {i18nT('pages.overview.displayTab.light')}</> : <><Moon className="lucide-inline" /> {i18nT('pages.overview.displayTab.dark')}</>}
            </button>
          ))}
        </div>
      </Card>
      <Card>
        <CardTitle>{i18nT('pages.overview.displayTab.color_theme')} <InfoTip text={i18nT('pages.overview.displayTab.choose_a_color_palette_each_theme_supports_dark')} /></CardTitle>
        <div className="flex flex-wrap items-center gap-2">
          {allThemes.map(t => (
            <div key={t.value} className="relative group">
              <button className={BTN + ' ' + active(colorTheme === t.value)} onClick={() => setColorTheme(t.value)}>
                {t.label}
              </button>
              {t.custom && (
                <button
                  onClick={(e) => { e.stopPropagation(); editor.openEditTheme(t.value.replace('custom-', '')) }}
                  className="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-accent text-accent-fg text-[10px] leading-none flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer"
                  title={i18nT('pages.overview.displayTab.edit_theme')}
                ><Pencil className="lucide-inline" /></button>
              )}
            </div>
          ))}
          <button
            className={BTN + ' ' + (editor.editorOpen
              ? 'bg-accent-subtle text-accent border-accent'
              : 'border-dashed border-border-strong text-muted hover:text-accent hover:border-accent transition-colors'
            )}
            onClick={editor.editorOpen ? editor.closeEditor : editor.openNewTheme}
          >
            {editor.editorOpen && !editor.isEditing ? <><Pencil className="lucide-inline" /> {i18nT('pages.overview.displayTab.creating')}</> : editor.editorOpen && editor.isEditing ? <><Pencil className="lucide-inline" /> {i18nT('pages.overview.displayTab.editing')}</> : i18nT('pages.overview.displayTab.new_theme')}
          </button>
        </div>

        {editor.editorOpen && (
          <div className="mt-4 border-t border-border pt-4 animate-rise">
            <ThemeEditorPanel editor={editor} />
          </div>
        )}
      </Card>
    </div>
  )
}
