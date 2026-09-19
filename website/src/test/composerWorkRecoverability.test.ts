import { describe, it, expect } from 'vitest'

import { WORK_IS_RECOVERABLE, hasUnsentComposerWork, hasComposerTextOrFiles } from '../utils/composerWork'
import type { ComposerWork } from '../utils/composerWork'

const EMPTY: ComposerWork = {
  text: '',
  files: [],
  dirs: [],
  sessionRefs: [],
  pasteBlocks: [],
  knowledge: false,
  uploading: false,
  voiceCapture: false,
}

describe('every unsent-work category declares a recoverability tier', () => {
  it('covers exactly the ComposerWork field set, with no category missing', () => {
    // Typed `Record<keyof ComposerWork, boolean>`, so a new category fails to COMPILE until
    // its tier is stated — asserted at runtime too, since tsconfig.app.json excludes src/test.
    expect(Object.keys(WORK_IS_RECOVERABLE).sort()).toEqual(Object.keys(EMPTY).sort())
    for (const key of Object.keys(EMPTY) as (keyof ComposerWork)[]) {
      expect(typeof WORK_IS_RECOVERABLE[key]).toBe('boolean')
    }
  })

  it('marks only the storage-backed categories recoverable', () => {
    // A misclassification drops real work to the short TTL, which is the loss this exists
    // to prevent, so the tiers are pinned individually rather than counted.
    expect(WORK_IS_RECOVERABLE.text).toBe(true)
    expect(WORK_IS_RECOVERABLE.dirs).toBe(true)
    const inMemoryOnly = ['files', 'sessionRefs', 'pasteBlocks', 'knowledge', 'uploading', 'voiceCapture'] as const
    for (const key of inMemoryOnly) {
      expect(WORK_IS_RECOVERABLE[key]).toBe(false)
    }
  })
})

describe('the one definition of "the composer holds unsent work"', () => {
  it('reads an empty composer as holding nothing', () => {
    expect(hasUnsentComposerWork(EMPTY)).toBe(false)
    expect(hasComposerTextOrFiles(EMPTY)).toBe(false)
  })

  it('does not count whitespace-only text as work', () => {
    expect(hasUnsentComposerWork({ ...EMPTY, text: '   ' })).toBe(false)
  })

  it('counts each traceless category, which a text-derived predicate misses', () => {
    expect(hasUnsentComposerWork({ ...EMPTY, knowledge: true })).toBe(true)
    expect(hasUnsentComposerWork({ ...EMPTY, uploading: true })).toBe(true)
    expect(hasUnsentComposerWork({ ...EMPTY, voiceCapture: true })).toBe(true)
  })

  it('counts each staged collection', () => {
    expect(hasUnsentComposerWork({ ...EMPTY, files: ['a'] })).toBe(true)
    expect(hasUnsentComposerWork({ ...EMPTY, dirs: ['a'] })).toBe(true)
    expect(hasUnsentComposerWork({ ...EMPTY, sessionRefs: ['a'] })).toBe(true)
    expect(hasUnsentComposerWork({ ...EMPTY, pasteBlocks: ['a'] })).toBe(true)
  })

  it('counts real text through both predicates', () => {
    expect(hasUnsentComposerWork({ ...EMPTY, text: 'hi' })).toBe(true)
    expect(hasComposerTextOrFiles({ text: 'hi', files: [] })).toBe(true)
    expect(hasComposerTextOrFiles({ text: '', files: ['a'] })).toBe(true)
  })
})
