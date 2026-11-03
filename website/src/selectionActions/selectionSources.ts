/**
 * Typed selection sources: the only places an app selection action can read from.
 *
 * An app item never reads the document's selection. A host surface that offers
 * app items owns a source adapter here, which turns the text that surface's own
 * selection toolbar already snapshotted into a `SelectedText`, bounded by the
 * item. A surface with no adapter offers no app items, and a new adapter is a new
 * `sources` value that widens every item's grant.
 *
 * - `chat`: text selected inside one chat reply, as `SelectionToolbar` read it from
 *   that reply's own content element (`AssistantMessage`).
 *
 * @module selectionActions/selectionSources
 */

export type SelectionSourceKind = 'chat'

export interface SelectedText {
  source: SelectionSourceKind
  /** At most the item's `maxLength` UTF-16 units. */
  text: string
  truncated: boolean
}

/** The bounded selection an item is handed, or null when the item does not apply to it. */
export function selectedTextFor(
  source: SelectionSourceKind,
  raw: string,
  item: { sources: ReadonlySet<SelectionSourceKind>; minLength: number; maxLength: number },
): SelectedText | null {
  if (!item.sources.has(source)) return null
  const text = raw.trim()
  if (text.length < item.minLength) return null
  if (text.length <= item.maxLength) return { source, text, truncated: false }
  // Never cut a surrogate pair in half.
  let end = item.maxLength
  const code = text.charCodeAt(end - 1)
  if (code >= 0xd800 && code <= 0xdbff) end -= 1
  return { source, text: text.slice(0, end), truncated: true }
}
