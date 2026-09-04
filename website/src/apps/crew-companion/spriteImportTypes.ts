/**
 * What the sprite importer hands back when the user confirms.
 *
 * Shared so the importer and the gallery agree on the payload; it was previously
 * described as `any` on both sides, which meant a rename on one side would not have
 * been caught on the other.
 */
export interface SpriteImportResult {
  name: string
  author: string
  description: string
  frameWidth: number
  frameHeight: number
  fps: number
  flipX: boolean
  offsetY: number
  /** The sheet as a data URL, absent when the user reused an installed pack's art. */
  sourceImage?: string
  /** Slot to per-state strip, base64 PNG. */
  assignments: Record<string, string>
  /** Optional random-behaviour strips, same shape. */
  randomAssignments?: Record<string, string>
  /** Slot to row index, kept so the sheet can be re-edited later. */
  rowAssignments: Record<string, number>
  /** Set when saving over an existing pack instead of creating one. */
  overwriteId?: string
}
