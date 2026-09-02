/**
 * CrewCompanion - Animation Resolver
 *
 * Resolves the correct animation source for a given PetState and PetMood
 * from the active appearance pack. Handles mood priority (mood animations
 * take precedence over state animations when available) and caches results.
 */

import type { PetState, PetMood } from './types'
import type { AnimationFormat, AnimationSource, PackManifest, StateAnimationMap } from './appearanceTypes'
import { applySvgColorMap, type ColorMap } from './colorCustomizer'

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
    return !!this.manifest.states[state as keyof StateAnimationMap]
  }
  /** Resolve a state to a file, falling back through a chain to idle when the
   *  pack doesn't provide that state. working→thinking→idle merges the two
   *  "busy" states; everything ultimately falls back to idle. */
  private stateFile(state: string): string {
    const s = this.manifest.states as unknown as Record<string, string | undefined>
    if (s[state]) return s[state] as string
    const chains: Record<string, string[]> = {
      walking: ['idle'],
      // done/loading are the new status slots; thinking and working are what older
      // packs called "busy", so they resolve to loading before giving up on idle.
      done: ['idle'],
      loading: ['thinking', 'working', 'idle'],
      thinking: ['loading', 'idle'],
      working: ['loading', 'thinking', 'idle'],
      error: ['idle'],
      offline: ['idle'],
      peeking: ['idle'],
      peekThinking: ['thinking', 'idle'],
    }
    for (const fb of chains[state] || ['idle']) {
      if (s[fb]) return s[fb] as string
    }
    return s['idle'] || ''
  }
  resolve(state: PetState, mood: PetMood): AnimationSource {
    const cacheKey = `${this.packId}:${state}:${mood}`
    const cached = this.cache.get(cacheKey)
    if (cached) return cached

    // Determine which animation file to use
    let filename: string

    if (mood !== 'neutral') {
      const moodKey = mood as keyof typeof this.manifest.moods
      const moodFile = this.manifest.moods[moodKey]
      filename = moodFile || this.stateFile(state)
    } else {
      filename = this.stateFile(state)
    }

    // Determine format and build the animation source
    const source = this.sourceFor(filename)
    this.cache.set(cacheKey, source)
    return source
  }

  /** Build an AnimationSource from a pack filename (format inferred + colorized). */
  private sourceFor(filename: string): AnimationSource {
    const format = formatFromFilename(filename)
    const rawContent = this.contentMap[filename] ?? ''
    let uri: string
    if (format === 'svg') {
      const processed = this.colorMap ? applySvgColorMap(rawContent, this.colorMap) : rawContent
      uri = toDataUri(processed)
    } else if (format === 'sprite') uri = rawContent.startsWith('data:') ? rawContent : `data:image/png;base64,${rawContent}`
    else uri = rawContent
    return { uri, format }
  }

  /** Names of the open-ended "random" clips this pack provides. */
  randomNames(): string[] {
    return this.manifest.random ? Object.keys(this.manifest.random) : []
  }

  /** Whether the pack provides art for a given mood. */
  hasMood(mood: string): boolean {
    return !!(this.manifest.moods as Record<string, string | undefined>)?.[mood]
  }

  /** Resolve a named random clip to a renderable source (null if absent). */
  resolveRandom(name: string): AnimationSource | null {
    const file = this.manifest.random?.[name]
    if (!file) return null
    return this.sourceFor(file)
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
