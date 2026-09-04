/**
 * The in-flight artifact-write registry, and the transport hook that feeds it.
 *
 * The hook is the point of the design, so it is tested at the transport rather
 * than only as a unit: the previous approach asked every write path on the artifact
 * page to announce itself, and four consecutive review rounds each found one that
 * did not — letting a just-created document be deleted while its own request was
 * still in the air. What these assert is that a request cannot be issued WITHOUT
 * being counted, which is the property that makes the next new write path safe
 * without anyone remembering to opt in.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  beginArtifactWrite,
  endArtifactWrite,
  hasPendingArtifactWrite,
  __resetArtifactWrites,
} from '../lib/artifactWrites'
import { api } from '../api/client'

describe('the in-flight artifact write registry', () => {
  beforeEach(() => __resetArtifactWrites())

  it('reports nothing pending for an untouched artifact', () => {
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
  })

  it('reports a write while it is outstanding, and not after', () => {
    beginArtifactWrite('untitled')
    expect(hasPendingArtifactWrite('untitled')).toBe(true)
    endArtifactWrite('untitled')
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
  })

  it('counts overlapping writes so the first to return does not clear the second', () => {
    // A tag edit and a rename can both be outstanding. A boolean would let the
    // faster one report "all clear" while the other is still on the wire.
    beginArtifactWrite('untitled')
    beginArtifactWrite('untitled')
    endArtifactWrite('untitled')
    expect(hasPendingArtifactWrite('untitled')).toBe(true)
    endArtifactWrite('untitled')
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
  })

  it('keeps artifacts independent', () => {
    beginArtifactWrite('untitled')
    expect(hasPendingArtifactWrite('other-doc')).toBe(false)
  })

  it('never goes negative on an unbalanced end', () => {
    endArtifactWrite('untitled')
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
    beginArtifactWrite('untitled')
    expect(hasPendingArtifactWrite('untitled')).toBe(true)
  })
})

describe('the API transport feeding the registry', () => {
  let release: (v: Response) => void
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    __resetArtifactWrites()
    globalThis.fetch = vi.fn(
      () => new Promise<Response>((res) => { release = res }),
    ) as unknown as typeof fetch
  })
  afterEach(() => { globalThis.fetch = originalFetch })

  const ok = () =>
    ({ ok: true, status: 200, json: async () => ({}) }) as unknown as Response

  it('counts a write issued through a handler that never announced itself', async () => {
    // The folder move lives in a child component with no access to any page-level
    // flag, which is precisely why it was the reported defect. Nothing about it
    // opts in here.
    const p = api.setArtifactFolder('untitled', 'folder-1')
    expect(hasPendingArtifactWrite('untitled')).toBe(true)
    release(ok())
    await p
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
  })

  it('counts a content save the same way', async () => {
    const p = api.updateArtifact('untitled', { content: '# body' })
    expect(hasPendingArtifactWrite('untitled')).toBe(true)
    release(ok())
    await p
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
  })

  it('counts a comment post, which touches a sub-path', async () => {
    const p = api.postArtifactComment('untitled', { text: 'hm' })
    expect(hasPendingArtifactWrite('untitled')).toBe(true)
    release(ok())
    await p
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
  })

  it('clears when the request FAILS', async () => {
    // The server never applied it, so the record it re-reads is authoritative
    // either way; leaving the guard set would strand the document forever.
    const p = api.updateArtifact('untitled', { content: '# body' }).catch(() => undefined)
    expect(hasPendingArtifactWrite('untitled')).toBe(true)
    release({ ok: false, status: 500, json: async () => ({}) } as unknown as Response)
    await p
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
  })

  it('does NOT count the settle call itself', async () => {
    // Settle IS the cleanup. Counting it would have it guard against itself and
    // never delete anything.
    const p = api.settleBlankArtifact('untitled', {
      untitled_name: 'Untitled',
      draft: '',
      allow_delete: true,
    })
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
    release(ok())
    await p
  })

  it('does not count a read', async () => {
    const p = api.artifact('untitled')
    expect(hasPendingArtifactWrite('untitled')).toBe(false)
    release(ok())
    await p
  })

  it('does not count a write to an unrelated resource', async () => {
    const p = api.createChatSlot('a')
    expect(hasPendingArtifactWrite('a')).toBe(false)
    release(ok())
    await p
  })

  it('decodes the slug, so an encoded path still guards the right document', async () => {
    const p = api.updateArtifact('my doc', { content: '# body' })
    expect(hasPendingArtifactWrite('my doc')).toBe(true)
    release(ok())
    await p
  })
})
