/**
 * CrewCompanion - Cat Color Preset System
 *
 * Data model, validation, and registry for cat color presets.
 * Pure logic — no Electron or DOM dependencies.
 */
import { type ColorMap, isValidHexColor } from './colorCustomizer'

export interface CatPreset {
  id: string
  name: string
  description: string
  colorMap: ColorMap
  swatches: string[]   // 2–5 representative hex colors
  builtIn: boolean
}

export function validatePreset(preset: CatPreset): string[] {
  const errors: string[] = []
  if (!preset.id) errors.push('id is required')
  if (!preset.name) errors.push('name is required')
  for (const [k, v] of Object.entries(preset.colorMap)) {
    if (!isValidHexColor(k)) errors.push(`invalid colorMap key: ${k}`)
    if (!isValidHexColor(v)) errors.push(`invalid colorMap value: ${v}`)
  }
  if (preset.swatches.length < 2 || preset.swatches.length > 5) {
    errors.push(`swatches length must be 2-5, got ${preset.swatches.length}`)
  }
  for (const s of preset.swatches) {
    if (!isValidHexColor(s)) errors.push(`invalid swatch: ${s}`)
  }
  return errors
}

export function extractSwatches(colorMap: ColorMap): string[] {
  const vals = Object.values(colorMap).filter(isValidHexColor)
  // Deduplicate while preserving order
  const seen = new Set<string>()
  const unique: string[] = []
  for (const v of vals) {
    if (!seen.has(v)) { seen.add(v); unique.push(v) }
  }
  return unique.slice(0, 5)
}

export function generatePresetId(): string {
  return `custom-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export class PresetRegistry {
  private builtInPresets: CatPreset[]
  private customPresets: CatPreset[]

  constructor(builtInPresets: CatPreset[], customPresets?: CatPreset[]) {
    this.builtInPresets = builtInPresets
    // Filter out invalid entries for resilience
    this.customPresets = (customPresets ?? []).filter(
      (p) => p && typeof p === 'object' && typeof p.id === 'string' && p.id,
    )
  }

  getAllPresets(): CatPreset[] {
    return [...this.builtInPresets, ...this.customPresets]
  }

  getPresetById(id: string): CatPreset | null {
    return this.getAllPresets().find((p) => p.id === id) ?? null
  }

  getBuiltInPresets(): CatPreset[] {
    return [...this.builtInPresets]
  }

  getCustomPresets(): CatPreset[] {
    return [...this.customPresets]
  }

  addCustomPreset(preset: Omit<CatPreset, 'id' | 'builtIn'>): string {
    const id = generatePresetId()
    this.customPresets.push({ ...preset, id, builtIn: false })
    return id
  }

  removeCustomPreset(id: string): boolean {
    // Refuse to delete built-in presets
    if (this.builtInPresets.some((p) => p.id === id)) return false
    const idx = this.customPresets.findIndex((p) => p.id === id)
    if (idx === -1) return false
    this.customPresets.splice(idx, 1)
    return true
  }
}
