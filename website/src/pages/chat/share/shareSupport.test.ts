import { describe, it, expect } from 'vitest'
import {
  buildIntentUrl, clampExcerpt, prevUserTextFor, scanSensitive, CARD_EXCERPT_LIMIT,
} from './shareSupport'

describe('scanSensitive', () => {
  it('flags an AWS access key id', () => {
    expect(scanSensitive('creds: AKIAIOSFODNN7EXAMPLE done')).toContain('aws_key')
  })

  it('flags GitHub / Slack tokens and bearer headers as token', () => {
    expect(scanSensitive('ghp_abcdefghijklmnopqrstu0123456789')).toContain('token')
    expect(scanSensitive('xoxb-1234567890-abcdefghij')).toContain('token')
    expect(scanSensitive('Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x')).toContain('token')
  })

  it('flags private key blocks, home paths, and loopback URLs', () => {
    // Assembled at runtime so the repo's own credential-pattern scrub lint
    // never sees the PEM header as a literal in source.
    const pemHeader = ['-----BEGIN RSA', 'KEY-----'].join(' PRIVATE ')
    expect(scanSensitive(pemHeader)).toContain('private_key')
    expect(scanSensitive('see /home/alice/notes.txt')).toContain('local_path')
    expect(scanSensitive('C:\\Users\\alice\\x')).toContain('local_path')
    expect(scanSensitive('open http://localhost:5476/settings')).toContain('internal_url')
    expect(scanSensitive('at http://192.168.1.7/admin')).toContain('internal_url')
    expect(scanSensitive('via http://172.16.0.1/console')).toContain('internal_url')
    expect(scanSensitive('via http://172.31.255.1:8080/x')).toContain('internal_url')
    expect(scanSensitive('local http://[::1]:5476/chat')).toContain('internal_url')
    // 172.32.x is public space — not a finding.
    expect(scanSensitive('see http://172.32.0.1/page')).toEqual([])
  })

  it('dedupes kinds and stays quiet on ordinary technical prose', () => {
    expect(scanSensitive('ghp_abcdefghijklmnopqrstu0123456789 and xoxb-1234567890-abcdefghij')).toEqual(['token'])
    expect(scanSensitive('Kiro Crew triaged 47 issues and opened PR #7202, CI green.')).toEqual([])
    // Public URLs and relative paths are not findings.
    expect(scanSensitive('https://github.com/kirodotdev/KiroCrew src/index.ts')).toEqual([])
  })
})

describe('buildIntentUrl', () => {
  it('URL-encodes the caption for both platforms', () => {
    const x = buildIntentUrl('x', 'hello #AIAgent & more')
    expect(x).toBe('https://x.com/intent/post?text=hello%20%23AIAgent%20%26%20more')
    const li = buildIntentUrl('linkedin', 'a b')
    expect(li).toBe('https://www.linkedin.com/feed/?shareActive=true&text=a%20b')
  })
})

describe('clampExcerpt', () => {
  it('strips markdown syntax down to card prose', () => {
    const md = [
      '## Batch done',
      'Scanned **47 issues** and `dispatched` them, see [PR #7202](https://example.test/pr).',
      '![summary](/tmp/x.png)',
      '- first item',
      '```bash\ngh pr checks\n```',
    ].join('\n\n')
    const out = clampExcerpt(md)
    expect(out).toContain('Batch done')
    expect(out).toContain('Scanned 47 issues and dispatched them, see PR #7202.')
    expect(out).toContain('· first item')
    expect(out).toContain('gh pr checks')
    // Syntax and undisplayable payloads are gone: no marks, no image ref, no URL.
    for (const noise of ['##', '**', '`', '![', '](', '/tmp/x.png', 'example.test']) {
      expect(out).not.toContain(noise)
    }
  })

  it('passes short text through and collapses blank runs', () => {
    expect(clampExcerpt('a\n\n\n\nb')).toBe('a\n\nb')
  })

  it('cuts at a word boundary near the cap and appends an ellipsis', () => {
    const words = Array.from({ length: 200 }, (_, i) => `word${i}`).join(' ')
    const out = clampExcerpt(words)
    expect(out.length).toBeLessThanOrEqual(CARD_EXCERPT_LIMIT + 1)
    expect(out.endsWith('…')).toBe(true)
    // The kept text is an input prefix that ends exactly where a space was:
    // the cut never lands mid-word.
    const body = out.slice(0, -1)
    expect(words.startsWith(body)).toBe(true)
    expect(words[body.length]).toBe(' ')
  })

  it('cuts CJK text (no spaces) at the cap', () => {
    const cjk = '多'.repeat(CARD_EXCERPT_LIMIT + 50)
    const out = clampExcerpt(cjk)
    expect(out.length).toBe(CARD_EXCERPT_LIMIT + 1)
    expect(out.endsWith('…')).toBe(true)
  })

  it('never splits a surrogate pair at the cap', () => {
    // An emoji straddling the UTF-16 cut would leave a dangling high
    // surrogate that renders as a corrupt glyph in the exported card.
    const straddling = '多'.repeat(CARD_EXCERPT_LIMIT - 1) + '😀' + '尾'.repeat(30)
    const out = clampExcerpt(straddling)
    expect(out.isWellFormed()).toBe(true)
    expect(out.endsWith('…')).toBe(true)
    // The half-cut emoji is dropped entirely rather than kept corrupt.
    expect(out).not.toContain('😀')
  })
})

describe('prevUserTextFor', () => {
  const msgs = [
    { role: 'user', content: 'first question' },
    { role: 'assistant', content: 'first answer' },
    { role: 'user', content: '  ' },
    { role: 'assistant', content: 'second answer' },
  ]

  it('finds the nearest preceding non-empty user row', () => {
    expect(prevUserTextFor(msgs, 3)).toBe('first question')
    expect(prevUserTextFor(msgs, 1)).toBe('first question')
  })

  it('returns undefined when nothing precedes', () => {
    expect(prevUserTextFor(msgs, 0)).toBeUndefined()
    expect(prevUserTextFor([], 0)).toBeUndefined()
  })

  it('tolerates an out-of-range index', () => {
    expect(prevUserTextFor(msgs, 99)).toBe('first question')
  })
})
