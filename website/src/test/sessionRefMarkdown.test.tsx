/**
 * Markdown breakout tests for staged session references.
 *
 * These render the emitted link through the app's REAL markdown pipeline
 * (`react-markdown` + `remark-gfm`) rather than asserting on the string, because
 * the vectors here are properties of the parser, not of the text. Two of them
 * were found only by rendering:
 *
 *   - a TRAILING backslash escapes the generated `]`, so GFM discards our link
 *     and renders only the autolink hidden in the label — it REPLACES our link
 *     rather than adding one;
 *   - a bare `<https://…>` autolinks even while our link parses correctly,
 *     adding a second href with no backslash involved.
 *
 * A staged ref's key and title both survive a round-trip through
 * sessionStorage, and `sanitizeSessionRefs` only requires a non-empty string
 * key — so both fields are untrusted input at this boundary.
 *
 * The invariant asserted for every payload: exactly ONE href, and its origin is
 * our own. Never a link the payload chose.
 */
import { describe, it, expect } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { appendSessionRefLinks, formatSessionRefLink, type SessionRef } from '../utils/sessionRefs'

const EVIL = 'https://evil.example/pwned'

const renderMd = (md: string): string =>
  renderToStaticMarkup(<Markdown remarkPlugins={[remarkGfm]}>{md}</Markdown>)

/**
 * Every payload must render to exactly one link, pointing at our own origin.
 *
 * The check is deliberately on the href's ORIGIN, not on whether the attacker's
 * hostname appears anywhere in the string: a crafted key round-trips through
 * `?sid=` as percent-encoded data, so `evil.example` legitimately appears inside
 * an href whose origin is ours. Asserting its absence would fail on safe output.
 * What must never appear is a scheme-bearing link TO it.
 */
const expectSingleSameOriginLink = (md: string, label: string) => {
  const html = renderMd(md)
  const hrefs = Array.from(html.matchAll(/href="([^"]+)"/g)).map(m => m[1])
  expect(hrefs, `${label}: expected exactly one href, got ${JSON.stringify(hrefs)}`).toHaveLength(1)
  expect(new URL(hrefs[0]).origin, `${label}: href must be same-origin`).toBe(window.location.origin)
  expect(html, `${label}: no link may target the payload's host`)
    .not.toMatch(/href="[a-z]+:\/\/evil\.example/i)
}

/** Payloads that previously produced an attacker-controlled href. */
const PAYLOADS: Array<[name: string, ref: SessionRef]> = [
  // Measured before the fix: ONE href, and it was the attacker's — our link gone.
  ['trailing backslash in title', { key: 'k', title: `<${EVIL}>\\` }],
  ['trailing backslash in key', { key: `x\\`, title: '' }],
  // Measured before the fix: TWO hrefs, no backslash needed.
  ['bare autolink in title', { key: 'k', title: `<${EVIL}>` }],
  ['bare autolink in key', { key: `<${EVIL}>`, title: '' }],
  // Bracket termination, the original vector.
  ['bracket termination in title', { key: 'k', title: `x](${EVIL})` }],
  ['bracket termination in key', { key: `x](${EVIL})`, title: '' }],
  // Combinations and near-misses.
  ['backslash then bracket', { key: 'k', title: `a\\](${EVIL})` }],
  ['angle brackets around text', { key: 'k', title: `<b>bold</b>` }],
  ['newline split', { key: 'k', title: `line one\n<${EVIL}>` }],
  ['everything at once', { key: `<${EVIL}>\\]`, title: `[<${EVIL}>\\]` }],
  // Degenerate: both fields sanitize to nothing, so the label falls back to the
  // URL. Must still be one same-origin link.
  ['both sanitize empty', { key: ']]\\<>', title: '[[\\<>' }],
]

describe('session-ref links cannot break out of markdown', () => {
  for (const [name, ref] of PAYLOADS) {
    it(`emits one same-origin link for: ${name}`, () => {
      expectSingleSameOriginLink(formatSessionRefLink(ref), name)
    })
  }

  it('holds when appended to user text alongside other refs', () => {
    const md = appendSessionRefLinks('look at these', [
      { key: 'good', title: 'A normal session' },
      { key: 'k', title: `<${EVIL}>\\` },
    ])
    const html = renderMd(md)
    const hrefs = Array.from(html.matchAll(/href="([^"]+)"/g)).map(m => m[1])
    expect(hrefs).toHaveLength(2)
    for (const h of hrefs) expect(new URL(h).origin).toBe(window.location.origin)
    expect(html).not.toMatch(/href="[a-z]+:\/\/evil\.example/i)
  })

  it('still renders a benign title as a working link with its real label', () => {
    const md = formatSessionRefLink({ key: 'chat-7', title: 'Release notes for 0.5.0' })
    expectSingleSameOriginLink(md, 'benign')
    expect(renderMd(md)).toContain('Release notes for 0.5.0')
  })

  it('keeps parentheses in the label — they are inert once brackets are gone', () => {
    // Guards against over-sanitizing: titles legitimately contain parens, and
    // the probe confirmed they cannot terminate the label.
    const md = formatSessionRefLink({ key: 'chat-8', title: 'Fix the flake (again)' })
    expectSingleSameOriginLink(md, 'parens')
    expect(renderMd(md)).toContain('(again)')
  })
})
