import { describe, it, expect } from 'vitest'

import { canonicalChatHref, sessionKeyFrom, sessionKeyFromChatHref } from './sessionKeys'
import { sessionRefUrl } from './sessionRefs'
import { buildShareableUrl } from './shareUrl'

describe('sessionKeyFrom', () => {
  it('reads a bare slot key', () => {
    expect(sessionKeyFrom('chat-24-1784661951')).toBe('chat-24-1784661951')
  })

  it('strips the dashboard_ prefix of the resumed spelling', () => {
    // History files are `dashboard_chat-<n>-<ts>.jsonl`, so this spelling must
    // resolve to the same slot key as the bare one.
    expect(sessionKeyFrom('dashboard_chat-24-1784661951')).toBe('chat-24-1784661951')
  })

  it('strips the dashboard: prefix the gateway itself mints', () => {
    // The colon spelling is the history key the gateway tags chat-launched runs
    // with; a second grammar here accepted only the underscore form.
    expect(sessionKeyFrom('dashboard:chat-24-1784661951')).toBe('chat-24-1784661951')
  })

  it('trims surrounding whitespace', () => {
    expect(sessionKeyFrom('  chat-7-1700000000\n')).toBe('chat-7-1700000000')
  })

  it.each([
    ['chat-24', 'no timestamp'],
    ['chat--1784661951', 'no slot number'],
    ['chat-24-', 'empty timestamp'],
    ['chat-abc-1784661951', 'non-numeric slot'],
    ['chat-24-17846x1951', 'non-numeric timestamp'],
    ['session-24-1784661951', 'wrong prefix'],
    ['', 'empty string'],
  ])('refuses %s (%s)', (raw) => {
    expect(sessionKeyFrom(raw)).toBeNull()
  })

  it('refuses a key that is only PART of the span', () => {
    // The regex is anchored at both ends on purpose. A loose match would chip a
    // span whose visible text says one thing while the target says another.
    expect(sessionKeyFrom('see chat-24-1784661951 for details')).toBeNull()
    expect(sessionKeyFrom('chat-24-1784661951.jsonl')).toBeNull()
    expect(sessionKeyFrom('xchat-24-1784661951')).toBeNull()
  })
})

describe('sessionKeyFromChatHref', () => {
  it('reads the canonical deep link', () => {
    expect(sessionKeyFromChatHref('/chat?sid=chat-24-1784661951')).toBe('chat-24-1784661951')
  })

  it('reads the legacy ?slot= alias', () => {
    // ChatPage itself still honours `?slot=`, so refusing it here would chip only
    // half the links that actually work.
    expect(sessionKeyFromChatHref('/chat?slot=chat-9-1700000000')).toBe('chat-9-1700000000')
  })

  it('tolerates the cosmetic title slug', () => {
    expect(sessionKeyFromChatHref('/chat/fix-the-pagination-bug?sid=chat-24-1784661951'))
      .toBe('chat-24-1784661951')
  })

  it('survives extra query parameters in any order', () => {
    expect(sessionKeyFromChatHref('/chat?tab=activity&sid=chat-3-1699999999')).toBe('chat-3-1699999999')
  })

  it('accepts the dashboard_ spelling inside the query', () => {
    expect(sessionKeyFromChatHref('/chat?sid=dashboard_chat-24-1784661951')).toBe('chat-24-1784661951')
  })

  it('refuses an absolute URL even when its path and query would match', () => {
    // A chip promises to stay inside THIS dashboard. Honouring a foreign origin
    // would retarget that promise without changing how the chip looks.
    expect(sessionKeyFromChatHref('https://elsewhere.example/chat?sid=chat-24-1784661951')).toBeNull()
    expect(sessionKeyFromChatHref('http://localhost:5476/chat?sid=chat-24-1784661951')).toBeNull()
  })

  it('refuses a protocol-relative href, which reads local but resolves foreign', () => {
    expect(sessionKeyFromChatHref('//elsewhere.example/chat?sid=chat-24-1784661951')).toBeNull()
  })

  it('refuses the BACKSLASH sibling of a protocol-relative href', () => {
    // WHATWG reads `\` as `/` for a special scheme, so this resolves to a foreign
    // origin while looking root-relative. Refusing only `//` left it open.
    expect(new URL('/\\evil.example/chat', 'http://localhost').origin).toBe('http://evil.example')
    expect(sessionKeyFromChatHref('/\\evil.example/chat?sid=chat-24-1784661951')).toBeNull()
  })

  it('refuses the percent-encoded backslash, raw and decoded', () => {
    // `MdAnchor` decodes before calling this, so `%5C` arrives as that backslash.
    // Both spellings are pinned so neither entry path can regress alone.
    expect(decodeURIComponent('/%5Cevil.example/chat')).toBe('/\\evil.example/chat')
    expect(sessionKeyFromChatHref('/%5Cevil.example/chat?sid=chat-24-1784661951')).toBeNull()
    expect(sessionKeyFromChatHref(decodeURIComponent('/%5Cevil.example/chat?sid=chat-24-1784661951'))).toBeNull()
  })

  it.each([
    ['/chat', 'no key at all'],
    ['/chat?sid=', 'empty key'],
    ['/chat?sid=nonsense', 'key fails the grammar'],
    ['/chats?sid=chat-24-1784661951', 'sibling path, not /chat'],
    ['/artifacts/foo?sid=chat-24-1784661951', 'a different route carrying sid'],
    ['/chatter?sid=chat-24-1784661951', 'prefix collision on the path'],
  ])('refuses %s (%s)', (href) => {
    expect(sessionKeyFromChatHref(href)).toBeNull()
  })

  it('refuses a bare key that is not a link', () => {
    // The two readers are deliberately separate: a key in prose is not an href,
    // and treating one as the other is how a chip ends up with no target.
    expect(sessionKeyFromChatHref('chat-24-1784661951')).toBeNull()
  })

  it('reads the absolute URL the app itself mints for Copy-link', () => {
    // The real producer, not a hand-copied literal: it emits an absolute
    // `${origin}/chat?sid=…`, the shape a leading-slash test refused outright.
    const href = buildShareableUrl('chat-24-1784661951')
    expect(href).toBe(`${window.location.origin}/chat?sid=chat-24-1784661951`)
    expect(sessionKeyFromChatHref(href)).toBe('chat-24-1784661951')
  })

  it('reads the absolute URL a staged session ref carries', () => {
    // Same producer behind a second entry point, so a ref echoed into a
    // transcript resolves in place instead of opening a new tab.
    const href = sessionRefUrl({ key: 'chat-9-1700000000', title: 'Fix the bug' })
    expect(href).toBe(`${window.location.origin}/chat/fix-the-bug?sid=chat-9-1700000000`)
    expect(sessionKeyFromChatHref(href)).toBe('chat-9-1700000000')
  })

  it('refuses the SAME path and query on a different host', () => {
    // Accepting our own absolute form must not widen the promise: swapping only
    // the host has to keep refusing.
    const foreign = `${window.location.origin}/chat?sid=chat-24-1784661951`
      .replace(window.location.origin, 'https://elsewhere.example')
    expect(foreign).toBe('https://elsewhere.example/chat?sid=chat-24-1784661951')
    expect(sessionKeyFromChatHref(foreign)).toBeNull()
  })

  it('refuses our own host on a different port', () => {
    // The port is part of an origin, so a sibling dev server is foreign even
    // though the hostname matches.
    expect(sessionKeyFromChatHref('http://localhost:1/chat?sid=chat-24-1784661951')).toBeNull()
  })

  it('refuses a link that targets a MESSAGE via ?msg=', () => {
    // ChatPage reads `msg` once at mount, so switching in place drops the target;
    // left unintercepted, the plain anchor mounts fresh and honours it.
    expect(sessionKeyFromChatHref('/chat?sid=chat-24-1784661951&msg=2026-08-30T12:00:00Z')).toBeNull()
  })

  it('refuses a link that targets a MESSAGE via ?mid=', () => {
    // `mid` is the stable per-message id the producer prefers over `msg`; both name
    // a place inside the session, so both must fall through.
    expect(sessionKeyFromChatHref('/chat?sid=chat-24-1784661951&mid=abc123')).toBeNull()
  })

  it('refuses a message-targeted ABSOLUTE share link too', () => {
    // Copy-link mints the absolute form, and it carries `msg` when copied from a
    // specific message, so the same-origin path must refuse it as well.
    const href = buildShareableUrl('chat-24-1784661951', undefined, '2026-08-30T12:00:00Z')
    expect(href).toContain('msg=')
    expect(sessionKeyFromChatHref(href)).toBeNull()
  })

  it('still resolves a link whose extra parameter is NOT a message target', () => {
    // The refusal must be scoped to message targeting, not to any extra parameter.
    expect(sessionKeyFromChatHref('/chat?sid=chat-24-1784661951&tab=activity'))
      .toBe('chat-24-1784661951')
  })
})

describe('canonicalChatHref', () => {
  it('rewrites a prefixed sid to the canonical key', () => {
    // A modified click is handed to the browser, so the attribute has to name a key
    // `?sid=` can resolve — `dashboard_…` is not one.
    expect(canonicalChatHref('/chat?sid=dashboard_chat-24-1784661951', 'chat-24-1784661951'))
      .toBe('/chat?sid=chat-24-1784661951')
  })

  it('collapses the legacy ?slot= alias into ?sid=', () => {
    expect(canonicalChatHref('/chat?slot=chat-9-1700000000', 'chat-9-1700000000'))
      .toBe('/chat?sid=chat-9-1700000000')
  })

  it('preserves the path and any unrelated query parameters', () => {
    // Only the session parameter is normalised; rewriting the whole href would
    // silently drop intent the author expressed.
    expect(canonicalChatHref('/chat/fix-the-bug?tab=activity&sid=dashboard_chat-3-1699999999', 'chat-3-1699999999'))
      .toBe('/chat/fix-the-bug?tab=activity&sid=chat-3-1699999999')
  })

  it('adds sid when the href carried none', () => {
    expect(canonicalChatHref('/chat', 'chat-1-1700000001')).toBe('/chat?sid=chat-1-1700000001')
  })
})
