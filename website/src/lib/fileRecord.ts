import type { FileData } from '../components/FileCard'

/**
 * Parse a `file` transcript row's content: the file_send record as JSON.
 *
 * Returns the record only when it names a file, because FileCard builds its
 * outbox URL from `filename`. Optional `content_type` and `description` must be
 * strings and `size` a number when present; anything else is rejected so
 * malformed metadata never reaches the card. Every surface that draws a file
 * row (the SDK default and ChatPage's override) decides with this one function,
 * and each keeps its own fallback for a `null` answer.
 */
export function parseFileRecord(content: string): FileData | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(content)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object') return null
  const { filename, content_type, description, size } = parsed as Record<string, unknown>
  if (typeof filename !== 'string' || !filename) return null
  if (content_type !== undefined && typeof content_type !== 'string') return null
  if (description !== undefined && typeof description !== 'string') return null
  if (size !== undefined && typeof size !== 'number') return null
  return { filename, content_type, description, size }
}
