/**
 * Split a translated sentence on a placeholder so a component can go in its place.
 *
 * Returns the literal pieces with `null` marking where the placeholder was.
 *
 * This exists because splitting prose across TWO keys is the wrong shape: the halves
 * each end mid-sentence, and a translator cannot reorder across keys — plenty of
 * languages need the embedded link somewhere other than where English puts it. One key
 * holding the whole sentence keeps it translatable and reorderable.
 */
export function splitOnPlaceholder(text: string, name: string): (string | null)[] {
  const token = `{{${name}}}`
  const at = text.indexOf(token)
  if (at === -1) return [text]
  return [text.slice(0, at), null, text.slice(at + token.length)].filter(
    (part) => part !== '',
  )
}
