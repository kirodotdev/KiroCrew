/**
 * CrewCompanion - Built-in Cat Color Presets
 *
 * 10 real-world cat breed color presets based on Default Kiro SVG colors.
 */
import type { CatPreset } from './catPresets'
import type { ColorMap } from './colorCustomizer'

/**
 * Default Kiro SVG source colors (the keys every preset maps FROM).
 * Extracted from assets/animations/kiro_idle.svg.
 * Each entry maps a hex color to its body part for prompt descriptions.
 */
export const DEFAULT_CREW_COMPANION_COLORS = [
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



type SourceKey = typeof DEFAULT_CREW_COMPANION_COLORS[number]
type PresetMap = Record<SourceKey, string>

function preset(
  id: string, name: string, description: string,
  colorMap: PresetMap, swatches: string[],
): CatPreset {
  return { id, name, description, colorMap: colorMap as ColorMap, swatches, builtIn: true }
}

export const BUILT_IN_CAT_PRESETS: CatPreset[] = [
  preset('orange-tabby', 'preset.orangeTabby', '', {
    '#F9A85F': '#F9A85F', '#F18D50': '#F18D50', '#EB8849': '#EB8849',
    '#E98649': '#E98649', '#FCD9B3': '#FCD9B3', '#F49681': '#F49681',
    '#F5E6CB': '#F5E6CB', '#522210': '#522210', '#522214': '#522214',
    '#391F19': '#391F19',
  }, ['#F9A85F', '#F18D50', '#FCD9B3']),

  preset('tuxedo', 'preset.tuxedo', '', {
    '#F9A85F': '#2C2C2C', '#F18D50': '#1A1A1A', '#EB8849': '#1A1A1A',
    '#E98649': '#1A1A1A', '#FCD9B3': '#F5F5F5', '#F49681': '#FFB6C1',
    '#F5E6CB': '#FFFFFF', '#522210': '#0D0D0D', '#522214': '#0D0D0D',
    '#391F19': '#000000',
  }, ['#2C2C2C', '#F5F5F5', '#FFFFFF']),

  preset('calico', 'preset.calico', '', {
    '#F9A85F': '#F5F0E8', '#F18D50': '#E8943A', '#EB8849': '#3D3D3D',
    '#E98649': '#E8943A', '#FCD9B3': '#FFFAF5', '#F49681': '#FFB6C1',
    '#F5E6CB': '#FFFAF5', '#522210': '#2B1810', '#522214': '#2B1810',
    '#391F19': '#1A0F0A',
  }, ['#F5F0E8', '#E8943A', '#3D3D3D']),

  preset('russian-blue', 'preset.russianBlue', '', {
    '#F9A85F': '#8BA4B8', '#F18D50': '#7090A8', '#EB8849': '#607E96',
    '#E98649': '#7090A8', '#FCD9B3': '#C0D4E4', '#F49681': '#B8A0B0',
    '#F5E6CB': '#D0E0EC', '#522210': '#283848', '#522214': '#283848',
    '#391F19': '#182830',
  }, ['#8BA4B8', '#C0D4E4', '#283848']),

  preset('siamese', 'preset.siamese', '', {
    '#F9A85F': '#F5E8D0', '#F18D50': '#6B4832', '#EB8849': '#4A3020',
    '#E98649': '#6B4832', '#FCD9B3': '#FFF5E8', '#F49681': '#D4A0A0',
    '#F5E6CB': '#FFF8F0', '#522210': '#2A1810', '#522214': '#2A1810',
    '#391F19': '#1A0E08',
  }, ['#F5E8D0', '#6B4832', '#2A1810']),

  preset('british-shorthair', 'preset.britishShorthair', '', {
    '#F9A85F': '#9898A8', '#F18D50': '#808090', '#EB8849': '#707080',
    '#E98649': '#808090', '#FCD9B3': '#C8C8D4', '#F49681': '#C0A8B0',
    '#F5E6CB': '#D4D4DE', '#522210': '#383840', '#522214': '#383840',
    '#391F19': '#282830',
  }, ['#9898A8', '#C8C8D4', '#383840']),

  preset('white', 'preset.white', '', {
    '#F9A85F': '#F8F8F8', '#F18D50': '#EFEFEF', '#EB8849': '#E8E8E8',
    '#E98649': '#EFEFEF', '#FCD9B3': '#FFFFFF', '#F49681': '#FFD0D0',
    '#F5E6CB': '#FFFFFF', '#522210': '#8A7A7A', '#522214': '#8A7A7A',
    '#391F19': '#6B5B5B',
  }, ['#F8F8F8', '#FFFFFF', '#8A7A7A']),

  preset('black', 'preset.black', '', {
    '#F9A85F': '#2A2A2A', '#F18D50': '#1E1E1E', '#EB8849': '#151515',
    '#E98649': '#1E1E1E', '#FCD9B3': '#3D3D3D', '#F49681': '#8B5A5A',
    '#F5E6CB': '#4A4A4A', '#522210': '#0A0A0A', '#522214': '#0A0A0A',
    '#391F19': '#000000',
  }, ['#2A2A2A', '#3D3D3D', '#0A0A0A']),

  preset('tabby', 'preset.tabby', '', {
    '#F9A85F': '#8C6840', '#F18D50': '#785830', '#EB8849': '#684828',
    '#E98649': '#785830', '#FCD9B3': '#C8A878', '#F49681': '#C09080',
    '#F5E6CB': '#D4B890', '#522210': '#2E1808', '#522214': '#2E1808',
    '#391F19': '#1E1004',
  }, ['#8C6840', '#C8A878', '#2E1808']),

  preset('ragdoll', 'preset.ragdoll', '', {
    '#F9A85F': '#EDE0D4', '#F18D50': '#A08068', '#EB8849': '#886850',
    '#E98649': '#A08068', '#FCD9B3': '#FFF0E0', '#F49681': '#E0A8B0',
    '#F5E6CB': '#FFF5EA', '#522210': '#4A3428', '#522214': '#4A3428',
    '#391F19': '#342018',
  }, ['#EDE0D4', '#A08068', '#4A3428']),
]
