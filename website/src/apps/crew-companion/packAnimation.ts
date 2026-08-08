/**
 * Reading a manifest's animation entry.
 *
 * A pack may name a slot either as a bare filename or as an object carrying the
 * content and its format. Both are legal and the desktop app accepted both, so rather
 * than casting at a dozen call sites the narrowing lives here once.
 */
import type { AnimationFormat } from './appearanceTypes'

export type AnimationEntry = string | { content?: string; format?: AnimationFormat }

/** The entry's content, or an empty string when it declares none. */
export function entryContent(entry: AnimationEntry | undefined): string {
  if (typeof entry === 'string') return entry
  return entry?.content ?? ''
}

/**
 * The entry's format.
 *
 * Falls back to inspecting the content: a bare filename carries no format, and Lottie
 * is JSON while everything else the editor writes is markup.
 */
export function entryFormat(entry: AnimationEntry | undefined): AnimationFormat {
  if (entry && typeof entry !== 'string' && entry.format) return entry.format
  const content = entryContent(entry).trimStart()
  return content.startsWith('{') ? 'lottie' : 'svg'
}
