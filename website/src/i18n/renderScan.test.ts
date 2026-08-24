/**
 * Tests for the render gate's analysis layer (`scripts/lib/render-scan.mjs`).
 *
 * The gate itself needs a browser and a DEV build, so it runs in CI rather than in
 * vitest. That leaves its REASONING untested unless it is tested here: whether a
 * `]…[` seam is really a concatenation, whether a word is really un-accented, which
 * exclusions apply. Those are pure string functions, and a wrong answer in one of
 * them either hides a defect forever or floods the ledger with noise.
 *
 * The other half of the job is locking the invariants the gate BORROWS from
 * `gen-pseudolocale.mjs`. The scanner assumes every catalog value is `[`-wrapped and
 * that no single value contains a `]…[` seam of its own. If the generator ever
 * changed its delimiters, the gate would not fail — it would silently find nothing.
 * So we assert those properties against the committed catalog too.
 */
import { describe, it, expect } from 'vitest'
import enXA from './locales/en-XA.json'
import glossary from './glossary.json'
import {
  splitUnits, isFiller, latinLeaks, gradeRun, dntViolations, expansionBudget,
  browserBundle, scanDocument, ALWAYS_LATIN, PSEUDO_PAD,
} from '../../scripts/lib/render-scan.mjs'

const flat = (obj, prefix = '', out = {}) => {
  for (const [k, v] of Object.entries(obj)) {
    if (v && typeof v === 'object') flat(v, `${prefix}${k}.`, out)
    else out[`${prefix}${k}`] = v
  }
  return out
}
const VALUES = Object.values(flat(enXA))

describe('splitUnits', () => {
  it('treats a single bracketed value as one unit with no orphans', () => {
    const { units, orphans } = splitUnits('[Şèşşìøñş ····]')
    expect(units).toEqual(['[Şèşşìøñş ····]'])
    expect(orphans).toEqual([])
  })

  it('splits two adjacent values at the `]…[` seam', () => {
    const { units, orphans } = splitUnits('[Şţàŕ ùş]·[Ŕèþøŕţ ìşşùè]')
    expect(units).toHaveLength(2)
    expect(orphans).toEqual(['·'])
  })

  it('reports text with no wrapper at all as pure orphan', () => {
    // A run with zero catalog units is still scanned — skipping it would report
    // nothing for a fully hardcoded component.
    expect(splitUnits('6m 38s')).toEqual({ units: [], orphans: ['6m 38s'] })
  })

  it('does not use bracket DEPTH, so copy carrying its own brackets survives', () => {
    // "[ Paste #" pseudolocalises to a value with an extra `[` inside it.
    const { units, orphans } = splitUnits('[[ Þàşţè # ···]')
    expect(units).toEqual(['[[ Þàşţè # ···]'])
    expect(orphans).toEqual([])
  })

  it('keeps leading and trailing text outside the units', () => {
    const { orphans } = splitUnits('Total: [ṽàĺùè] items')
    expect(orphans).toEqual(['Total: ', ' items'])
  })
})

describe('isFiller', () => {
  it('discounts padding and whitespace', () => {
    expect(isFiller(` ${PSEUDO_PAD}${PSEUDO_PAD} `)).toBe(true)
    expect(isFiller('·x·')).toBe(false)
  })
})

describe('latinLeaks', () => {
  it('finds un-accented words', () => {
    expect(latinLeaks('Chat')).toEqual(['Chat'])
  })

  it('finds single-letter unit symbols', () => {
    // `8h 49m 29s` under a translated heading is a real shipped defect. A >=3-letter
    // threshold misses every one of these.
    expect(latinLeaks('8h 49m 29s')).toEqual(['h', 'm', 's'])
  })

  it('ignores accented pseudolocale text', () => {
    expect(latinLeaks('Şèşşìøñş')).toEqual([])
  })

  it('ignores do-not-translate terms and language endonyms', () => {
    expect(latinLeaks('GitHub')).toEqual([])
    expect(latinLeaks('Node.js')).toEqual([])
    expect(latinLeaks('Español')).toEqual([])
  })

  it('removes longer terms first, so GitHub does not leave a bare Hub', () => {
    expect(ALWAYS_LATIN).toContain('Git')
    expect(ALWAYS_LATIN).toContain('GitHub')
    // `and` is correctly reported — it is untranslated copy. The point is that no
    // fragment of either DNT term survives to be reported alongside it.
    expect(latinLeaks('GitHub and Git')).toEqual(['and'])
  })

  it('ignores the markup and nesting regions the generator preserves', () => {
    expect(latinLeaks('<your-host>')).toEqual([])
    expect(latinLeaks('kirocrew app install <path>'.replace(/kirocrew/, ''))).toEqual(['app', 'install'])
    expect(latinLeaks('$t(app.name)')).toEqual([])
  })

  it('strips a nested preserved region without orphaning its host', () => {
    // `https://<your-host>` is a real committed value. Stripping `<your-host>` first
    // leaves `https://`, which no longer matches the URL pattern.
    expect(latinLeaks('https://<your-host>')).toEqual([])
  })

  it('ignores interpolation placeholders and URLs, which pass through un-accented', () => {
    expect(latinLeaks('{{count}}')).toEqual([])
    expect(latinLeaks('https://github.com/owner/repo')).toEqual([])
  })

  it('honours a raised minLetters', () => {
    expect(latinLeaks('8h 49m', { minLetters: 3 })).toEqual([])
  })
})

describe('gradeRun', () => {
  it('flags a run built from two catalog units', () => {
    const { fragments } = gradeRun('[Şţàŕ ùş]·[Ŕèþøŕţ ìşşùè]')
    expect(fragments.map(f => f.signal)).toContain('multi-unit')
  })

  it('flags orphan punctuation that continues a translated sentence', () => {
    const { fragments } = gradeRun('[Ŕèàðý]: ')
    expect(fragments.map(f => f.signal)).toContain('continuation-punctuation')
  })

  it('flags a closing delimiter the units left open', () => {
    const { fragments } = gradeRun('[Ŕèàðý (ɱøðè])')
    expect(fragments.map(f => f.signal)).toContain('dangling-delimiter')
  })

  it('reports leaks in a run with NO catalog unit at all', () => {
    // Guards the `units.length === 0` case: skipping a run with no catalog unit
    // would make the gate blinder the more untranslated a surface is.
    const { leaks, fragments } = gradeRun('Cron Jobs')
    expect(leaks).toEqual(['Cron', 'Jobs'])
    expect(fragments).toEqual([])
  })

  it('reports a hardcoded suffix beside a translated unit', () => {
    const { leaks } = gradeRun('[Ùþţìɱè ···] 6m 38s')
    expect(leaks).toEqual(['m', 's'])
  })

  it('stays silent on a correctly translated single unit', () => {
    const { leaks, fragments } = gradeRun('[Şèşşìøñş ············]')
    expect(leaks).toEqual([])
    expect(fragments).toEqual([])
  })
})

describe('dntViolations', () => {
  const dnt = glossary.dnt

  it('accepts the exact term', () => {
    expect(dntViolations('Open GitHub to continue', dnt)).toEqual([])
  })

  it('reports wrong internal capitalisation', () => {
    expect(dntViolations('Open Github to continue', dnt))
      .toEqual([{ term: 'GitHub', found: 'Github' }])
  })

  it('reports a separator turned into a space', () => {
    expect(dntViolations('Requires Node js 24', dnt))
      .toEqual([{ term: 'Node.js', found: 'Node js' }])
  })

  it('accepts an all-lowercase hit as the command, not the product', () => {
    // Prose about denied commands is full of `git push` / `docker run`.
    expect(dntViolations('Blockiert git push und docker run', dnt)).toEqual([])
  })

  it('does not match a term inside a longer word', () => {
    expect(dntViolations('Visit GitLab now', dnt)).toEqual([])
  })
})

describe('expansionBudget', () => {
  it('matches the generator table it is copied from', () => {
    expect(expansionBudget(5)).toBe(2.5)
    expect(expansionBudget(20)).toBe(1.9)
    expect(expansionBudget(30)).toBe(1.7)
    expect(expansionBudget(50)).toBe(1.5)
    expect(expansionBudget(70)).toBe(1.35)
    expect(expansionBudget(200)).toBe(1.3)
  })
})

describe('browserBundle', () => {
  const bundle = browserBundle(
    'export const A = 1\nexport function scanDocument() { return A }\n'
    + '// __BROWSER_BUNDLE_CUT__\nexport function browserBundle() { throw new Error("node only") }\n',
  )

  it('strips export keywords so the source runs as a plain script', () => {
    expect(bundle).not.toMatch(/^export /m)
    expect(bundle).toContain('const A = 1')
  })

  it('cuts everything below the marker, keeping Node-only code out of the page', () => {
    expect(bundle).not.toContain('node only')
  })

  it('publishes scanDocument on window', () => {
    expect(bundle).toContain('window.__I18N_SCAN = { scanDocument }')
  })
})

describe('the pseudolocale invariants the scanner depends on', () => {
  it('wraps every catalog value in the delimiters the scanner looks for', () => {
    const bad = VALUES.filter(v => typeof v === 'string' && !(v.startsWith('[') && v.endsWith(']')))
    expect(bad).toEqual([])
  })

  it('never puts a `]…[` seam inside a single value', () => {
    // This is what makes "a seam means two keys were concatenated" sound. If the
    // generator ever emitted a seam of its own, the gate would report every value
    // containing one as a fragment.
    const bad = VALUES.filter(v => typeof v === 'string' && /\][^[\]]*\[/.test(v))
    expect(bad).toEqual([])
  })

  it('leaves no un-accented Latin the scanner would read as a leak', () => {
    // Bounds the gate's own false-positive floor: whatever survives here is text the
    // generator deliberately preserved (placeholders, markup, URLs) and every one of
    // those is excluded by `latinLeaks`. A non-empty result means a NEW preserved
    // shape appeared and the exclusion list has to learn about it.
    const offenders = []
    for (const v of VALUES) {
      if (typeof v !== 'string') continue
      const inner = v.slice(1, -1)
      if (latinLeaks(inner).length) offenders.push(v)
    }
    expect(offenders).toEqual([])
  })
})

describe('scanDocument — an opaque child is skipped, not joined', () => {
  // `scanDocument` normally runs inside the gate's real browser; happy-dom is
  // enough for the RUN-WALK topology under test here because inline-ness falls
  // back to the tag list and `isOpaque` is a `closest()` call. What happy-dom
  // cannot answer (computed display from CSS classes, mono font detection)
  // stays covered by the CI gate itself.
  const mount = (html: string) => {
    document.body.replaceChildren(document.createRange().createContextualFragment(html))
  }
  const pseudoScan = () => scanDocument({ mode: 'pseudo' })

  it('does not charge <kbd> keycap text to the surrounding prose run', () => {
    mount('<div><span>[Þŕèşş ···]</span> <kbd>Ctrl</kbd></div>')
    expect(pseudoScan().filter(f => f.kind === 'latin-leak')).toEqual([])
  })

  it('still reports prose on the far side of an opaque child', () => {
    // The opaque child is skipped, not the rest of the walk: text around it
    // stays in the run and is graded.
    mount('<div><kbd>K</kbd> hardcoded prose</div>')
    const leaks = pseudoScan().filter(f => f.kind === 'latin-leak').map(f => f.detail)
    expect(leaks).toContain('hardcoded')
    expect(leaks).toContain('prose')
  })

  it('an inline [data-i18n-opaque] child is excluded from the run', () => {
    mount(
      '<div><span>[Ĺàbèĺ ···]</span><span data-i18n-opaque>Alt</span></div>')
    expect(pseudoScan().filter(f => f.kind === 'latin-leak')).toEqual([])
  })

  it('an inline wrapper AROUND opaque content is excluded too', () => {
    // The ShortcutRow shape: the keycaps sit inside a plain span container, so
    // the opaque element is a grandchild. The run is built from `textContent`,
    // which reaches through the wrapper — so the wrapper must be skipped.
    mount(
      '<div><span>[Ĵùɱþ ţø çĥàţ ···]</span><span><span>+</span><kbd>Alt</kbd></span></div>')
    expect(pseudoScan().filter(f => f.kind === 'latin-leak')).toEqual([])
  })

  it('a wrapper around a computed-monospace child is excluded too', () => {
    // Mono is the other half of the opaque contract and is not
    // selector-expressible, so the descendant probe must read computed style
    // (the PerformanceTab machine-identity strip: a plain span wrapping a
    // font-mono strong).
    mount(
      '<div><span>[Ĥøşţñàɱè ···]</span><span><strong style="font-family: monospace">Ctrl</strong></span></div>')
    expect(pseudoScan().filter(f => f.kind === 'latin-leak')).toEqual([])
  })

  it('still grades prose inside a wrapper that also carries a keycap', () => {
    // Skipping the wrapper must not exempt the wrapper's own prose: the
    // element loop visits it and grades its text around the keycap.
    mount(
      '<div><span>press <kbd>K</kbd> anytime</span></div>')
    const leaks = pseudoScan().filter(f => f.kind === 'latin-leak').map(f => f.detail)
    expect(leaks).toContain('press')
    expect(leaks).toContain('anytime')
  })

  it('keeps the multi-unit fragment signal across an opaque child', () => {
    // The opaque child is skipped WITHOUT ending the run: a sentence spliced
    // from several catalog keys around a keycap or code term is a real
    // reorder defect (the fix is one key with interpolation), and flushing at
    // the boundary would let it pass unseen.
    mount(
      '<div><span>[Ûñìţ øñè·]</span><kbd>⌘</kbd><span>[Ûñìţ ţŵø·]</span></div>')
    expect(pseudoScan().filter(f => f.signature === 'multi-unit')).toHaveLength(1)
  })
})
