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
  setConfigAutolinkRules,
  setConfigScanBudgetForTest,
  drainConfigScanBudget,
  wholeMatchAutolinkHref,
  autolinkHref,
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

afterEach(() => {
  resetAutolinkRulesForTest()
  setConfigScanBudgetForTest()
})

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

describe('setConfigAutolinkRules (dashboard.link_patterns)', () => {
  it('accepts a valid rule and resolves through the shared expansion', () => {
    setConfigAutolinkRules([{ pattern: '\\bPROJ-\\d+\\b', url: 'https://t.example/browse/{match}' }])
    const rules = getAutolinkRules()
    expect(rules).toHaveLength(1)
    expect(autolinkHref(rules[0], 'PROJ-12')).toBe('https://t.example/browse/PROJ-12')
  })

  it('drops entries the registration validator would refuse', () => {
    setConfigAutolinkRules([
      { pattern: '(unclosed', url: 'https://t.example/{match}' }, // invalid regex
      { pattern: 'a*', url: 'https://t.example/{match}' }, // matches empty string
      { pattern: 'ok', url: 'https://t.example/x' }, // no {match}
      { pattern: 'ok', url: 'ftp://t.example/{match}' }, // scheme
      { pattern: 'ok', url: 'https://{match}.evil.example/' }, // placeholder in authority
      { pattern: 'ok', url: 'https://u:p@t.example/{match}' }, // userinfo
    ])
    expect(getAutolinkRules()).toHaveLength(0)
  })

  it('refuses a catastrophic-backtracking pattern at registration, quickly', () => {
    const start = performance.now()
    setConfigAutolinkRules([
      { pattern: '(a+)+$', url: 'https://t.example/{match}' }, // canonical ReDoS shape
      { pattern: '(b+)+$', url: 'https://t.example/{match}' }, // pumps a char no fixed alphabet covers
      { pattern: '(a|aa)+$', url: 'https://t.example/{match}' }, // ambiguous alternation under repetition
      { pattern: '(\\d+)+$', url: 'https://t.example/{match}' }, // shorthand-class pump
      { pattern: '(ü+)+$', url: 'https://t.example/{match}' }, // non-ASCII pump
      { pattern: '(\\w+)\\1+', url: 'https://t.example/{match}' }, // quantified backreference
      { pattern: '(?:a{2,4})+$', url: 'https://t.example/{match}' }, // brace quantifier nested under +
      // ECMAScript closes `[^]` immediately (any char) — the POSIX
      // leading-]-is-literal rule would lex the catastrophic suffix as class
      // contents and let it through both scans.
      { pattern: '[^](a|aa)+$', url: 'https://t.example/{match}' },
      { pattern: '[](a|aa)+$', url: 'https://t.example/{match}' }, // empty class variant
      // UNQUANTIFIED backreferences: no quantifier nesting for the shape
      // gates, and the probe subjects miss the capture-split backtracking —
      // measured ~125ms each against a 2000-char subject.
      { pattern: '(a+)\\1b', url: 'https://t.example/{match}' }, // numeric backreference
      { pattern: '(?<g>a+)\\k<g>b', url: 'https://t.example/{match}' }, // named backreference
      { pattern: '\\bOK-\\d+\\b', url: 'https://t.example/{match}' }, // linear sibling survives
    ])
    const elapsed = performance.now() - start
    const rules = getAutolinkRules()
    expect(rules).toHaveLength(1)
    expect(rules[0].pattern.source).toBe('\\bOK-\\d+\\b')
    // Structural rejection decides these without executing anything; the
    // whole registration must stay far below anything a user would notice.
    expect(elapsed).toBeLessThan(2000)
  })

  it('keeps expressive SAFE shapes: unquantified groups, classes, bounded braces', () => {
    setConfigAutolinkRules([
      { pattern: '\\b(?:proj|ops)-\\d+\\b', url: 'https://t.example/{match}' }, // alternation, unquantified group
      { pattern: '\\b[a-z]{2,10}-\\d+\\b', url: 'https://t.example/{match}' }, // class + brace quantifier
      { pattern: '\\b(cr|mcm)-(\\d+)\\b', url: 'https://t.example/{match}' }, // captures without repetition
      { pattern: '\\b[\\]a-z]{2,10}-\\d+\\b', url: 'https://t.example/{match}' }, // escaped literal ] in a class
    ])
    expect(getAutolinkRules()).toHaveLength(4)
  })

  it('refuses any over-budget pump chain: unbounded, or wide bounded ranges', () => {
    const start = performance.now()
    setConfigAutolinkRules([
      { pattern: 'a*a*a*a*a*b', url: 'https://t.example/{match}' }, // adjacent unbounded pumps
      { pattern: '.*x.*y', url: 'https://t.example/{match}' }, // pumps split by overlapping must-consume atoms
      { pattern: '\\d+\\d+', url: 'https://t.example/{match}' }, // adjacent shorthand pumps
      { pattern: 'a*b?a*', url: 'https://t.example/{match}' }, // pumps around a min-zero atom
      { pattern: '(a*)(a*)!', url: 'https://t.example/{match}' }, // pumps across group syntax
      { pattern: '\\w+\\s\\w+', url: 'https://t.example/{match}' }, // separator does not license a second pump
      { pattern: '^a{0,1000}a{0,1000}a{0,1000}a{0,1000}a{0,1000}b$', url: 'https://t.example/{match}' }, // bounded-chain pumps: range never binds, the subject does
      { pattern: 'a{0,50}a{0,50}b', url: 'https://t.example/{match}' }, // two wide bounded pumps exceed the budget
      { pattern: '[a-h]{2,}-[j-z]{3,}', url: 'https://t.example/{match}' }, // two unbounded braces
      { pattern: '\\bID\\d{2,}x?\\b', url: 'https://t.example/{match}' }, // one wide pump + one `?` survives
      { pattern: '\\b[A-Z]{1,10}-\\d+\\b', url: 'https://t.example/{match}' }, // small range + one wide pump survives
      { pattern: '\\bw?x?y?-\\d+\\b', url: 'https://t.example/{match}' }, // optionals within budget survive
    ])
    const elapsed = performance.now() - start
    const sources = getAutolinkRules().map(r => r.pattern.source)
    expect(sources).toEqual(['\\bID\\d{2,}x?\\b', '\\b[A-Z]{1,10}-\\d+\\b', '\\bw?x?y?-\\d+\\b'])
    // Structural rejection decides these without executing anything.
    expect(elapsed).toBeLessThan(2000)
  })

  it('refuses width-fixed demand above the cap: exact quantifiers dodge the pump product', () => {
    // `{n}` opens no backtracking choice, so it carries zero pump factors and
    // the ≤24-char probe subjects never reach its width — but its LINEAR cost
    // repeats at every scan-restart position (~5ms per 2000-char node,
    // measured). The fixed-width cap refuses the demand syntactically.
    setConfigAutolinkRules([
      { pattern: 'a{1900}', url: 'https://t.example/{match}' }, // plain exact quantifier
      { pattern: '[a-z]{1000}[a-z]{999}b', url: 'https://t.example/{match}' }, // stacked fixed class atoms
      { pattern: '(?:ab){900}c', url: 'https://t.example/{match}' }, // quantified literal group
      { pattern: '\\d{1500}x', url: 'https://t.example/{match}' }, // shorthand-class fixed width
      { pattern: '\\d{150,}x', url: 'https://t.example/{match}' }, // floor above the cap on an unbounded brace
      { pattern: '[a-f0-9]{40}', url: 'https://t.example/{match}' }, // git SHA: fixed width within the cap survives
      { pattern: '\\b[PVDT]\\d{8,}\\b', url: 'https://t.example/{match}' }, // ticket-id vocabulary survives
      { pattern: '[a-f0-9]{100}', url: 'https://t.example/{match}' }, // boundary: exactly the cap survives
    ])
    const sources = getAutolinkRules().map(r => r.pattern.source)
    expect(sources).toEqual(['[a-f0-9]{40}', '\\b[PVDT]\\d{8,}\\b', '[a-f0-9]{100}'])
  })

  it('refuses fixed-width runs beside a pump: `{n}` adjacency dodges neither cap', () => {
    // `a+a{100}z` carries ONE pump (single-pump exempt before this gate) and
    // a within-cap fixed run — yet each split the pump opens re-verifies the
    // 100-wide tail, ~554ms measured for ONE synchronous exec on a 2000-char
    // subject, spent before the per-document budget can drain. Fixed widths
    // therefore pay into the pump product; zero-pump patterns stay exempt.
    setConfigAutolinkRules([
      { pattern: 'a+a{100}z', url: 'https://t.example/{match}' }, // the round-20 counterexample
      { pattern: '\\d{50}\\d+x', url: 'https://t.example/{match}' }, // order-independent: fixed run before the pump
      { pattern: '[a-z]*[a-z0-9]{20}', url: 'https://t.example/{match}' }, // overlapping class beside a star
      { pattern: '[a-f0-9]{40}:[a-f0-9]{40}', url: 'https://t.example/{match}' }, // zero pumps: deterministic SHA pair survives
      { pattern: '\\bREL-\\d{4}[a-z]?\\b', url: 'https://t.example/{match}' }, // small fixed (4) × one `?` beside the free pump — within budget
    ])
    const sources = getAutolinkRules().map(r => r.pattern.source)
    expect(sources).toEqual(['[a-f0-9]{40}:[a-f0-9]{40}', '\\bREL-\\d{4}[a-z]?\\b'])
  })

  it('caps the subject a config rule scans, leaving edition rules unbounded', () => {
    registerAutolinkRules([
      { id: 'edition-long', pattern: /\bED-\d+\b/g, href: 'https://edition.example/{match}' },
    ])
    setConfigAutolinkRules([{ pattern: '\\bCFG-\\d+\\b', url: 'https://cfg.example/{match}' }])
    const long = `${'x'.repeat(2100)} CFG-1 ED-1`
    expect(wholeMatchAutolinkHref('CFG-1')).toBe('https://cfg.example/CFG-1') // short subject resolves
    expect(wholeMatchAutolinkHref(`${'x'.repeat(2100)}CFG-1`)).toBeNull() // oversized subject skipped
    // The registered config rule carries the cap; the edition rule does not.
    const rules = getAutolinkRules()
    expect(rules.find(r => r.id === 'edition-long')?.maxSubject).toBeUndefined()
    expect(rules.find(r => r.id === 'config-link-pattern-0')?.maxSubject).toBe(2000)
    expect(long.length).toBeGreaterThan(2000)
  })

  it('REPLACES the config set on each call, leaving edition rules ahead', () => {
    registerAutolinkRules([
      { id: 'edition', pattern: /\bED-\d+\b/g, href: 'https://edition.example/{match}' },
    ])
    setConfigAutolinkRules([{ pattern: '\\bCFG-\\d+\\b', url: 'https://one.example/{match}' }])
    setConfigAutolinkRules([{ pattern: '\\bCFG-\\d+\\b', url: 'https://two.example/{match}' }])
    const rules = getAutolinkRules()
    expect(rules.map(r => r.id)).toEqual(['edition', 'config-link-pattern-0'])
    expect(autolinkHref(rules[1], 'CFG-1')).toBe('https://two.example/CFG-1')
  })

  it('an exhausted document scan budget stops config rules, never edition rules', () => {
    // The per-scan ladder bounds ONE execution; cost multiplies across
    // rules × text nodes, so the plugin meters config-rule execution against
    // a per-document budget. Zero budget = already exhausted: the config rule
    // must not link anywhere, while the unmetered edition vocabulary still does.
    registerAutolinkRules([
      { id: 'edition-budget', pattern: /\bED-\d+\b/g, href: 'https://edition.example/{match}' },
    ])
    setConfigAutolinkRules([{ pattern: '\\bCFG-\\d+\\b', url: 'https://cfg.example/{match}' }])
    setConfigScanBudgetForTest(0)
    const tree = run('see CFG-1 and ED-1\n\nand CFG-2') as unknown as N
    const urls: string[] = []
    const walk = (n: N): void => {
      if (n.type === 'link' && n.url) urls.push(n.url)
      for (const c of n.children ?? []) walk(c)
    }
    walk(tree)
    expect(urls).toEqual(['https://edition.example/ED-1'])
  })

  it('the budget is re-armed per document from the configured value', () => {
    setConfigAutolinkRules([{ pattern: '\\bCFG-\\d+\\b', url: 'https://cfg.example/{match}' }])
    setConfigScanBudgetForTest(0)
    expect(links('CFG-1')).toEqual([]) // runs with budget 0 and links nothing
    // Restoring the default gives the next document its own fresh budget.
    setConfigScanBudgetForTest()
    expect(links('see CFG-3').map(l => l.url)).toEqual(['https://cfg.example/CFG-3'])
  })
})

describe('wholeMatchAutolinkHref', () => {
  it('resolves only when the entire text is one match', () => {
    setConfigAutolinkRules([{ pattern: '\\bPROJ-\\d+\\b', url: 'https://t.example/browse/{match}' }])
    expect(wholeMatchAutolinkHref('PROJ-123')).toBe('https://t.example/browse/PROJ-123')
    expect(wholeMatchAutolinkHref('run PROJ-123 now')).toBeNull()
    expect(wholeMatchAutolinkHref('PROJ-123x')).toBeNull()
    expect(wholeMatchAutolinkHref('')).toBeNull()
  })

  it('sees edition-registered rules too', () => {
    registerAutolinkRules([
      { id: 'edition-wm', pattern: /\bED-\d+\b/g, href: 'https://edition.example/{match}' },
    ])
    // The chip is the CONFIG feature's UI: an edition rule links prose via
    // the remark pass but does not flip a shipped copy-chip into a link chip.
    expect(wholeMatchAutolinkHref('ED-7')).toBeNull()
    setConfigAutolinkRules([{ pattern: '\\bCFG-\\d+\\b', url: 'https://cfg.example/{match}' }])
    expect(wholeMatchAutolinkHref('CFG-7')).toBe('https://cfg.example/CFG-7')
  })

  it('drains the shared scan budget: an exhausted pool degrades the chip to copy-only', () => {
    // Inline-code spans are transcript text: without the meter, ten 2KB
    // spans x 50 gate-passing rules re-run the round-15 freeze through the
    // chip path (measured ~7.8s). Exhausted pool => null, i.e. copy chip.
    setConfigAutolinkRules([{ pattern: '\\bCFG-\\d+\\b', url: 'https://cfg.example/{match}' }])
    setConfigScanBudgetForTest(0)
    expect(wholeMatchAutolinkHref('CFG-7')).toBeNull()
    setConfigScanBudgetForTest() // restore: the same span resolves again
    expect(wholeMatchAutolinkHref('CFG-7')).toBe('https://cfg.example/CFG-7')
  })

  it('never re-arms the pool at tree entry: one message shares one budget across its blocks', () => {
    // One MESSAGE assembles many remark trees (`useBlockAssembler` mounts one
    // per fence-separated block). If the plugin restored the pool per tree,
    // every block would get a fresh 50ms and a fence-heavy message would
    // multiply the ceiling by its block count (the round-21 bypass). So: with
    // the DEFAULT budget drained mid-message, a later block's tree must stay
    // plain — only the top-level renderer re-arms, once per message.
    setConfigAutolinkRules([{ pattern: '\\bCFG-\\d+\\b', url: 'https://cfg.example/{match}' }])
    drainConfigScanBudget(10_000) // a prior block spent the message's pool
    const tree = run('later block with CFG-9') as unknown as N
    const urls: string[] = []
    const walk = (n: N): void => {
      if (n.type === 'link' && n.url) urls.push(n.url)
      for (const c of n.children ?? []) walk(c)
    }
    walk(tree)
    expect(urls).toEqual([])
  })
})
