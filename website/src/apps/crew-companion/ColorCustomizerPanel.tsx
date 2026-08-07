/**
 * CrewCompanion - Color Customizer Panel
 *
 * Preset picker grid (each preset shows an SVG preview) + manual color editor
 * with per-body-part highlighting. i18n via useT().
 */
import { Check, X } from 'lucide-react'
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { extractSvgColors, applySvgColorMap, type ColorMap } from './colorCustomizer'
import { PresetRegistry, extractSwatches, type CatPreset } from './catPresets'
import { BUILT_IN_CAT_PRESETS } from './builtInCatPresets'
import { toDataUri } from './animationResolver'
import { i18nT } from '../../i18n/t'
import { petBridge } from './petBridge'

/**
 * The built-in pack's id, canonical in the backend (appearances.py `DEFAULT_PACK`).
 * This panel previously used a stale `DEFAULT_PACK` literal — the same bug
 * GalleryPanel fixed: recolors saved under an id no pack has, so the renderer
 * (which reloads by `kiro-ghost`) never saw them and the customization
 * silently vanished on the next reload.
 */
const DEFAULT_PACK = 'kiro-ghost'


const api = petBridge

/** i18n key for each source color's body part label */
const BODY_PART_KEYS: Record<string, string> = {
  '#F9A85F': 'apps.crewCompanion.color.bodyFur',
  '#F18D50': 'apps.crewCompanion.color.earsShadow',
  '#EB8849': 'apps.crewCompanion.color.chinLegs',
  '#E98649': 'apps.crewCompanion.color.belly',
  '#FCD9B3': 'apps.crewCompanion.color.tummyPaws',
  '#F49681': 'apps.crewCompanion.color.innerEar',
  '#F5E6CB': 'apps.crewCompanion.color.paws',
  '#522210': 'apps.crewCompanion.color.outlines',
  '#522214': 'apps.crewCompanion.color.bodyOutline',
  '#391F19': 'apps.crewCompanion.color.shadows',
}

const CS = {
  root: { marginTop: 16 },
  section: { fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase' as const, letterSpacing: 1, marginBottom: 8, marginTop: 12 },
  presetGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(90px, 1fr))', gap: 8, marginBottom: 12 },
  presetCard: (active: boolean) => ({
    padding: 6, borderRadius: 8, cursor: 'pointer', textAlign: 'center' as const,
    border: active ? '2px solid var(--accent)' : '1px solid var(--border)',
    background: active ? 'var(--accent-glow)' : 'var(--cc-input-bg)',
    transition: 'border-color 150ms',
  }),
  presetThumb: { width: 56, height: 56, objectFit: 'contain' as const, borderRadius: 6, margin: '0 auto 4px' },
  presetName: { fontSize: 10, fontWeight: 500, lineHeight: 1.3 },
  btnRow: { display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' as const },
  btn: { padding: '6px 14px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--cc-input-bg)', color: 'var(--text)', cursor: 'pointer', fontSize: 12 },
  deleteBtn: { background: 'none', border: 'none', color: 'var(--danger)', cursor: 'pointer', fontSize: 10, marginTop: 2 },
} as const

function PresetCard({ preset, active, svgContent, onClick, onDelete, deleteLabel, i18nT }: {
  preset: CatPreset; active: boolean; svgContent: string
  onClick: () => void; onDelete?: () => void; deleteLabel: string
  i18nT: (key: any) => string
}) {
  const previewUri = useMemo(() => {
    const cm = preset.colorMap
    if (!cm || Object.keys(cm).length === 0) return toDataUri(svgContent)
    return toDataUri(applySvgColorMap(svgContent, cm))
  }, [preset.colorMap, svgContent])

  // Built-in presets store i18n key in name; custom presets store literal name
  const displayName = preset.builtIn ? i18nT(preset.name as any) : preset.name

  return (
    <div role="button" tabIndex={0} aria-pressed={active} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }} style={CS.presetCard(active)} onClick={onClick}>
      <img src={previewUri} alt={displayName} style={CS.presetThumb} draggable={false} />
      <div style={CS.presetName}>{displayName}</div>
      {onDelete && (
        <button style={CS.deleteBtn} onClick={(e) => { e.stopPropagation(); onDelete() }}>{deleteLabel}</button>
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

  return (
    <div style={{ padding: 6, borderRadius: 8, textAlign: 'center' as const, border: '1px solid var(--border)', background: 'var(--cc-input-bg)' }}>
      <img src={highlightUri} alt={bodyPart} style={CS.presetThumb} draggable={false} />
      <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 4 }}>{bodyPart}</div>
      <input type="color" value={targetColor} onChange={e => onChange(e.target.value)}
        style={{ width: 36, height: 20, border: 'none', padding: 0, cursor: 'pointer', borderRadius: 4 }} />
    </div>
  )
}

interface Props { idleSvgContent: string; }

export const ColorCustomizerPanel: React.FC<Props> = ({ idleSvgContent }) => {
  const [colorMap, setColorMap] = useState<ColorMap>({})
  const [registry, setRegistry] = useState<PresetRegistry>(() => new PresetRegistry(BUILT_IN_CAT_PRESETS))
  const [activePresetId, setActivePresetId] = useState<string | null>(null)
  const [showSaveForm, setShowSaveForm] = useState(false)
  const [saveNameInput, setSaveNameInput] = useState('')
  const sourceColors = useMemo(() => extractSvgColors(idleSvgContent), [idleSvgContent])

  useEffect(() => {
    (async () => {
      const [saved, customs] = await Promise.all([
        api?.presetsGetColorMap?.(DEFAULT_PACK),
        api?.presetsLoadCustom?.(),
      ])
      if (saved && Object.keys(saved).length > 0) setColorMap(saved)
      // Stored presets are user data, so narrow rather than assume the shape.
      const saved2 = (customs ?? []).filter(
        (c): c is CatPreset => typeof c === 'object' && c !== null && 'name' in c,
      )
      if (saved2.length) setRegistry(new PresetRegistry(BUILT_IN_CAT_PRESETS, saved2))
    })()
  }, [])

  const applyPreset = useCallback((preset: CatPreset) => {
    setColorMap(preset.colorMap)
    setActivePresetId(preset.id)
    api?.gallerySetColorMap?.(DEFAULT_PACK, preset.colorMap)
  }, [])

  const handleColorChange = useCallback((sourceColor: string, targetColor: string) => {
    setColorMap(prev => {
      const next = { ...prev, [sourceColor]: targetColor }
      api?.gallerySetColorMap?.(DEFAULT_PACK, next)
      return next
    })
    setActivePresetId(null)
  }, [])

  const handleReset = useCallback(async () => {
    setColorMap({})
    setActivePresetId(null)
    await api?.gallerySetColorMap?.(DEFAULT_PACK, {})
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
    setShowSaveForm(false)
  }, [colorMap, registry, saveNameInput])

  const handleDeletePreset = useCallback(async (id: string) => {
    registry.removeCustomPreset(id)
    const customs = registry.getCustomPresets()
    setRegistry(new PresetRegistry(BUILT_IN_CAT_PRESETS, customs))
    await api?.presetsSaveCustom?.(customs)
    if (activePresetId === id) setActivePresetId(null)
  }, [registry, activePresetId])

  return (
    <div style={CS.root}>
      <div style={CS.section}>{i18nT('apps.crewCompanion.color.presets')}</div>
      <div style={CS.presetGrid}>
        {registry.getAllPresets().map(p => (
          <PresetCard key={p.id} preset={p} active={activePresetId === p.id}
            svgContent={idleSvgContent} onClick={() => applyPreset(p)}
            onDelete={!p.builtIn ? () => handleDeletePreset(p.id) : undefined}
            deleteLabel={i18nT('apps.crewCompanion.color.delete')} i18nT={i18nT} />
        ))}
      </div>

      <div style={CS.section}>{i18nT('apps.crewCompanion.color.manual')}</div>
      <div style={CS.presetGrid}>
        <div style={{ padding: 6, borderRadius: 8, textAlign: 'center' as const, border: '2px solid var(--accent)', background: 'var(--accent-glow)' }}>
          <img src={Object.keys(colorMap).length > 0 ? toDataUri(applySvgColorMap(idleSvgContent, colorMap)) : toDataUri(idleSvgContent)}
            alt="final" style={CS.presetThumb} draggable={false} />
          <div style={{ fontSize: 10, fontWeight: 600 }}>{i18nT('apps.crewCompanion.color.currentEffect')}</div>
        </div>
        {sourceColors.map(src => (
          <ColorEditorCard key={src} sourceColor={src} targetColor={colorMap[src] || src}
            bodyPart={BODY_PART_KEYS[src] ? i18nT(BODY_PART_KEYS[src]) : src}
            svgContent={idleSvgContent} allColors={sourceColors}
            onChange={color => handleColorChange(src, color)} />
        ))}
      </div>

      <div style={CS.btnRow}>
        <button style={CS.btn} onClick={handleReset}>{i18nT('apps.crewCompanion.color.reset')}</button>
        {!showSaveForm ? (
          <button style={CS.btn} onClick={() => setShowSaveForm(true)}>{i18nT('apps.crewCompanion.color.savePreset')}</button>
        ) : (
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <input
              type="text"
              value={saveNameInput}
              onChange={e => setSaveNameInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleSaveAsPreset(); if (e.key === 'Escape') setShowSaveForm(false) }}
              placeholder={i18nT('apps.crewCompanion.color.promptName')}
              autoFocus
              style={{
                padding: '5px 10px', borderRadius: 6, border: '1px solid var(--border)',
                background: 'var(--cc-input-bg)', color: 'var(--text)', fontSize: 12, width: 140,
                outline: 'none',
              }}
            />
            <button style={CS.btn} onClick={handleSaveAsPreset} disabled={!saveNameInput.trim()}
              aria-label={i18nT('apps.crewCompanion.color.savePreset')}>
              <Check className="lucide-inline" aria-hidden="true" />
            </button>
            <button style={CS.btn} onClick={() => { setShowSaveForm(false); setSaveNameInput('') }}
              aria-label={i18nT('apps.crewCompanion.gallery.cancel')}>
              <X className="lucide-inline" aria-hidden="true" />
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
