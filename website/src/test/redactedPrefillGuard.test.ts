/**
 * The crew edit sheet's save must not echo untouched redacted prefills.
 *
 * `GET /api/agents` redacts record strings and marks the changed fields in
 * `redacted_fields` (#8447). The sheet sends every form field on save, so
 * without this guard an untouched `[REDACTED: ...]` prefill would be written
 * over the real stored value — and after a concurrent edit, over a NEWER
 * value the server-side echo guard can no longer recognise (the stale-save
 * race). Omission is keyed on the manifest SNAPSHOTTED at sheet open, never
 * on the live row or on marker text: a mid-edit roster refresh can swap in a
 * clean manifest while the form still holds the stale marker (GPT round-4
 * finding on #8465), and a user deliberately saving marker-looking text
 * into a clean field must still write.
 */
import { describe, expect, it } from 'vitest'

import {
  omitUntouchedRedactedPrefills,
  snapshotRedactedPrefills,
} from '../utils/redactedPrefillGuard'

const MARKER = '[REDACTED: credential]'

describe('omitUntouchedRedactedPrefills', () => {
  it('omits a redacted field whose form value is the untouched prefill', () => {
    const snap = snapshotRedactedPrefills({
      workspace: MARKER,
      triggers: 'benign',
      redacted_fields: ['workspace'],
    })
    const body = { workspace: MARKER, triggers: 'benign' }

    expect(omitUntouchedRedactedPrefills(body, snap)).toEqual({ triggers: 'benign' })
  })

  it('keeps a redacted field the user actually edited', () => {
    const snap = snapshotRedactedPrefills({ workspace: MARKER, redacted_fields: ['workspace'] })

    expect(omitUntouchedRedactedPrefills({ workspace: 'new-value' }, snap)).toEqual({
      workspace: 'new-value',
    })
  })

  it('keeps a redacted field the user cleared', () => {
    const snap = snapshotRedactedPrefills({ workspace: MARKER, redacted_fields: ['workspace'] })

    expect(omitUntouchedRedactedPrefills({ workspace: '' }, snap)).toEqual({ workspace: '' })
  })

  it('keeps a non-redacted field even when its value looks like a marker', () => {
    // The manifest, not the marker text, is the authority: a user pasting
    // marker-looking text into a clean field is a deliberate edit.
    const snap = snapshotRedactedPrefills({ description: 'benign', redacted_fields: [] })

    expect(omitUntouchedRedactedPrefills({ description: MARKER }, snap)).toEqual({
      description: MARKER,
    })
  })

  it('returns the body unchanged when the row has no manifest (older payloads)', () => {
    const snap = snapshotRedactedPrefills({ workspace: MARKER } as { redacted_fields?: string[] })
    const body = { workspace: MARKER, triggers: 't' }

    expect(omitUntouchedRedactedPrefills(body, snap)).toEqual(body)
  })

  it('still omits the untouched prefill after a mid-edit roster refresh', () => {
    // The stale-manifest race (GPT round 4): the sheet opens on a row whose
    // triggers is redacted; while the user edits ANOTHER field, a concurrent
    // save + roster refresh replaces (or rewrites) the row with a benign
    // value and an EMPTY manifest. The form still holds the stale marker.
    // Keyed on the live row the marker would pass through; keyed on the
    // opening snapshot it is still recognised as an untouched prefill.
    const rowAtOpen: { triggers: string; workspace: string; redacted_fields: string[] } = {
      triggers: MARKER,
      workspace: 'w1',
      redacted_fields: ['triggers'],
    }
    const snap = snapshotRedactedPrefills(rowAtOpen)

    // The live row moves on — BOTH by replacement and by in-place rewrite;
    // the snapshot must have copied what it needs and not care.
    rowAtOpen.triggers = 'newer benign'
    rowAtOpen.redacted_fields = []

    const saved = omitUntouchedRedactedPrefills(
      { triggers: MARKER, workspace: 'w2-edited' },
      snap,
    )
    expect(saved).toEqual({ workspace: 'w2-edited' })
  })
})
