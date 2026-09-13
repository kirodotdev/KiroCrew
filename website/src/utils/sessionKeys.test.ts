import { describe, it, expect } from 'vitest'

import { canonicalChatHref, chatHrefSid, isSessionShortName, namesASession, sessionKeyFrom, sessionKeyFromChatHref, sessionKeyFromShort } from './sessionKeys'
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

describe('sessionKeyFromShort', () => {
  const ROSTER = ['chat-1380-1789049480', 'chat-138-1700000000', 'chat-7-1699999999']
  // Later than every fixture slot's mint, so these cases exercise the grammar and
  // the roster lookup rather than the mint guard.
  const WRITTEN = 1789049999

  it('resolves a short name to the one open slot that answers to it', () => {
    expect(sessionKeyFromShort('chat-1380', ROSTER, WRITTEN)).toBe('chat-1380-1789049480')
  })

  it('does not let a shorter number claim a longer one', () => {
    // The separator is part of the prefix. Without it `chat-138` also matches
    // `chat-1380-…`, which is a different conversation.
    expect(sessionKeyFromShort('chat-138', ROSTER, WRITTEN)).toBe('chat-138-1700000000')
  })

  it('strips the prefixed spellings, like the full grammar', () => {
    expect(sessionKeyFromShort('dashboard_chat-7', ROSTER, WRITTEN)).toBe('chat-7-1699999999')
    expect(sessionKeyFromShort('dashboard:chat-7', ROSTER, WRITTEN)).toBe('chat-7-1699999999')
  })

  it('trims surrounding whitespace', () => {
    expect(sessionKeyFromShort('  chat-7\n', ROSTER, WRITTEN)).toBe('chat-7-1699999999')
  })

  it('refuses a name no open session answers to', () => {
    // The roster is the sole authority: an unknown nickname stays plain text for
    // the same reason an unknown full key does.
    expect(sessionKeyFromShort('chat-999', ROSTER, WRITTEN)).toBeNull()
  })

  it('refuses an AMBIGUOUS name rather than guessing', () => {
    // A slot number is reused across gateway generations, so two open slots can
    // share one number. Either choice would open a session the reader did not name.
    expect(sessionKeyFromShort('chat-1380', ['chat-1380-1789049480', 'chat-1380-1700000000'], WRITTEN))
      .toBeNull()
  })

  it('refuses an empty roster', () => {
    expect(sessionKeyFromShort('chat-1380', [], WRITTEN)).toBeNull()
  })

  it('ignores a roster entry that is not a slot key', () => {
    // The roster also carries channel and cron keys; only a `chat-<n>-<ts>` slot
    // can be switched to.
    expect(sessionKeyFromShort('chat-1380', ['slack:1789049480.001', 'cron_f353f9f6'], WRITTEN)).toBeNull()
  })

  it('ignores an entry that carries the prefix but not the key SHAPE', () => {
    // Reaches the shape test, which the two entries above never do — they are
    // rejected on the prefix. A transcript filename and a non-numeric tail both
    // start with `chat-1380-` and neither names a switchable slot.
    expect(sessionKeyFromShort('chat-1380', ['chat-1380-1789049480.jsonl'], WRITTEN)).toBeNull()
    expect(sessionKeyFromShort('chat-1380', ['chat-1380-abc'], WRITTEN)).toBeNull()
    // And a malformed sibling must not shadow the real one.
    expect(sessionKeyFromShort('chat-1380', ['chat-1380-abc', 'chat-1380-1789049480'], WRITTEN))
      .toBe('chat-1380-1789049480')
  })

  it.each([
    ['chat-1380-1789049480', 'a FULL key is not a short name'],
    ['chat-', 'no slot number'],
    ['chat-abc', 'non-numeric slot'],
    ['session-1380', 'wrong prefix'],
    ['xchat-1380', 'prefix collision'],
    ['see chat-1380 for details', 'only part of the span'],
    ['', 'empty string'],
  ])('refuses %s (%s)', (raw) => {
    expect(sessionKeyFromShort(raw, ROSTER, WRITTEN)).toBeNull()
  })

  it('accepts a Map keySet, which is the roster the renderer holds', () => {
    // `SessionActions.sessions` is a key→title Map; this is the real call shape.
    const sessions = new Map([['chat-1380-1789049480', 'PR 9223 container runtime']])
    expect(sessionKeyFromShort('chat-1380', sessions.keys(), WRITTEN)).toBe('chat-1380-1789049480')
  })

  describe('the mint-time guard, for a collision across TIME', () => {
    // The ambiguity check only sees a collision while BOTH generations are open.
    // Once the older slot closes, one match remains and it is the wrong one.
    const LATER = ['chat-1380-1789049480']       // minted at 1789049480
    const WRITTEN_BEFORE = 1789000000            // ... by a message written earlier

    it('refuses a slot minted AFTER the text naming it was written', () => {
      expect(sessionKeyFromShort('chat-1380', LATER, WRITTEN_BEFORE)).toBeNull()
    })

    it('accepts a slot minted before, or in the same second as, the text', () => {
      expect(sessionKeyFromShort('chat-1380', LATER, 1789049481)).toBe('chat-1380-1789049480')
      expect(sessionKeyFromShort('chat-1380', LATER, 1789049480)).toBe('chat-1380-1789049480')
    })

    it('picks the generation the message could actually have meant', () => {
      // Both open, same slot number: ambiguous with no time, decided with it.
      const both = ['chat-1380-1700000000', 'chat-1380-1789049480']
      expect(sessionKeyFromShort('chat-1380', both)).toBeNull()
      expect(sessionKeyFromShort('chat-1380', both, 1789000000)).toBe('chat-1380-1700000000')
    })

    it('refuses every short name when the caller does not know the time', () => {
      // Fail closed. Without a write time there is nothing separating "the session
      // this named" from "the session that later took its number", and the wrong
      // answer is silent -- the reader lands in a conversation they did not name.
      expect(sessionKeyFromShort('chat-1380', LATER, undefined)).toBeNull()
      expect(sessionKeyFromShort('chat-1380', ['chat-1380-1'], undefined)).toBeNull()
    })

    it('does not apply to a FULL key, which names its generation exactly', () => {
      expect(sessionKeyFrom('chat-1380-1789049480')).toBe('chat-1380-1789049480')
    })
  })
})

describe('chatHrefSid', () => {
  it('returns the session parameter verbatim, unresolved', () => {
    // The caller decides which spellings name a session, so a short sid comes
    // back as authored instead of being refused by the full-key grammar.
    expect(chatHrefSid('/chat?sid=chat-1380')).toBe('chat-1380')
    expect(chatHrefSid('/chat?sid=dashboard_chat-24-1784661951')).toBe('dashboard_chat-24-1784661951')
  })

  it('applies the same href gates the resolved reader does', () => {
    expect(chatHrefSid('https://elsewhere.example/chat?sid=chat-24-1784661951')).toBeNull()
    expect(chatHrefSid('/chat?sid=chat-24-1784661951&mid=abc123')).toBeNull()
    expect(chatHrefSid('/chats?sid=chat-24-1784661951')).toBeNull()
    expect(chatHrefSid('/chat')).toBeNull()
  })

  it('is the sole gate sessionKeyFromChatHref adds the key grammar to', () => {
    // Pins the composition, so the two readers cannot drift apart on which hrefs
    // they accept.
    for (const href of [
      '/chat?sid=chat-24-1784661951',
      '/chat?sid=chat-1380',
      '/chat?sid=nonsense',
      'https://elsewhere.example/chat?sid=chat-24-1784661951',
    ]) {
      const sid = chatHrefSid(href)
      expect(sessionKeyFromChatHref(href)).toBe(sid ? sessionKeyFrom(sid) : null)
    }
  })
})

describe('namesASession', () => {
  it('accepts both spellings, and nothing else', () => {
    expect(namesASession('chat-24-1784661951')).toBe(true)
    expect(namesASession('chat-24')).toBe(true)
    expect(namesASession('dashboard_chat-24')).toBe(true)
    expect(namesASession('nonsense')).toBe(false)
    expect(namesASession('chat-24-1784661951.jsonl')).toBe(false)
  })

  it('is the union the two readers agree on, so neither can drift', () => {
    // The anchor's decline gate and the chip resolver must accept the SAME
    // spellings: a spelling one of them learned alone would either chip a link
    // the other navigates to a dead session view (#9914), or the reverse.
    for (const raw of ['chat-24-1784661951', 'chat-24', 'dashboard:chat-9', 'chat-', 'x', '']) {
      expect(namesASession(raw)).toBe(sessionKeyFrom(raw) !== null || isSessionShortName(raw))
    }
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
