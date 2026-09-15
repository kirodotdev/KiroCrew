/**
 * Mochi - Animation Resolver
 *
 * Resolves the correct animation source for a given PetState and PetMood
 * from the active appearance pack. Handles mood priority (mood animations
 * take precedence over state animations when available) and caches results.
 */

import type { PetState, PetMood } from '../shared/types'
import type { AnimationFormat, AnimationSource, PackManifest } from '../shared/appearanceTypes'
import { applySvgColorMap, type ColorMap } from '../shared/colorCustomizer'

/**
 * Converts raw SVG content to a data URI suitable for <img> src.
 * Strips any XML declaration before encoding.
 */
export function toDataUri(raw: string): string {
  return `data:image/svg+xml,${encodeURIComponent(raw.replace(/<\?xml[^?]*\?>\s*/, ''))}`
}

/**
 * Determines the animation format from a filename's extension.
 */
function formatFromFilename(filename: string): AnimationFormat {
  if (filename.endsWith('.json')) return 'lottie'
  if (filename.endsWith('.png') || filename.endsWith('.webp')) return 'sprite'
  return 'svg'
}

/**
 * Resolves animation sources from an appearance pack based on pet state and mood.
 *
 * Resolution logic:
 * 1. Build cache key `${packId}:${state}:${mood}`, return cached if hit
 * 2. If mood !== 'neutral' AND the pack's moods has that mood → use mood animation
 * 3. Otherwise → use states[state] animation
 * 4. Determine format from file extension: .svg → SVG, .json → Lottie
 * 5. For SVG: convert raw content to data URI via toDataUri()
 * 6. For Lottie: return the JSON string as-is
 * 7. Store in cache and return
 */
export class AnimationResolver {
  private packId: string
  private manifest: PackManifest
  private contentMap: Record<string, string>
  private cache: Map<string, AnimationSource> = new Map()
  private colorMap: ColorMap | null = null

  constructor(manifest: PackManifest, contentMap: Record<string, string>) {
    this.packId = manifest.meta.id
    this.manifest = manifest
    this.contentMap = contentMap
  }

  /** Set or clear the color map. Clears cache so next resolve() picks up new colors. */
  setColorMap(colorMap: ColorMap | null): void {
    this.colorMap = colorMap
    this.cache.clear()
  }

  /**
   * Resolves the animation source for the given state and mood.
   * Mood animations take priority over state animations when available.
   */
  hasState(state: string): boolean {
    return !!(this.manifest.states as unknown as Record<string, string | undefined>)[state]
  }
  resolve(state: PetState, mood: PetMood): AnimationSource {
    const cacheKey = `${this.packId}:${state}:${mood}`
    const cached = this.cache.get(cacheKey)
    if (cached) return cached

    // Determine which animation file to use
    let filename: string | undefined

    if (mood !== 'neutral') {
      const moodKey = mood as keyof typeof this.manifest.moods
      const moodFile = this.manifest.moods[moodKey]
      if (moodFile) {
        filename = moodFile
      } else {
        filename = this.manifest.states[state]
      }
    } else {
      filename = this.manifest.states[state]
      // Fallback for optional states
      if (!filename && (state as string) === 'peeking') filename = this.manifest.states['idle']
      if (!filename && (state as string) === 'peekThinking') filename = this.manifest.states['thinking']
    }

    // The universal fallback. Every caller above could already leave `filename`
    // undefined for a pack that does not assign that slot — which the STORE
    // explicitly allows (`REQUIRED_STATES = ("idle",)`; one drawing is a complete
    // pack). Without this the next line called `.endsWith` on undefined and threw
    // out of the render, so the only way to ship such a pack safely was to reject
    // it up front — which is what made a one-drawing pack fall back to the cat.
    if (!filename) filename = this.manifest.states['idle']
    if (!filename) filename = ''

    // Determine format and build the animation source
    const format = formatFromFilename(filename)
    const rawContent = this.contentMap[filename] ?? ''
    let uri: string
    if (format === 'svg') {
      const processed = this.colorMap ? applySvgColorMap(rawContent, this.colorMap) : rawContent
      uri = toDataUri(processed)
    } else if (format === 'sprite') uri = rawContent.startsWith('data:') ? rawContent : `data:image/png;base64,${rawContent}`
    else uri = rawContent

    const source: AnimationSource = { uri, format }
    this.cache.set(cacheKey, source)
    return source
  }

  /**
   * Switches to a new appearance pack and clears the cache.
   */
  setPack(manifest: PackManifest, contentMap: Record<string, string>): void {
    this.packId = manifest.meta.id
    this.manifest = manifest
    this.contentMap = contentMap
    this.cache.clear()
  }
}
