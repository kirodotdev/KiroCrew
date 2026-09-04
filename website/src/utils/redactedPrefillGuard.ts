/**
 * Don't-echo-what-you-didn't-edit for forms prefilled from a redacted GET.
 *
 * `GET /api/agents` scrubs credential / exfiltration-URL shapes out of record
 * strings before serving them (#8447) and marks WHICH fields it changed via
 * each row's `redacted_fields` manifest. A form prefilled from such a row
 * holds `[REDACTED: ...]` markers in those fields, and a save that sends
 * every form field back would ask the server to store the marker over the
 * real value.
 *
 * The backend refuses obvious marker echoes, but a server-side comparison
 * necessarily runs against the value stored at SAVE time — after a
 * concurrent write it cannot tell a stale marker echo from a deliberate
 * edit. Only the client knows a field was never touched, so the client
 * omitting it is what closes that race.
 *
 * The omission is keyed on a SNAPSHOT taken when the form opened, never on
 * the live roster row: a roster refetch mid-edit (WebSocket refresh, another
 * save's query invalidation) can swap in a row whose manifest no longer
 * lists a field the FORM still holds a stale marker for — keying on the live
 * row would then let that stale marker through and overwrite the newer
 * stored value.
 */

/** What the form was OPENED with: the manifest and the marker prefills. */
export interface RedactedPrefillSnapshot {
  redacted_fields: string[]
  prefills: Record<string, unknown>
}

/**
 * Capture, at form-open time, which fields arrived redacted and the exact
 * prefill each held. Copies the values out of the row, so a later mutation
 * or replacement of the roster row cannot rewrite what the form remembers.
 */
export function snapshotRedactedPrefills(row: {
  redacted_fields?: string[]
}): RedactedPrefillSnapshot {
  const fields = [...(row.redacted_fields || [])]
  const source = row as Record<string, unknown>
  const prefills: Record<string, unknown> = {}
  for (const field of fields) prefills[field] = source[field]
  return { redacted_fields: fields, prefills }
}

/**
 * Return `body` without fields that are UNTOUCHED redacted prefills.
 *
 * A field is omitted only when the OPENING snapshot marked it redacted AND
 * the form value still equals the prefill the form opened with — the user
 * never edited it, so nothing should be written. Any field the user
 * actually changed, including clearing it to `''`, is kept.
 */
export function omitUntouchedRedactedPrefills<T extends Record<string, unknown>>(
  body: T,
  snapshot: RedactedPrefillSnapshot,
): Partial<T> {
  const redacted = new Set(snapshot.redacted_fields)
  if (redacted.size === 0) return body
  const out: Partial<T> = {}
  for (const [key, value] of Object.entries(body)) {
    if (redacted.has(key) && value === snapshot.prefills[key]) continue
    ;(out as Record<string, unknown>)[key] = value
  }
  return out
}
