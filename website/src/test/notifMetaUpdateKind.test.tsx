/**
 * The `update` notification kind has its own metadata.
 *
 * Without an entry in `KIND_META` a note falls back to `DEFAULT_META`, which
 * renders a plain bell and the label "Notification". That is not a crash and no
 * other test notices it, so this pins the entry: the kind carries both the
 * gateway's own "a new Kiro Crew is out" notices and an installed app's "a new
 * version is published" ones, and neither should read as an unlabelled note.
 */
import { describe, it, expect } from 'vitest'
import { KIND_META, DEFAULT_META } from '../components/notifications/notifMeta'

describe('KIND_META.update', () => {
  it('exists, so an update note is not an unlabelled one', () => {
    expect(KIND_META.update).toBeDefined()
  })

  it('does not fall back to the default metadata', () => {
    expect(KIND_META.update.label).not.toBe(DEFAULT_META.label)
    expect(KIND_META.update.icon).not.toBe(DEFAULT_META.icon)
  })

  it('carries a translated label', () => {
    const label = KIND_META.update.label
    expect(typeof label).toBe('string')
    expect(label.length).toBeGreaterThan(0)
    // A key the catalog does not hold renders as the key path itself.
    expect(label).not.toContain('notifMeta')
  })

  it('is tinted as informational, not as a warning', () => {
    // Nothing is blocked by an available update: whatever is running keeps
    // working on the version it has.
    expect(KIND_META.update.color).toContain('info')
    expect(KIND_META.update.borderColor).toContain('info')
  })

  it('reads as a generic update, naming neither the gateway nor an app', () => {
    // One kind, two producers. A label naming either one would be wrong half
    // the time it is shown.
    const label = KIND_META.update.label.toLowerCase()
    expect(label).not.toContain('app')
    expect(label).not.toContain('kiro')
  })
})
