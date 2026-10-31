/**
 * Mochi - Color Customizer Panel
 *
 * Preset picker grid (each preset shows an SVG preview) + manual color editor
 * with per-body-part highlighting. i18n via useT().
 */
import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { Check, X } from 'lucide-react'
import { extractSvgColors, applySvgColorMap, type ColorMap } from '../shared/colorCustomizer'
import { PresetRegistry, extractSwatches, type CatPreset } from '../shared/catPresets'
import { BUILT_IN_CAT_PRESETS, presetDisplayName } from '../shared/builtInCatPresets'
import { toDataUri } from './animationResolver'
import { api } from '../mochiApi'
import { i18nT } from '../../../../i18n/t'
import { useImeGuard } from '../../../../hooks/useImeGuard'

/** i18n key for each source color's body part label */
const BODY_PART_KEYS: Record<string, string> = {
  '#F9A85F': 'apps.mochi.color.body_fur',
  '#F18D50': 'apps.mochi.color.ears_shadow',
  '#EB8849': 'apps.mochi.color.chin_legs',
  '#E98649': 'apps.mochi.color.belly',
  '#FCD9B3': 'apps.mochi.color.tummy_paws',
  '#F49681': 'apps.mochi.color.inner_ear',
  '#F5E6CB': 'apps.mochi.color.paws',
  '#522210': 'apps.mochi.color.outlines',
  '#522214': 'apps.mochi.color.body_outline',
  '#391F19': 'apps.mochi.color.shadows',
}

const CS = {
  root: { marginTop: 16 },
  section: { fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase' as const, letterSpacing: 1, marginBottom: 8, marginTop: 12 },
  presetGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(90px, 1fr))', gap: 8, marginBottom: 12 },
  presetCard: (active: boolean) => ({
    borderRadius: 8, textAlign: 'center' as const,
    border: active ? '2px solid var(--accent)' : '1px solid var(--border)',
    background: active ? 'var(--accent-glow)' : 'var(--bg-input)',
    transition: 'border-color 150ms',
  }),
  // The tile's "apply" button fills the tile and carries its padding, so the
  // tile looks and clicks as before. Its radius follows the tile's, so the
  // keyboard focus ring is rounded like the tile rather than square.
  presetSelect: (hasDelete: boolean) => ({
    display: 'block', width: '100%', padding: hasDelete ? '6px 6px 0' : 6,
    border: 'none', borderRadius: 7, background: 'none', color: 'inherit',
    font: 'inherit', textAlign: 'center' as const, cursor: 'pointer',
  }),
  presetThumb: { width: 56, height: 56, objectFit: 'contain' as const, borderRadius: 6, margin: '0 auto 4px' },
  presetName: { fontSize: 10, fontWeight: 500, lineHeight: 1.3 },
  btnRow: { display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' as const },
  btn: { padding: '6px 14px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg-input)', color: 'var(--text)', cursor: 'pointer', fontSize: 12 },
  deleteBtn: { background: 'none', border: 'none', borderRadius: 4, color: 'var(--danger)', cursor: 'pointer', fontSize: 10, marginTop: 2, position: 'relative' as const },
} as const

function PresetCard({ preset, active, svgContent, onClick, onDelete, deleteLabel }: {
  preset: CatPreset; active: boolean; svgContent: string
  onClick: () => void; onDelete?: () => void; deleteLabel: string
}) {
  const previewUri = useMemo(() => {
    const cm = preset.colorMap
    if (!cm || Object.keys(cm).length === 0) return toDataUri(svgContent)
    return toDataUri(applySvgColorMap(svgContent, cm))
  }, [preset.colorMap, svgContent])

  // Resolved beside the key map (see presetDisplayName) so the catalog-key gate can
  // verify every builtin preset key exists.
  const displayName = presetDisplayName(preset)

  return (
    // A plain tile holding two sibling buttons, "apply" and Delete. Delete used
    // to sit inside a role="button" tile whose key handler took its Enter/Space.
    <div style={{ ...CS.presetCard(active), paddingBottom: onDelete ? 6 : 0, position: 'relative' }}>
      <button type="button" aria-pressed={active} style={CS.presetSelect(!!onDelete)} onClick={onClick}>
        <img src={previewUri} alt={displayName} style={CS.presetThumb} draggable={false} />
        <span style={{ ...CS.presetName, display: 'block' }}>{displayName}</span>
        {/* Stretches the apply button's hit area over the whole tile (beside
            Delete, and the space a taller grid row adds), so a click anywhere
            applies the preset, as when the tile was the control. Delete is
            positioned after it, so it stays on top. */}
        <span aria-hidden="true" style={{ position: 'absolute', inset: 0 }} />
      </button>
      {onDelete && (
        <button type="button" style={CS.deleteBtn} onClick={onDelete}>{deleteLabel}</button>
      )}
    </div>
  )
}

function buildHighlightMap(allColors: string[], highlightSrc: string, targetColor: string): ColorMap {
  const faded = '#FAFAFA'
  const fadedDark = '#ECECEC'
  const darkColors = new Set(['#522210', '#522214', '#391F19'])
  const map: ColorMap = {}
  for (const c of allColors) {
    map[c] = c === highlightSrc ? targetColor : (darkColors.has(c) ? fadedDark : faded)
  }
  return map
}

function ColorEditorCard({ sourceColor, targetColor, bodyPart, svgContent, allColors, onChange }: {
  sourceColor: string; targetColor: string; bodyPart: string; svgContent: string
  allColors: string[]; onChange: (color: string) => void
}) {
  const highlightUri = useMemo(() => {
    return toDataUri(applySvgColorMap(svgContent, buildHighlightMap(allColors, sourceColor, targetColor)))
  }, [sourceColor, targetColor, svgContent, allColors])

  // The body-part caption IS this swatch's name, so it is bound to the input
  // instead of being restated as an aria-label the translators would have to
  // keep in step: `htmlFor` makes the caption open the colour picker, and
  // `aria-labelledby` names the input from the same node. `display: 'block'` is
  // the <div> default the caption is keeping — a <label> is inline, and without
  // it the caption's marginBottom would stop applying.
  const swatchId = useId()
  const labelId = `${swatchId}-label`

  return (
    <div style={{ padding: 6, borderRadius: 8, textAlign: 'center' as const, border: '1px solid var(--border)', background: 'var(--bg-input)' }}>
      <img src={highlightUri} alt={bodyPart} style={CS.presetThumb} draggable={false} />
      <label id={labelId} htmlFor={swatchId} style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 4, display: 'block' }}>{bodyPart}</label>
      <input id={swatchId} aria-labelledby={labelId} type="color" value={targetColor} onChange={e => onChange(e.target.value)}
        style={{ width: 36, height: 20, border: 'none', padding: 0, cursor: 'pointer', borderRadius: 4 }} />
    </div>
  )
}

interface Props { idleSvgContent: string }

export const ColorCustomizerPanel: React.FC<Props> = ({ idleSvgContent }) => {
  const ime = useImeGuard()
  const [colorMap, setColorMap] = useState<ColorMap>({})
  const [registry, setRegistry] = useState<PresetRegistry>(() => new PresetRegistry(BUILT_IN_CAT_PRESETS))
  const [activePresetId, setActivePresetId] = useState<string | null>(null)
  const [showSaveForm, setShowSaveForm] = useState(false)
  // The "Save preset" button that opens the name field. Closing the field
  // unmounts whatever had focus in it (the input, or its confirm or cancel
  // button), and a browser then drops focus to document.body, so every close
  // hands focus back here once the button has remounted (the pattern in
  // MeetingTitle). The field has no blur handler, so there is no pointer-blur
  // path to exclude.
  const saveTriggerRef = useRef<HTMLButtonElement | null>(null)
  const closeSaveForm = useCallback(() => {
    setShowSaveForm(false)
    // Only take back focus the unmount dropped: a save can resolve after the
    // user has already moved on to another control, and that control keeps it.
    requestAnimationFrame(() => {
      const at = document.activeElement
      if (at === null || at === document.body) saveTriggerRef.current?.focus({ preventScroll: true })
    })
  }, [])
  const [saveNameInput, setSaveNameInput] = useState('')
  const sourceColors = useMemo(() => extractSvgColors(idleSvgContent), [idleSvgContent])

  useEffect(() => {
    (async () => {
      const [saved, customs] = await Promise.all([
        api?.presetsGetColorMap?.('default-mochi'),
        api?.presetsLoadCustom?.(),
      ])
      if (saved && Object.keys(saved).length > 0) setColorMap(saved)
      if (customs?.length) setRegistry(new PresetRegistry(BUILT_IN_CAT_PRESETS, customs))
    })()
  }, [])

  const applyPreset = useCallback((preset: CatPreset) => {
    setColorMap(preset.colorMap)
    setActivePresetId(preset.id)
    api?.gallerySetColorMap?.('default-mochi', preset.colorMap)
  }, [])

  const handleColorChange = useCallback((sourceColor: string, targetColor: string) => {
    setColorMap(prev => {
      const next = { ...prev, [sourceColor]: targetColor }
      api?.gallerySetColorMap?.('default-mochi', next)
      return next
    })
    setActivePresetId(null)
  }, [])

  const handleReset = useCallback(async () => {
    setColorMap({})
    setActivePresetId(null)
    await api?.gallerySetColorMap?.('default-mochi', {})
  }, [])

  const handleSaveAsPreset = useCallback(async () => {
    const name = saveNameInput.trim()
    if (!name) return
    const swatches = extractSwatches(colorMap)
    if (swatches.length < 2) swatches.push(...Object.values(colorMap).slice(0, 2 - swatches.length))
    registry.addCustomPreset({ name, description: '', colorMap, swatches })
    const customs = registry.getCustomPresets()
    setRegistry(new PresetRegistry(BUILT_IN_CAT_PRESETS, customs))
    await api?.presetsSaveCustom?.(customs)
    setSaveNameInput('')
    closeSaveForm()
  }, [closeSaveForm, colorMap, registry, saveNameInput])

  const handleDeletePreset = useCallback(async (id: string) => {
    registry.removeCustomPreset(id)
    const customs = registry.getCustomPresets()
    setRegistry(new PresetRegistry(BUILT_IN_CAT_PRESETS, customs))
    await api?.presetsSaveCustom?.(customs)
    if (activePresetId === id) setActivePresetId(null)
  }, [registry, activePresetId])

  return (
    <div style={CS.root}>
      <div style={CS.section}>{i18nT('apps.mochi.color.presets')}</div>
      <div style={CS.presetGrid}>
        {registry.getAllPresets().map(p => (
          <PresetCard key={p.id} preset={p} active={activePresetId === p.id}
            svgContent={idleSvgContent} onClick={() => applyPreset(p)}
            onDelete={!p.builtIn ? () => handleDeletePreset(p.id) : undefined}
            deleteLabel={i18nT('apps.mochi.color.delete')} />
        ))}
      </div>

      <div style={CS.section}>{i18nT('apps.mochi.color.manual')}</div>
      <div style={CS.presetGrid}>
        <div style={{ padding: 6, borderRadius: 8, textAlign: 'center' as const, border: '2px solid var(--accent)', background: 'var(--accent-glow)' }}>
          <img src={Object.keys(colorMap).length > 0 ? toDataUri(applySvgColorMap(idleSvgContent, colorMap)) : toDataUri(idleSvgContent)}
            alt={i18nT('apps.mochi.color.final')} style={CS.presetThumb} draggable={false} />
          <div style={{ fontSize: 10, fontWeight: 600 }}>{i18nT('apps.mochi.color.current_effect')}</div>
        </div>
        {sourceColors.map(src => (
          <ColorEditorCard key={src} sourceColor={src} targetColor={colorMap[src] || src}
            bodyPart={BODY_PART_KEYS[src] ? i18nT(BODY_PART_KEYS[src]) : src}
            svgContent={idleSvgContent} allColors={sourceColors}
            onChange={color => handleColorChange(src, color)} />
        ))}
      </div>

      <div style={CS.btnRow}>
        <button style={CS.btn} onClick={handleReset}>{i18nT('apps.mochi.color.reset')}</button>
        {!showSaveForm ? (
          <button ref={saveTriggerRef} style={CS.btn} onClick={() => setShowSaveForm(true)}>{i18nT('apps.mochi.color.save_preset')}</button>
        ) : (
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <input
              type="text"
              value={saveNameInput}
              onChange={e => setSaveNameInput(e.target.value)}
              {...ime.bindEnter({ onEnter: handleSaveAsPreset, onEscape: closeSaveForm })}
              placeholder={i18nT('apps.mochi.color.prompt_name')}
              autoFocus
              style={{
                padding: '5px 10px', borderRadius: 6, border: '1px solid var(--border)',
                background: 'var(--bg-input)', color: 'var(--text)', fontSize: 12, width: 140,
                outline: 'none',
              }}
            />
            <button aria-label={i18nT('apps.mochi.color.save_preset')} style={{ ...CS.btn, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }} onClick={handleSaveAsPreset} disabled={!saveNameInput.trim()}><Check size={14} /></button>
            <button aria-label={i18nT('apps.mochi.editor.cancel')} style={{ ...CS.btn, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => { setSaveNameInput(''); closeSaveForm() }}><X size={14} /></button>
          </div>
        )}
      </div>
    </div>
  )
}
