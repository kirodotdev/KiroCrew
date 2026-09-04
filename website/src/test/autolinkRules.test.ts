/**
 * Bare-token autolink seam.
 *
 * The exclusion cases carry the weight: the plugin is trivially right on a bare
 * token in a sentence, and every real defect is a token somewhere it must NOT be
 * linked. Each has a paired positive assertion, so a case that passes because the
 * rule never fired at all shows up as a failure.
 *
 * Four exclusions the source-rewrite version needed are gone by construction, so
 * the cases that pinned them now assert the linking they used to refuse.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import remarkAutolinkRules from '../utils/remarkAutolinkRules'
import {
  registerAutolinkRules,
  getAutolinkRules,
  resetAutolinkRulesForTest,
} from '../utils/autolinkRules'

const PIPE = unified().use(remarkParse).use(remarkGfm).use(remarkAutolinkRules).freeze()

type N = { type: string; url?: string; value?: string; children?: N[] }

const run = (md: string): N => PIPE.runSync(PIPE.parse(md)) as unknown as N

/** Every link in the transformed tree, as `url` plus its own text. */
const links = (md: string): Array<{ url?: string; text: string }> => {
  const out: Array<{ url?: string; text: string }> = []
  const text = (n: N): string =>
    n.value ?? (n.children ?? []).map(text).join('')
  const walk = (n: N): void => {
    if (n.type === 'link') out.push({ url: n.url, text: text(n) })
    for (const c of n.children ?? []) walk(c)
  }
  walk(run(md))
  return out
}

/** Concatenated prose, to prove an excluded token survived unchanged. */
const prose = (md: string): string => {
  const parts: string[] = []
  const walk = (n: N): void => {
    if (n.value) parts.push(n.value)
    for (const c of n.children ?? []) walk(c)
  }
  walk(run(md))
  return parts.join('')
}

const CR = () =>
  registerAutolinkRules([
    { id: 'cr', pattern: /\bCR-\d+\b/g, href: 'https://example.invalid/reviews/{match}' },
  ])

afterEach(() => resetAutolinkRulesForTest())

describe('remarkAutolinkRules', () => {
  it('adds no link with nothing registered', () => {
    expect(getAutolinkRules()).toHaveLength(0)
    expect(links('See CR-123 and `CR-7`.')).toHaveLength(0)
  })

  it('links a bare token in prose', () => {
    CR()
    expect(links('See CR-123 please')).toEqual([
      { url: 'https://example.invalid/reviews/CR-123', text: 'CR-123' },
    ])
  })

  it('links every occurrence, not just the first', () => {
    CR()
    expect(links('CR-1 then CR-2')).toHaveLength(2)
  })

  it('keeps the surrounding prose intact around a link', () => {
    CR()
    expect(prose('See CR-123 please')).toBe('See CR-123 please')
  })

  // ── exclusions that remain ────────────────────────────────────────────────

  it('leaves a token inside an inline code span alone', () => {
    CR()
    expect(links('use `CR-123` verbatim')).toHaveLength(0)
    expect(links('use `x` then CR-123')).toHaveLength(1)
  })

  it('leaves a token inside a fenced block alone', () => {
    CR()
    expect(links('```\ncr: CR-123\n```')).toHaveLength(0)
  })

  it('leaves a token inside an existing link label alone', () => {
    CR()
    expect(links('[CR-123](https://elsewhere.invalid/x)')).toEqual([
      { url: 'https://elsewhere.invalid/x', text: 'CR-123' },
    ])
  })

  it('leaves a token inside a pasted bare URL alone', () => {
    CR()
    const src = 'https://example.invalid/reviews/CR-123'
    expect(links(src)).toEqual([{ url: src, text: src }])
  })

  it('leaves an escaped token alone', () => {
    CR()
    expect(links('\\CR-123')).toHaveLength(0)
    expect(links('\\CR-123 and CR-9')).toHaveLength(1)
  })

  it('leaves a token enclosed by raw inline HTML alone', () => {
    CR()
    // The tags are their own nodes; the text between them is a SIBLING, so the
    // walk has to pair the tags rather than rely on node type.
    expect(links('a <code>CR-1</code> b')).toHaveLength(0)
    expect(links('a <code>x</code> CR-1')).toHaveLength(1)
  })

  it.each([
    ['a double-quoted attribute', 'a <code title="1 > 0">CR-1</code> b'],
    ['a single-quoted attribute', "a <code title='1 > 0'>CR-1</code> b"],
  ])('leaves a token enclosed by an element with %s containing >', (_label, src) => {
    CR()
    expect(links(src)).toHaveLength(0)
  })

  it.each([
    ['emphasis', 'a <a href="http://x">**CR-1**</a> b'],
    ['italics', 'a <a href="http://x">*CR-1*</a> b'],
    ['strikethrough', 'a <a href="http://x">~~CR-1~~</a> b'],
  ])('leaves a token inside %s between paired raw <a> tags alone', (_label, src) => {
    CR()
    expect(links(src)).toHaveLength(0)
  })

  it('still links a token inside emphasis with no enclosing tag', () => {
    CR()
    expect(links('a **CR-1** b')).toHaveLength(1)
  })

  it('leaves a token in a footnote label alone', () => {
    CR()
    // A `footnoteReference` label is a property, not children, so no exclusion
    // is needed for it — the walk simply never reaches a text node.
    expect(links('note[^CR-1]\n\n[^CR-1]: body')).toHaveLength(0)
  })

  it('does not match inside a longer word when the pattern is anchored', () => {
    CR()
    expect(links('INCR-123')).toHaveLength(0)
  })

  // ── exclusions that dissolved with the source rewrite ─────────────────────

  it('links a match carrying a bracket, which no longer re-delimits anything', () => {
    registerAutolinkRules([
      { id: 'brackety', pattern: /X\]\d+/g, href: 'https://example.invalid/{match}' },
    ])
    expect(links('X]1')).toHaveLength(1)
  })

  it('links a token directly preceded by a bang, which cannot make an image', () => {
    CR()
    expect(links('wow!CR-1')).toEqual([
      { url: 'https://example.invalid/reviews/CR-1', text: 'CR-1' },
    ])
  })

  it('keeps a destination containing a paren intact', () => {
    registerAutolinkRules([
      { id: 'r', pattern: /\bCR-\d+\b/g, href: 'https://example.invalid/x)y/{match}' },
    ])
    expect(links('CR-1')[0].url).toBe('https://example.invalid/x)y/CR-1')
  })

  // ── href template validation, all at registration ────────────────────────

  it.each([
    ['javascript:alert(1){match}', 'a non-http(s) scheme'],
    ['data:text/html,{match}', 'a data URL'],
    ['/relative/{match}', 'a relative destination'],
    ['https://user:pw@example.invalid/{match}', 'Basic-auth userinfo'],
    ['https://{match}.example.invalid/', 'a placeholder in the authority'],
    ['https://example.invalid:{match}/', 'a placeholder in the port'],
    ['https://example.invalid/fixed', 'no placeholder at all'],
  ])('refuses %s (%s)', template => {
    expect(() =>
      registerAutolinkRules([{ id: 'r', pattern: /\bCR-\d+\b/g, href: template }]),
    ).toThrow(/unusable href template/)
    expect(getAutolinkRules()).toHaveLength(0)
  })

  it('percent-encodes the match so it cannot escape its path segment', () => {
    registerAutolinkRules([
      { id: 'enc', pattern: /Z[^ ]+/g, href: 'https://example.invalid/x/{match}' },
    ])
    const url = links('Za/b@c')[0].url
    expect(url).toBe('https://example.invalid/x/Za%2Fb%40c')
    expect(new URL(url as string).origin).toBe('https://example.invalid')
  })

  it('encodeURIComponent itself throws on a lone surrogate', () => {
    // The mechanism control: a stream cut mid-pair yields an unpaired high
    // surrogate, which is why the substitution cannot hand one to the encoder.
    expect(() => encodeURIComponent('TICKET-\uD83D')).toThrow(URIError)
  })

  it('survives a match split mid-surrogate-pair', () => {
    registerAutolinkRules([
      { id: 'sur', pattern: /TICKET-\S*/gu, href: 'https://example.invalid/{match}' },
    ])
    expect(() => links('TICKET-\uD83D')).not.toThrow()
    expect(links('TICKET-\uD83D')[0].url).toBe('https://example.invalid/TICKET-%EF%BF%BD')
  })

  it('keeps an intact surrogate pair intact', () => {
    registerAutolinkRules([
      { id: 'sur2', pattern: /TICKET-\S*/gu, href: 'https://example.invalid/{match}' },
    ])
    expect(links('TICKET-\u{1F4A9}')[0].url).toBe(
      `https://example.invalid/TICKET-${encodeURIComponent('\u{1F4A9}')}`,
    )
  })

  it('links the NORMALIZED destination, not the raw template', () => {
    registerAutolinkRules([
      { id: 'r', pattern: /\bCR-\d+\b/g, href: ' https://example.invalid/a\tb/{match} ' },
    ])
    expect(links('CR-1')[0].url).toBe('https://example.invalid/ab/CR-1')
  })

  // ── ordering ─────────────────────────────────────────────────────────────

  it('gives an overlapping span to the earlier-registered rule', () => {
    registerAutolinkRules([
      { id: 'first', pattern: /\bCR-\d+\b/g, href: 'https://first.invalid/{match}' },
      { id: 'second', pattern: /\bCR-\d+\b/g, href: 'https://second.invalid/{match}' },
    ])
    expect(links('CR-1')[0].url).toBe('https://first.invalid/CR-1')
  })

  it('keeps registration order when the LATER rule starts earlier', () => {
    registerAutolinkRules([
      { id: 'inner', pattern: /BAR/g, href: 'https://inner.invalid/{match}' },
      { id: 'outer', pattern: /FOO-BAR/g, href: 'https://outer.invalid/{match}' },
    ])
    expect(links('FOO-BAR')).toEqual([{ url: 'https://inner.invalid/BAR', text: 'BAR' }])
  })

  it('still links a non-overlapping match from a later rule', () => {
    registerAutolinkRules([
      { id: 'inner', pattern: /BAR/g, href: 'https://inner.invalid/{match}' },
      { id: 'outer', pattern: /\bZZ-\d+\b/g, href: 'https://outer.invalid/{match}' },
    ])
    expect(links('BAR and ZZ-9').map(l => l.url)).toEqual([
      'https://inner.invalid/BAR',
      'https://outer.invalid/ZZ-9',
    ])
  })
})

describe('registerAutolinkRules validation', () => {
  it('abandons a rule that yields a zero-width match instead of hanging', () => {
    // Measured: bumping lastIndex instead loops forever, because under `u` the
    // bump lands mid-surrogate and the engine re-matches the same position.
    registerAutolinkRules([
      { id: 'zerowidth', pattern: /(?=💩)/gu, href: 'https://example.invalid/{match}' },
    ])
    expect(links('hello 💩 world')).toHaveLength(0)
  }, 5000)

  it('abandoning a zero-width rule does not disable a sibling rule', () => {
    registerAutolinkRules([
      { id: 'zerowidth', pattern: /(?=💩)/gu, href: 'https://example.invalid/{match}' },
      { id: 'real', pattern: /\bCR-\d+\b/g, href: 'https://example.invalid/{match}' },
    ])
    expect(links('💩 CR-1')).toHaveLength(1)
  }, 5000)

  it('ignores a duplicate id', () => {
    CR()
    expect(() => CR()).toThrow(/already registered/)
    expect(getAutolinkRules()).toHaveLength(1)
  })

  it('adds a missing global flag rather than refusing', () => {
    registerAutolinkRules([
      { id: 'nog', pattern: /\bCR-\d+\b/, href: 'https://example.invalid/{match}' },
    ])
    expect(getAutolinkRules()[0].pattern.global).toBe(true)
    expect(links('CR-1 and CR-2')).toHaveLength(2)
  })

  it('refuses a sticky pattern', () => {
    expect(() =>
      registerAutolinkRules([
        { id: 'sticky', pattern: /\bCR-\d+\b/y, href: 'https://example.invalid/{match}' },
      ]),
    ).toThrow(/unusable pattern/)
    expect(getAutolinkRules()).toHaveLength(0)
  })

  it('refuses a pattern that matches the empty string', () => {
    expect(() =>
      registerAutolinkRules([
        { id: 'empty', pattern: /\d*/g, href: 'https://example.invalid/{match}' },
      ]),
    ).toThrow(/unusable pattern/)
    expect(getAutolinkRules()).toHaveLength(0)
  })
})
