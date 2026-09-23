import { afterEach, describe, expect, it } from 'vitest'
import { composerDraftStoreFor } from './composerDraftStore'

describe('composerDraftStoreFor', () => {
  afterEach(() => { window.sessionStorage.clear() })

  it('keeps one draft per passage and clears only the one asked for', () => {
    const store = composerDraftStoreFor('mc-artifact-composer-draft:doc')
    store.write('about A', 'alpha', 0)
    store.write('about B', 'beta', 6)
    expect(store.read('alpha', 0)).toBe('about A')
    expect(store.read('beta', 6)).toBe('about B')
    // Same text elsewhere in the document is another passage.
    expect(store.read('alpha', 40)).toBeNull()
    store.clear('alpha', 0)
    expect(store.read('alpha', 0)).toBeNull()
    expect(store.read('beta', 6)).toBe('about B')
  })

  it('a second store for the same key sees the draft — the panel that wrote it is gone after a slot switch', () => {
    composerDraftStoreFor('mc-artifact-composer-draft:doc').write('half a thought', 'gamma', 12)
    expect(composerDraftStoreFor('mc-artifact-composer-draft:doc').read('gamma', 12)).toBe('half a thought')
    expect(composerDraftStoreFor('mc-artifact-composer-draft:other').read('gamma', 12)).toBeNull()
  })

  it('survives a refusing sessionStorage through the in-memory twin, and never throws', () => {
    const original = window.sessionStorage.setItem
    Object.defineProperty(window.sessionStorage, 'setItem', { configurable: true, value: () => { throw new Error('QuotaExceeded') } })
    try {
      const store = composerDraftStoreFor('mc-artifact-composer-draft:full')
      expect(() => store.write('kept', 'delta', 3)).not.toThrow()
      expect(store.read('delta', 3)).toBe('kept')
    } finally {
      Object.defineProperty(window.sessionStorage, 'setItem', { configurable: true, value: original })
    }
  })

  it('ignores a corrupt record instead of throwing', () => {
    window.sessionStorage.setItem('mc-artifact-composer-draft:bad', '{not json')
    expect(composerDraftStoreFor('mc-artifact-composer-draft:bad').read('x', 0)).toBeNull()
  })
})
