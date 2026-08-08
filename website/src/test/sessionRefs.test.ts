import { describe, it, expect } from 'vitest'
import { buildShareableUrl } from '../utils/shareUrl'
import {
  MAX_SESSION_REFS,
  MAX_SESSION_REFS_RECOVERY,
  PRIVATE_MEMORY_MODES,
  addSessionRef,
  appendSessionRefLinks,
  formatSessionRefLink,
  isPrivateMemoryMode,
  removeSessionRef,
  mergeSessionRefs,
  sanitizeRefTitle,
  sanitizeSessionRefs,
  sessionRefBlockReason,
  sessionRefUrl,
  type SessionRef,
} from '../utils/sessionRefs'

const ref = (key: string, title = key, messages?: number): SessionRef => ({ key, title, messages })

describe('sessionRefUrl', () => {
  it('is byte-identical to what the session menu Copy link produces', () => {
    // The contract that matters: a dragged reference and a hand-copied link are
    // the same string, so nothing downstream has to recognise two dialects.
    for (const r of [
      ref('chat-7-1785440411', 'Release notes for 0.5.0'),
      ref('chat-9', ''),
      ref('chat-42', 'Tabs vs Spaces: The Reckoning'),
    ]) {
      expect(sessionRefUrl(r)).toBe(buildShareableUrl(r.key, r.title))
    }
  })

  it('carries the slot key in the sid parameter the dashboard reads', () => {
    const url = new URL(sessionRefUrl(ref('chat-7-1785440411', 'Release notes')))
    expect(url.searchParams.get('sid')).toBe('chat-7-1785440411')
    expect(url.pathname.startsWith('/chat')).toBe(true)
  })

  it('keeps a crafted key inside the sid parameter instead of injecting others', () => {
    const url = new URL(sessionRefUrl(ref('a&b=c', 'x')))
    expect(url.searchParams.get('sid')).toBe('a&b=c')
    expect([...url.searchParams.keys()]).toEqual(['sid'])
  })

  it('is absolute, so the link resolves outside the dashboard SPA', () => {
    expect(sessionRefUrl(ref('chat-1', 'One')).startsWith('http')).toBe(true)
  })
})

describe('sanitizeRefTitle', () => {
  it('flattens newlines so a title cannot span lines in the sent message', () => {
    expect(sanitizeRefTitle('line one\nline two')).toBe('line one line two')
    expect(sanitizeRefTitle('tabs\t and   runs')).toBe('tabs and runs')
  })

  it('strips brackets so a title cannot break out of the markdown link label', () => {
    expect(sanitizeRefTitle('we [broke] it]]')).toBe('we broke it')
  })

  it('truncates past the cap with an ellipsis', () => {
    const out = sanitizeRefTitle('x'.repeat(200))
    expect(out).toHaveLength(80)
    expect(out.endsWith('…')).toBe(true)
  })

  it('returns empty for whitespace-only or bracket-only titles', () => {
    expect(sanitizeRefTitle('   ')).toBe('')
    expect(sanitizeRefTitle('[[]]')).toBe('')
  })
})

describe('formatSessionRefLink', () => {
  it('emits a markdown link whose label is the title and target the share URL', () => {
    const r = ref('k1', 'Release notes')
    expect(formatSessionRefLink(r)).toBe(`[Release notes](${buildShareableUrl('k1', 'Release notes')})`)
  })

  it('falls back to the key when the title is empty or unusable', () => {
    expect(formatSessionRefLink(ref('k2', ''))).toBe(`[k2](${buildShareableUrl('k2', '')})`)
    expect(formatSessionRefLink(ref('k3', '[]'))).toBe(`[k3](${buildShareableUrl('k3', '[]')})`)
  })

  it('produces a link that parses as a single markdown link', () => {
    const out = formatSessionRefLink(ref('k4', 'a]b(c)d\ne'))
    expect(out.match(/\[/g)).toHaveLength(1)
    expect(out.match(/\]/g)).toHaveLength(1)
    expect(out.includes('\n')).toBe(false)
  })
})

describe('appendSessionRefLinks', () => {
  it('is a byte-identical no-op when nothing is staged', () => {
    expect(appendSessionRefLinks('hello', [])).toBe('hello')
    expect(appendSessionRefLinks('', [])).toBe('')
  })

  it('appends after a blank line, one link per line', () => {
    const a = ref('a', 'A')
    const b = ref('b', 'B')
    expect(appendSessionRefLinks('look at these', [a, b]))
      .toBe(`look at these\n\n${formatSessionRefLink(a)}\n${formatSessionRefLink(b)}`)
  })

  it('emits only the links when the user typed nothing', () => {
    const a = ref('a', 'A')
    expect(appendSessionRefLinks('', [a])).toBe(formatSessionRefLink(a))
  })

  it('carries a LINK and never the referenced content', () => {
    // The whole point of the v1 design: the outgoing text grows by a bounded
    // link, not by a transcript. Asserted as EXACT equality against the link
    // builder — `not.toContain('900')` could never fail, since the function
    // never reads `messages` at all.
    const r = ref('k', 'Some session', 900)
    expect(appendSessionRefLinks('hi', [r])).toBe(`hi\n\n${formatSessionRefLink(r)}`)
    expect(formatSessionRefLink(r)).not.toContain('900')
  })

  it('appends rather than splices, so earlier text is untouched', () => {
    const typed = 'before [ Paste #1 · 9 lines ] after'
    expect(appendSessionRefLinks(typed, [ref('a', 'A')]).startsWith(typed)).toBe(true)
  })

  it('would DUPLICATE a link if a ref were re-staged over already-linked text', () => {
    // Why the transport-failure restore path deliberately does NOT re-stage
    // chips: that path restores `txt`, which already carries the serialized
    // links. Re-staging the refs on top would make the retry append them again.
    // Asserted here so the reasoning is executable rather than a comment.
    const r = ref('a', 'A')
    const restoredText = appendSessionRefLinks('hi', [r])   // what the draft holds
    const onRetry = appendSessionRefLinks(restoredText, [r]) // if chips came back too
    expect(onRetry.match(/sid=a/g)).toHaveLength(2)
    // Whereas leaving the refs cleared keeps it at one.
    expect(appendSessionRefLinks(restoredText, []).match(/sid=a/g)).toHaveLength(1)
  })
})

describe('label sanitization cannot be bypassed via the key', () => {
  // A corrupt draft entry reaches formatSessionRefLink with an arbitrary key
  // (sanitizeSessionRefs only requires a non-empty string). If the fallback
  // label skipped sanitization, an unescaped `]` would close the markdown link
  // early and render the remainder as a second, ATTACKER-CHOSEN link.
  //
  // The property under test is therefore "the link target is always our own
  // origin, and only one link is emitted" — not "the crafted text disappears".
  // Parens are deliberately NOT stripped: legitimate titles contain them, and
  // with `]` gone the label cannot terminate early, so surviving parens are
  // display text inside a link that still points at the dashboard.
  const evil = 'x](https://evil.example/pwned)'
  /** The href of the single markdown link in `out`. */
  const targetOf = (out: string): string => {
    const m = out.match(/^\[.*\]\((.+)\)$/)
    expect(m, `expected exactly one markdown link, got: ${out}`).toBeTruthy()
    return m![1]
  }

  it('emits exactly one link, targeting our own origin, for a crafted key', () => {
    const out = formatSessionRefLink({ key: evil, title: '' })
    expect(out.match(/\]\(/g)).toHaveLength(1)
    const url = new URL(targetOf(out))
    expect(url.origin).toBe(window.location.origin)
    expect(url.pathname.startsWith('/chat')).toBe(true)
    expect(url.searchParams.get('sid')).toBe(evil)   // carried as encoded data
  })

  it('emits exactly one link, targeting our own origin, for a crafted title', () => {
    const out = formatSessionRefLink({ key: 'k', title: evil })
    expect(out.match(/\]\(/g)).toHaveLength(1)
    expect(new URL(targetOf(out)).origin).toBe(window.location.origin)
  })

  it('strips the bracket that would terminate the label early', () => {
    for (const title of ['', '   ', '[]', '[[]]']) {
      const out = formatSessionRefLink({ key: evil, title })
      const label = out.slice(1, out.lastIndexOf(']('))
      expect(label).not.toContain(']')
      expect(label).not.toContain('[')
    }
  })

  it('falls back to the bracket-free URL when key and title both sanitize empty', () => {
    const out = formatSessionRefLink({ key: ']]]', title: '[[[' })
    expect(out.match(/\]\(/g)).toHaveLength(1)
    expect(out.startsWith('[http')).toBe(true)
  })

  it('keeps a crafted ref to a single line when appended', () => {
    const out = appendSessionRefLinks('hi', [{ key: evil, title: evil }])
    expect(out.split('\n').filter(l => l.includes('](')).length).toBe(1)
  })
})

describe('addSessionRef / removeSessionRef', () => {
  it('stages a new ref', () => {
    expect(addSessionRef([], ref('a'))).toEqual([ref('a')])
  })

  it('returns the SAME array on a duplicate so no re-render is triggered', () => {
    const refs = [ref('a')]
    expect(addSessionRef(refs, ref('a', 'renamed'))).toBe(refs)
  })

  it('returns the SAME array past the cap', () => {
    const full = Array.from({ length: MAX_SESSION_REFS }, (_, i) => ref(`k${i}`))
    expect(addSessionRef(full, ref('extra'))).toBe(full)
    expect(full).toHaveLength(MAX_SESSION_REFS)
  })

  it('ignores an empty key', () => {
    const refs = [ref('a')]
    expect(addSessionRef(refs, ref(''))).toBe(refs)
  })

  it('removes by key and returns the same array when absent', () => {
    const refs = [ref('a'), ref('b')]
    expect(removeSessionRef(refs, 'a')).toEqual([ref('b')])
    expect(removeSessionRef(refs, 'nope')).toBe(refs)
  })
})

describe('privacy guard', () => {
  it('treats exactly the backend INCOGNITO_MEMORY_MODES as private', () => {
    // Parametrized on the constant itself, so adding a mode auto-covers here.
    for (const mode of PRIVATE_MEMORY_MODES) expect(isPrivateMemoryMode(mode)).toBe(true)
    expect(PRIVATE_MEMORY_MODES).toContain('incognito')
    expect(PRIVATE_MEMORY_MODES).toContain('temporary')
  })

  it('treats persistent / absent / unknown modes as referenceable', () => {
    expect(isPrivateMemoryMode('persistent')).toBe(false)
    expect(isPrivateMemoryMode(undefined)).toBe(false)
    expect(isPrivateMemoryMode(null)).toBe(false)
    expect(isPrivateMemoryMode('')).toBe(false)
  })

  it('refuses every private mode at the drop decision', () => {
    for (const mode of PRIVATE_MEMORY_MODES) {
      expect(sessionRefBlockReason({ key: 'a', activeSlot: 'b', memoryMode: mode })).toBe('private')
    }
  })

  it('refuses dropping the already-open session onto itself', () => {
    expect(sessionRefBlockReason({ key: 'a', activeSlot: 'a' })).toBe('self')
  })

  it('reports private ahead of self when both apply', () => {
    expect(sessionRefBlockReason({ key: 'a', activeSlot: 'a', memoryMode: 'incognito' })).toBe('private')
  })

  it('allows a normal session dropped onto a different chat', () => {
    expect(sessionRefBlockReason({ key: 'a', activeSlot: 'b', memoryMode: 'persistent' })).toBeNull()
    expect(sessionRefBlockReason({ key: 'a', activeSlot: null })).toBeNull()
    expect(sessionRefBlockReason({ key: 'a' })).toBeNull()
  })
})

describe('sanitizeSessionRefs (draft store contract)', () => {
  it('returns null for non-arrays and for empty results', () => {
    expect(sanitizeSessionRefs(null)).toBeNull()
    expect(sanitizeSessionRefs('nope')).toBeNull()
    expect(sanitizeSessionRefs({})).toBeNull()
    expect(sanitizeSessionRefs([])).toBeNull()
    expect(sanitizeSessionRefs([{ nope: 1 }, null, 7])).toBeNull()
  })

  it('keeps well-formed records and defaults a missing title to the key', () => {
    expect(sanitizeSessionRefs([{ key: 'a' }])).toEqual([{ key: 'a', title: 'a', messages: undefined }])
  })

  it('drops a non-numeric or negative message count rather than storing garbage', () => {
    expect(sanitizeSessionRefs([{ key: 'a', title: 'A', messages: 'lots' }])?.[0].messages).toBeUndefined()
    expect(sanitizeSessionRefs([{ key: 'a', title: 'A', messages: -3 }])?.[0].messages).toBeUndefined()
    expect(sanitizeSessionRefs([{ key: 'a', title: 'A', messages: NaN }])?.[0].messages).toBeUndefined()
    expect(sanitizeSessionRefs([{ key: 'a', title: 'A', messages: 12 }])?.[0].messages).toBe(12)
  })

  it('de-dupes by key, keeping the first occurrence', () => {
    const out = sanitizeSessionRefs([{ key: 'a', title: 'first' }, { key: 'a', title: 'second' }])
    expect(out).toHaveLength(1)
    expect(out?.[0].title).toBe('first')
  })

  it('enforces the cap on a corrupt oversized draft', () => {
    // Bounded by the RECOVERY ceiling, not the staging cap: the store must be able
    // to hold a set handed back by mergeSessionRefs after a failed send. Still a
    // hard bound, so a corrupt draft cannot grow without limit.
    const many = Array.from({ length: MAX_SESSION_REFS_RECOVERY + 20 }, (_, i) => ({ key: `k${i}` }))
    expect(sanitizeSessionRefs(many)).toHaveLength(MAX_SESSION_REFS_RECOVERY)
  })
})

describe('mergeSessionRefs (failure-restore rule, shared by both paths)', () => {
  it('returns the kept array untouched when nothing is coming back', () => {
    const keep = [ref('a', 'A')]
    expect(mergeSessionRefs(keep, [])).toBe(keep)
  })

  it('appends incoming refs after the kept ones', () => {
    expect(mergeSessionRefs([ref('a', 'A')], [ref('b', 'B')]).map(r => r.key)).toEqual(['a', 'b'])
  })

  it('lets the STAGED ref win a key collision, so in-flight staging is not clobbered', () => {
    const out = mergeSessionRefs([ref('a', 'staged since')], [ref('a', 'came back')])
    expect(out).toHaveLength(1)
    expect(out[0].title).toBe('staged since')
  })

  it('restores into an empty composer', () => {
    expect(mergeSessionRefs([], [ref('a', 'A')]).map(r => r.key)).toEqual(['a'])
  })

  it('preserves the whole recovery set — a failed send discards nothing', () => {
    // A recovery set is refs the user already staged that were never delivered.
    // Capping it at the STAGING bound silently dropped the originals when a full
    // set was sent and more were staged during the in-flight window. Asserted as
    // a round-trip so neither the merge nor the store can truncate it.
    const full = (p: string) => Array.from({ length: MAX_SESSION_REFS }, (_, i) => ref(`${p}${i}`))
    const merged = mergeSessionRefs(full('keep'), full('sent'))
    expect(merged).toHaveLength(MAX_SESSION_REFS * 2)
    expect(merged.length).toBeLessThanOrEqual(MAX_SESSION_REFS_RECOVERY)
    expect(sanitizeSessionRefs(merged)).toHaveLength(merged.length)
  })

  it('still bounds recovery, so a corrupt draft cannot grow without limit', () => {
    const many = Array.from({ length: MAX_SESSION_REFS_RECOVERY + 20 }, (_, i) => ({ key: `k${i}` }))
    expect(sanitizeSessionRefs(many)).toHaveLength(MAX_SESSION_REFS_RECOVERY)
    const huge = Array.from({ length: 200 }, (_, i) => ref(`s${i}`))
    expect(mergeSessionRefs([], huge)).toHaveLength(MAX_SESSION_REFS_RECOVERY)
  })

  it('staging stays capped at MAX_SESSION_REFS even though recovery may exceed it', () => {
    // The two bounds are deliberately different: nobody reaches the recovery
    // ceiling by adding, only by being given references back.
    let refs: ReturnType<typeof ref>[] = []
    for (let i = 0; i < MAX_SESSION_REFS_RECOVERY; i++) refs = addSessionRef(refs, ref(`a${i}`))
    expect(refs).toHaveLength(MAX_SESSION_REFS)
  })

  it('restores the whole set in the realistic case (nothing staged since)', () => {
    // A send fails within the 10s abort window, so `keep` is normally empty and
    // every reference comes back — the cap is a bound, not the common path.
    const full = Array.from({ length: MAX_SESSION_REFS }, (_, i) => ref(`sent${i}`))
    expect(mergeSessionRefs([], full)).toHaveLength(MAX_SESSION_REFS)
  })
})
