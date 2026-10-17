/**
 * Mochi - Built-in Cat Color Presets
 *
 * 10 real-world cat breed color presets based on Default Mochi SVG colors.
 */
import { i18nT } from '../../../../i18n/t'
import type { CatPreset } from './catPresets'
import type { ColorMap } from './colorCustomizer'

/**
 * Default Mochi SVG source colors (the keys every preset maps FROM).
 * Extracted from assets/animations/mochi_idle.svg.
 * Each entry maps a hex color to its body part for prompt descriptions.
 */
export const DEFAULT_MOCHI_COLORS = [
  '#F9A85F', // body main
  '#F18D50', // darker orange (ears, shadow)
  '#EB8849', // orange accent (chin, legs)
  '#E98649', // orange accent (belly)
  '#FCD9B3', // light (tummy, paw pads)
  '#F49681', // pink (inner ear)
  '#F5E6CB', // pale cream (paws)
  '#522210', // dark brown (outlines, eyes, mouth)
  '#522214', // dark brown (body outline)
  '#391F19', // darkest brown (detail shadows)
] as const

/** Human-readable body part labels for each source color */
export const COLOR_BODY_PARTS: Record<string, string> = {
  '#F9A85F': 'body/fur',
  '#F18D50': 'ears/shadow',
  '#EB8849': 'chin/legs',
  '#E98649': 'belly',
  '#FCD9B3': 'tummy/paw pads',
  '#F49681': 'inner ear',
  '#F5E6CB': 'paws',
  '#522210': 'outlines/eyes/mouth',
  '#522214': 'body outline',
  '#391F19': 'detail shadows',
}

/**
 * Generate a human-readable appearance description from a preset or colorMap.
 * Returns something like: "Russian Blue cat — body/fur: steel blue (#8BA4B8), ears: darker blue (#7090A8), ..."
 */
export function describeAppearance(presetName: string | null, colorMap: Record<string, string>): string {
  if (!colorMap || Object.keys(colorMap).length === 0) {
    return i18nT('apps.mochi.preset.default_desc')
  }
  const parts: string[] = []
  // Only describe the most visually important parts
  const keyParts = ['#F9A85F', '#F18D50', '#FCD9B3', '#F49681', '#522210']
  for (const src of keyParts) {
    const dst = colorMap[src]
    if (dst && dst !== src) {
      parts.push(`${COLOR_BODY_PARTS[src]}: ${dst}`)
    }
  }
  const prefix = presetName ? i18nT('apps.mochi.preset.named_desc', { name: presetName }) : i18nT('apps.mochi.preset.custom_desc')
  return parts.length > 0 ? `${prefix} — ${parts.join(', ')}` : prefix
}

type SourceKey = typeof DEFAULT_MOCHI_COLORS[number]
type PresetMap = Record<SourceKey, string>

/**
 * Display-name key per builtin preset id — the single source for both the `name` field
 * below and the render site in `ColorCustomizer`.
 *
 * It is a map rather than a literal argument to `preset()` because `check-i18n-keys.mjs`
 * resolves only file-scope bindings: a key read off a `CatPreset` at render time is one
 * it cannot verify exists in the catalog, which would exempt that file from the check.
 * Indexing this map in place checks all ten.
 */
export const BUILT_IN_PRESET_NAME_KEY = {
  'orange-tabby': 'apps.mochi.preset.orange_tabby',
  tuxedo: 'apps.mochi.preset.tuxedo',
  calico: 'apps.mochi.preset.calico',
  'russian-blue': 'apps.mochi.preset.russian_blue',
  siamese: 'apps.mochi.preset.siamese',
  'british-shorthair': 'apps.mochi.preset.british_shorthair',
  white: 'apps.mochi.preset.white',
  black: 'apps.mochi.preset.black',
  tabby: 'apps.mochi.preset.tabby',
  ragdoll: 'apps.mochi.preset.ragdoll',
} as const

export type BuiltInPresetId = keyof typeof BUILT_IN_PRESET_NAME_KEY

function preset(
  id: BuiltInPresetId, description: string,
  colorMap: PresetMap, swatches: string[],
): CatPreset {
  return {
    id,
    name: BUILT_IN_PRESET_NAME_KEY[id],
    description,
    colorMap: colorMap as ColorMap,
    swatches,
    builtIn: true,
  }
}

/**
 * Display name for a preset card.
 *
 * The lookup lives HERE, beside the key map, and not at the render site: the
 * catalog-key gate resolves file-scope bindings within one file, so an imported map
 * indexed in a component is still unverifiable. Colocating the map with its one
 * `i18nT` call is what makes all ten keys checked. Custom presets carry a literal name
 * the user typed, and an unknown builtin id falls back to it rather than rendering
 * nothing.
 */
export function presetDisplayName(preset: CatPreset): string {
  const k = preset.id as BuiltInPresetId
  return preset.builtIn && BUILT_IN_PRESET_NAME_KEY[k]
    ? i18nT(BUILT_IN_PRESET_NAME_KEY[k])
    : preset.name
}

export const BUILT_IN_CAT_PRESETS: CatPreset[] = [
  preset('orange-tabby', '', {
    '#F9A85F': '#F9A85F', '#F18D50': '#F18D50', '#EB8849': '#EB8849',
    '#E98649': '#E98649', '#FCD9B3': '#FCD9B3', '#F49681': '#F49681',
    '#F5E6CB': '#F5E6CB', '#522210': '#522210', '#522214': '#522214',
    '#391F19': '#391F19',
  }, ['#F9A85F', '#F18D50', '#FCD9B3']),

  preset('tuxedo', '', {
    '#F9A85F': '#2C2C2C', '#F18D50': '#1A1A1A', '#EB8849': '#1A1A1A',
    '#E98649': '#1A1A1A', '#FCD9B3': '#F5F5F5', '#F49681': '#FFB6C1',
    '#F5E6CB': '#FFFFFF', '#522210': '#0D0D0D', '#522214': '#0D0D0D',
    '#391F19': '#000000',
  }, ['#2C2C2C', '#F5F5F5', '#FFFFFF']),

  preset('calico', '', {
    '#F9A85F': '#F5F0E8', '#F18D50': '#E8943A', '#EB8849': '#3D3D3D',
    '#E98649': '#E8943A', '#FCD9B3': '#FFFAF5', '#F49681': '#FFB6C1',
    '#F5E6CB': '#FFFAF5', '#522210': '#2B1810', '#522214': '#2B1810',
    '#391F19': '#1A0F0A',
  }, ['#F5F0E8', '#E8943A', '#3D3D3D']),

  preset('russian-blue', '', {
    '#F9A85F': '#8BA4B8', '#F18D50': '#7090A8', '#EB8849': '#607E96',
    '#E98649': '#7090A8', '#FCD9B3': '#C0D4E4', '#F49681': '#B8A0B0',
    '#F5E6CB': '#D0E0EC', '#522210': '#283848', '#522214': '#283848',
    '#391F19': '#182830',
  }, ['#8BA4B8', '#C0D4E4', '#283848']),

  preset('siamese', '', {
    '#F9A85F': '#F5E8D0', '#F18D50': '#6B4832', '#EB8849': '#4A3020',
    '#E98649': '#6B4832', '#FCD9B3': '#FFF5E8', '#F49681': '#D4A0A0',
    '#F5E6CB': '#FFF8F0', '#522210': '#2A1810', '#522214': '#2A1810',
    '#391F19': '#1A0E08',
  }, ['#F5E8D0', '#6B4832', '#2A1810']),

  preset('british-shorthair', '', {
    '#F9A85F': '#9898A8', '#F18D50': '#808090', '#EB8849': '#707080',
    '#E98649': '#808090', '#FCD9B3': '#C8C8D4', '#F49681': '#C0A8B0',
    '#F5E6CB': '#D4D4DE', '#522210': '#383840', '#522214': '#383840',
    '#391F19': '#282830',
  }, ['#9898A8', '#C8C8D4', '#383840']),

  preset('white', '', {
    '#F9A85F': '#F8F8F8', '#F18D50': '#EFEFEF', '#EB8849': '#E8E8E8',
    '#E98649': '#EFEFEF', '#FCD9B3': '#FFFFFF', '#F49681': '#FFD0D0',
    '#F5E6CB': '#FFFFFF', '#522210': '#8A7A7A', '#522214': '#8A7A7A',
    '#391F19': '#6B5B5B',
  }, ['#F8F8F8', '#FFFFFF', '#8A7A7A']),

  preset('black', '', {
    '#F9A85F': '#2A2A2A', '#F18D50': '#1E1E1E', '#EB8849': '#151515',
    '#E98649': '#1E1E1E', '#FCD9B3': '#3D3D3D', '#F49681': '#8B5A5A',
    '#F5E6CB': '#4A4A4A', '#522210': '#0A0A0A', '#522214': '#0A0A0A',
    '#391F19': '#000000',
  }, ['#2A2A2A', '#3D3D3D', '#0A0A0A']),

  preset('tabby', '', {
    '#F9A85F': '#8C6840', '#F18D50': '#785830', '#EB8849': '#684828',
    '#E98649': '#785830', '#FCD9B3': '#C8A878', '#F49681': '#C09080',
    '#F5E6CB': '#D4B890', '#522210': '#2E1808', '#522214': '#2E1808',
    '#391F19': '#1E1004',
  }, ['#8C6840', '#C8A878', '#2E1808']),

  preset('ragdoll', '', {
    '#F9A85F': '#EDE0D4', '#F18D50': '#A08068', '#EB8849': '#886850',
    '#E98649': '#A08068', '#FCD9B3': '#FFF0E0', '#F49681': '#E0A8B0',
    '#F5E6CB': '#FFF5EA', '#522210': '#4A3428', '#522214': '#4A3428',
    '#391F19': '#342018',
  }, ['#EDE0D4', '#A08068', '#4A3428']),
]
