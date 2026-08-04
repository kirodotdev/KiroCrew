/**
 * Render-time i18n scanner — the DOM half of the Phase 5 gate.
 *
 * Every other i18n gate in this repo reads SOURCE (an eslint pass over `src`) or
 * CATALOG JSON. Both are blind to the same two things: a string that is never
 * extracted at all reads as ordinary TypeScript, and a sentence assembled from
 * three catalog keys reads as three correct lookups. Only the rendered page shows
 * either one. This file is that check.
 *
 * It runs against the `en-XA` pseudolocale, where `gen-pseudolocale.mjs` has
 * already given us two properties we can assert on cheaply:
 *
 *   1. every catalog value is wrapped in `[` … `]`, so a `]…[` seam inside one
 *      inline run is a surviving concatenation; and
 *   2. every ASCII letter outside a preserved region is accented, so any plain
 *      Latin letter outside a `[` … `]` unit is text that never reached a catalog.
 *
 * DUAL TARGET. The same source runs in two places, and there is deliberately only
 * one copy of the logic:
 *   - Node, via `import` — `renderScan.test.ts` exercises the pure text functions.
 *   - the browser, via `browserBundle()` — `check-i18n-render.mjs` strips the
 *     `export` keywords and injects the result with `page.addScriptTag`.
 * Because of the second target this file must stay import-free and top-level-side-
 * effect-free, and every export must be a plain `export function` / `export const`
 * that `STRIP_EXPORT` can rewrite. Nothing here may reference module scope that
 * would not survive being pasted into a page.
 */

// ---------------------------------------------------------------------------
// Pseudolocale constants — must match scripts/gen-pseudolocale.mjs
// ---------------------------------------------------------------------------

/** The wrapper `transform()` puts around every catalog value. */
export const UNIT_OPEN = '['
export const UNIT_CLOSE = ']'

/** The padding glyph (U+00B7). Filler, never copy — discounted everywhere. */
export const PSEUDO_PAD = '·'

/**
 * IBM/W3C expansion budget, copied from `gen-pseudolocale.mjs:expansionRatio`.
 * Used to grade an overflow: a 2.4x-wide short label is within the pseudolocale's
 * own padding budget, so it is advisory, while exceeding it is a real finding.
 */
export function expansionBudget(length) {
  if (length <= 10) return 2.5
  if (length <= 20) return 1.9
  if (length <= 30) return 1.7
  if (length <= 50) return 1.5
  if (length <= 70) return 1.35
  return 1.3
}

// ---------------------------------------------------------------------------
// Text-level analysis (pure — unit-tested in Node)
// ---------------------------------------------------------------------------

/**
 * Split rendered text into catalog units and the text outside them.
 *
 * Units are recognised by the wrapper the transform actually guarantees — "starts
 * `[`, ends `]`" — NOT by balanced-bracket depth: real copy carries its own
 * unbalanced brackets (`"[ Paste #"` pseudolocalises to `[[ Þàşţè #···]`) and depth
 * counting misreports it. Two adjacent units are separated by a `]…[` seam.
 *
 * Splitting INSIDE a text node matters: `{t('a')}{t('b')}` gives React two text
 * nodes, but `` {`${t('a')} ${t('b')}`} `` and `t('a') + t('b')` give it one.
 * Counting nodes alone would miss the second shape entirely.
 */
export function splitUnits(text) {
  const first = text.indexOf(UNIT_OPEN)
  const last = text.lastIndexOf(UNIT_CLOSE)
  // No wrapper at all, or a `[` with nothing closing it: none of this came from a
  // catalog, so the whole string is orphan text.
  if (first < 0 || last < first) return { units: [], orphans: text ? [text] : [] }

  const units = []
  const orphans = []
  if (first > 0) orphans.push(text.slice(0, first))
  if (last < text.length - 1) orphans.push(text.slice(last + 1))

  const inner = text.slice(first, last + 1)
  const seam = /\][^[\]]*\[/g
  let cursor = 0
  let m = seam.exec(inner)
  while (m) {
    units.push(inner.slice(cursor, m.index + 1))
    const between = m[0].slice(1, -1)
    if (between) orphans.push(between)
    cursor = m.index + m[0].length - 1
    m = seam.exec(inner)
  }
  units.push(inner.slice(cursor))
  return { units, orphans }
}

/** True when `s` carries no meaning — whitespace and pseudolocale padding only. */
export function isFiller(s) {
  return s.split(PSEUDO_PAD).join('').trim() === ''
}

/**
 * Tokens that are plain Latin in EVERY language and must not count as a leak.
 *
 * Two populations, and they are excluded for opposite reasons:
 *
 *   - DNT terms (`glossary.json`) are proper nouns a translator must leave alone.
 *     Note the asymmetry this creates: `gen-pseudolocale.mjs` does NOT preserve
 *     them, so in `en-XA` they render accented (`GitHub` -> `ĞìţĤùƀ`). A plain
 *     `GitHub` in an en-XA render therefore means the string never went through the
 *     catalog at all — it IS a leak. We exclude them anyway, because the same term
 *     legitimately appears in fixture data and in DNT-by-provenance third-party
 *     metadata, and a gate that cries wolf on the word "GitHub" will be ignored.
 *     Consequence, stated plainly: a hardcoded string whose every word is a DNT
 *     term is invisible to this gate.
 *   - Language endonyms are deliberately never translated (`languages.ts`), and
 *     they all render on `/settings?tab=display`, one of the gated surfaces.
 */
export const ALWAYS_LATIN = [
  // glossary.json `dnt`
  'AWS', 'Discord', 'Docker', 'Git', 'GitHub', 'GitLab', 'JSON', 'Kiro', 'KiroCrew',
  'MCP', 'Markdown', 'Node.js', 'OAuth', 'Playwright', 'Python', 'Slack',
  'TypeScript', 'YAML', 'npm',
  // language endonyms from SUPPORTED_LANGUAGES
  'English', 'Espanol', 'Español', 'Francais', 'Français', 'Portugues', 'Português',
  'Deutsch', 'Italiano', 'Pseudolocale',
]

/**
 * Regions `gen-pseudolocale.mjs` passes through byte-identical, so they are Latin in
 * the pseudolocale BY DESIGN and are not leaks.
 *
 * Same four shapes as that file's `PRESERVE` array, but deliberately in a DIFFERENT
 * ORDER. The generator carves all four in ONE pass where the outermost match wins;
 * stripping them one regex at a time is not equivalent, because a pattern that sits
 * INSIDE another destroys its host. `https://<your-host>` is the live case: strip
 * `<your-host>` first and the surviving `https://` no longer matches the URL
 * pattern, orphaning `https` as a phantom leak. Broadest-first restores
 * outermost-wins for the shapes that actually nest here.
 *
 * `renderScan.test.ts` asserts this over every committed en-XA value, so a new
 * preserved shape fails that test instead of flooding the ledger.
 */
export const PRESERVED_REGIONS = [
  /https?:\/\/\S+/g, // urls — may CONTAIN a <…> or {{…}} region, so strip first
  /\{\{[^}]*\}\}/g, // i18next interpolation
  /\$t\([^)]*\)/g, // i18next nesting
  /<[^>]+>/g, // markup, and the URL-shaped <…> values in this catalog
]

/**
 * Strip everything that is legitimately un-accented, then report the Latin that is
 * left over.
 *
 * Order matters. The `ALWAYS_LATIN` pass runs before the letter-run scan so that
 * `Node.js` cannot leave a bare `js` behind, and the longest terms are removed
 * first so `GitHub` is not half-consumed by `Git`.
 */
export function latinLeaks(text, opts) {
  const minLetters = opts && opts.minLetters ? opts.minLetters : 1
  const allow = (opts && opts.allow) || ALWAYS_LATIN
  let rest = text
  for (const term of [...allow].sort((a, b) => b.length - a.length)) {
    rest = rest.split(term).join(' ')
  }
  // Preserved regions normally sit INSIDE a unit and so never reach this function,
  // but an interpolated value React renders as its own text node arrives here bare.
  for (const rx of PRESERVED_REGIONS) rest = rest.replace(rx, ' ')
  const runs = rest.match(/[A-Za-z]+/g) || []
  return runs.filter(r => r.length >= minLetters)
}

/** Punctuation that, at the START of orphan text, continues the sentence before it. */
const CONTINUATION = /^[)\]}:;,.!?…）】」]/

/**
 * Delimiter pairs checked for a dangling open bracket. `[`/`]` is excluded on
 * purpose: those are the pseudolocale's own wrapper, so every unit carries exactly
 * one of each and counting them says nothing.
 */
const DELIMITER_PAIRS = [['(', ')'], ['{', '}'], ['（', '）'], ['【', '】'], ['「', '」']]

const countOf = (haystack, needle) => haystack.split(needle).length - 1

/**
 * Grade one inline run's text.
 *
 * Returns `{ fragments, leaks }`. `fragments` is non-empty when the run was
 * assembled from more than one catalog unit, or when orphan text continues a
 * sentence a unit started. `leaks` is the plain-Latin text with no catalog origin.
 *
 * Deliberately NOT gated on `units.length > 0`. The predecessor implementation
 * skipped any run with no bracketed unit, which inverted the gate's sensitivity:
 * the more untranslated a surface was, the less it reported, and a component with
 * zero catalog usage produced zero findings. That hole is why the hardcoded
 * `"6m 38s"` duration in `useUptime.ts` survived every gate.
 */
export function gradeRun(text, opts) {
  const { units, orphans } = splitUnits(text)
  const live = orphans.filter(o => !isFiller(o))
  const fragments = []
  const leaks = []

  if (units.length >= 2) {
    fragments.push({ signal: 'multi-unit', detail: `${units.length} catalog units in one run` })
  }

  for (const orphan of live) {
    const trimmed = orphan.trim()
    if (units.length > 0) {
      if (CONTINUATION.test(trimmed)) {
        fragments.push({ signal: 'continuation-punctuation', detail: trimmed.slice(0, 40) })
      }
      for (const [open, close] of DELIMITER_PAIRS) {
        const opened = units.reduce((n, u) => n + countOf(u, open) - countOf(u, close), 0)
        if (opened > 0 && trimmed.includes(close)) {
          fragments.push({ signal: 'dangling-delimiter', detail: `${open}…${close}` })
          break
        }
      }
    }
    for (const run of latinLeaks(orphan, opts)) leaks.push(run)
  }
  return { fragments, leaks, units, orphans: live }
}

/**
 * DNT terms must survive translation byte-identical. Only meaningful in a REAL
 * locale — `en-XA` accents them by construction, so running this against the
 * pseudolocale would report all 19 as mangled.
 */
export function dntViolations(text, terms) {
  const out = []
  for (const term of terms) {
    // Match LOOSELY, then require the hit to be byte-exact. Searching for the exact
    // term would find only the cases that are already correct; the finding we want
    // is the near-miss. Alphanumerics are matched literally and every separator
    // becomes optional, so `Node.js` also matches `Node js` and `NodeJS` — both of
    // which then fail the equality test and are reported.
    const loose = [...term]
      .map(ch => (/[A-Za-z0-9]/.test(ch) ? ch : '[^\\p{L}\\p{N}]?'))
      .join('')
    const rx = new RegExp(`(^|[^\\p{L}\\p{N}])(${loose})([^\\p{L}\\p{N}]|$)`, 'giu')
    let m = rx.exec(text)
    while (m) {
      // An all-lowercase hit is the COMMAND or package, not the product: `git push`,
      // `docker run`, `python -m`, `npm i`. Prose about denied commands is full of
      // them, and calling `git` a mangled `Git` is how a gate earns a blanket
      // suppression. What is left is the class DNT actually protects against —
      // wrong internal capitalisation or spacing: `Github`, `NodeJS`, `Node JS`,
      // `YAml`. A term translated away entirely is invisible to any render check,
      // since the word simply is not there to find.
      //
      // The upper-case twin of that exemption: an ALL-CAPS hit touching `_` is a
      // SCREAMING_SNAKE identifier, where caps is the correct spelling —
      // `PLAYWRIGHT_MCP_EXTENSION_TOKEN`, `KIROCREW_OWNER_ID`. Both are env var
      // names quoted verbatim in settings copy, and every one of the 9 shipped
      // catalogs carries them, so without this the gate opens with 18 findings it
      // must not have. Requiring BOTH the all-caps form and an adjacent underscore
      // keeps a bare `PLAYWRIGHT` in prose reportable.
      const upperIdent = m[2] === term.toUpperCase() && (m[1] === '_' || m[3] === '_')
      if (m[2] !== term && m[2] !== term.toLowerCase() && !upperIdent) {
        out.push({ term, found: m[2] })
      }
      m = rx.exec(text)
    }
  }
  return out
}

// ---------------------------------------------------------------------------
// DOM traversal (browser-only — `scanDocument` runs inside the page)
// ---------------------------------------------------------------------------

/**
 * Elements text flows through, so their content joins the surrounding run.
 *
 * Seeded from the tag set the happy-dom predecessor used, but in a real browser we
 * prefer the computed `display`, which is correct for CSS-overridden inline-ness
 * and for `display:contents`. The tag list survives only as the fallback for
 * elements with no box at all.
 */
const INLINE_TAGS = 'SPAN STRONG B EM I CODE A SMALL KBD SAMP MARK U ABBR SUB SUP LABEL TIME BDI BDO SVG IMG Q CITE VAR DFN S DEL INS WBR BR'

/** Attributes that carry user-visible prose but never appear in the text flow. */
const TEXT_ATTRS = ['title', 'aria-label', 'placeholder', 'alt', 'aria-placeholder']

/**
 * Walk the document and return every render-time i18n finding.
 *
 * Runs inside the page. `opts` arrives structured-cloned, so it may hold only
 * JSON-shaped values.
 *
 * @param {{
 *   mode: 'pseudo' | 'real',
 *   minLetters?: number,
 *   allow?: string[],
 *   dnt?: string[],
 *   maxFindings?: number,
 * }} opts
 */
export function scanDocument(opts) {
  const mode = opts.mode
  const inlineTags = new Set(INLINE_TAGS.split(' '))
  const findings = []
  const push = f => {
    if (findings.length < (opts.maxFindings || 4000)) findings.push(f)
  }

  // -- shared predicates ----------------------------------------------------

  const visible = el => {
    if (typeof el.checkVisibility !== 'function') return true
    return el.checkVisibility({
      contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true,
    })
  }

  /**
   * Opaque-data subtrees. Code, terminal output, file paths and hashes are Latin in
   * every language, and the mono font stack is how this codebase marks them. Read
   * the COMPUTED family, not the class list: `.msg-content pre` sets the font in
   * `index.css`, where no `font-mono` token appears.
   */
  const isOpaque = el => {
    if (el.closest('code, pre, kbd, samp, [data-i18n-opaque]')) return true
    const fam = getComputedStyle(el).fontFamily || ''
    return /mono/i.test(fam)
  }

  /**
   * Virtuoso sizes its inner container past its viewport by design, so its
   * scrollWidth/scrollHeight carry no information about text fit.
   */
  const inVirtualList = el => !!el.closest('[data-testid="virtuoso"]')

  const describe = el => {
    const parts = []
    let cur = el
    for (let i = 0; i < 4 && cur && cur !== document.body; i++) {
      const id = cur.getAttribute && cur.getAttribute('data-testid')
      let seg = cur.tagName.toLowerCase()
      if (id) seg += `[${id}]`
      else if (cur.parentElement) {
        const sibs = [...cur.parentElement.children].filter(c => c.tagName === cur.tagName)
        if (sibs.length > 1) seg += `:nth(${sibs.indexOf(cur)})`
      }
      parts.unshift(seg)
      cur = cur.parentElement
    }
    return parts.join('>')
  }

  const clip = s => (s.length > 90 ? `${s.slice(0, 87)}…` : s)

  /**
   * Where in the SOURCE this element came from — `[{ file, line }, ...]`, nearest
   * first, empty when it cannot be determined.
   *
   * A DOM path (`div:nth(1)>div>div:nth(1)>button:nth(0)`) says where a finding is on
   * screen and nothing about where to edit. The DEV bundle this gate is required to
   * render already carries the answer: the JSX dev transform bakes a `fileName` /
   * `lineNumber` into every element, and React hangs it on the fiber as
   * `_debugSource`. Measured on this build: 14,005 of them.
   *
   * The chain matters, not just the leaf, but the LEAF COMES FIRST. Measured both ways:
   * for a string written inline — `<span title="Filter these runs">` in
   * `ProjectsPage.tsx:355` — the element's own `_debugSource` is already the answer, and
   * `_debugOwner` walks OUTWARD into route and app wrappers (`App.tsx:2223`,
   * `BuiltinAppRoute.tsx:28`) that tell the author nothing. For a string passed into a
   * shared component — `"Chat"` rendering from `components/settings.tsx:241` but written
   * at `pages/settings/DisplayPanel.tsx:167` — the answer is one hop out. Inline is much
   * the commoner case, so the leaf leads and the owners follow as `via` lines; the
   * second entry is the answer in the pass-through case.
   *
   * Deliberately NOT using `fiber.type.name` for a component name: this is a build,
   * so those are minified (`<ySt>`, `<bhe>`). The file/line survives because it is a
   * string literal the transform wrote, not an identifier the minifier renames.
   *
   * `_debugSource` is React-internal and React 19 removes it (this repo is on 18.3.1).
   * When it is gone this returns `[]` and the caller SAYS so rather than quietly
   * falling back to the DOM path, because a gate that silently loses its most useful
   * output is how the DOM-path-only version survived this long.
   */
  const sourceChain = el => {
    let fiber = null
    for (const k in el) {
      if (k.charCodeAt(0) === 95 && k.startsWith('__reactFiber$')) { fiber = el[k]; break }
    }
    const chain = []
    let guard = 0
    while (fiber && chain.length < 3 && guard++ < 60) {
      const src = fiber._debugSource
      if (src && src.fileName) {
        // Absolute paths are baked in at build time, so they name the BUILD machine's
        // layout ("/local/home/.../kc-minw0/website/src/..."). Repo-relative is what a
        // reader can act on and all a CI log should disclose.
        const file = String(src.fileName).replace(/^.*?\/website\//, '')
        const entry = `${file}:${src.lineNumber}`
        if (!chain.includes(entry)) chain.push(entry)
      }
      fiber = fiber._debugOwner
    }
    return chain
  }

  /** Both coordinates of a finding: where it is on screen, and where it is in git. */
  const at = el => ({ path: describe(el), source: sourceChain(el) })

  // -- 1/2. inline runs: fragments + plain-Latin leaks -----------------------

  const isInline = node => {
    if (node.nodeType === 3) return true
    if (node.nodeType !== 1) return false
    if (!visible(node)) return false
    const d = getComputedStyle(node).display
    if (d === 'contents') return true
    return d.startsWith('inline') || inlineTags.has(node.tagName)
  }

  for (const el of document.body.querySelectorAll('*')) {
    if (!visible(el) || inVirtualList(el) || isOpaque(el)) continue

    // Group DIRECT children into maximal inline runs; a block child ends the run.
    let run = []
    const flush = () => {
      if (!run.length) return
      const text = run.map(n => (n.nodeType === 3 ? n.textContent : n.textContent || '')).join('')
      run = []
      if (!text.trim()) return
      const graded = gradeRun(text, opts)
      if (mode === 'pseudo') {
        for (const frag of graded.fragments) {
          push({
            kind: 'fragment', signature: frag.signal, ...at(el),
            text: clip(text), detail: frag.detail,
            fix: frag.signal === 'multi-unit'
              ? 'Merge the adjacent catalog keys into one key with {{vars}} — see AGENTS.md "merge, not join".'
              : 'Move the orphan punctuation/word inside the catalog value.',
          })
        }
        for (const leak of graded.leaks) {
          push({
            kind: 'latin-leak', signature: 'untranslated-text', ...at(el),
            text: clip(text), detail: leak,
            fix: `Extract "${leak}" into the catalog and render it with t().`,
          })
        }
      }
    }
    for (const node of el.childNodes) {
      if (isInline(node)) run.push(node)
      else flush()
    }
    flush()

    // Attribute prose is outside the text flow, so the run walk above cannot see
    // it. This is the class that hid ThinkingBlock's English title/aria-label.
    for (const attr of TEXT_ATTRS) {
      const v = el.getAttribute && el.getAttribute(attr)
      if (!v || !v.trim()) continue
      if (mode !== 'pseudo') continue
      const graded = gradeRun(v, opts)
      for (const leak of graded.leaks) {
        push({
          kind: 'latin-leak', signature: 'untranslated-attribute', ...at(el),
          text: clip(`@${attr}=${v}`), detail: leak,
          fix: `Extract the ${attr} text into the catalog and render it with t().`,
        })
      }
    }
  }

  // -- 3. DNT integrity (real locales only) ---------------------------------

  if (mode === 'real' && opts.dnt && opts.dnt.length) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const parent = n.parentElement
      if (!parent || !visible(parent) || isOpaque(parent)) continue
      const text = (n.textContent || '').normalize('NFC')
      if (!text.trim()) continue
      // Prose only. A text node that is a single whitespace-free token is an
      // IDENTIFIER — an agent name, a slug, a filename, a config key — and the
      // casing convention there is the machine's, not the translator's. Without
      // this, the agent named `kirocrew` reads as a mangled `KiroCrew` on every
      // surface that lists it. The accepted blind spot: a genuinely mistranslated
      // DNT term standing alone in its own element is missed.
      if (!/\s/.test(text.trim())) continue
      for (const v of dntViolations(text, opts.dnt)) {
        push({
          kind: 'dnt', signature: 'dnt-mangled', ...at(parent),
          text: clip(text), detail: `${v.term} rendered as ${v.found}`,
          fix: `Restore "${v.term}" verbatim in this catalog value — it is on the do-not-translate list.`,
        })
      }
    }
  }

  // -- 4. overflow, with the prescriptive signature ------------------------

  /** Nearest ancestor whose width cannot give, and why. */
  const fixedAncestor = el => {
    let cur = el.parentElement
    for (let i = 0; i < 12 && cur && cur !== document.body; i++) {
      const cs = getComputedStyle(cur)
      if (/px$/.test(cs.flexBasis)) return `${describe(cur)} (flex-basis:${cs.flexBasis})`
      if (/px$/.test(cs.maxWidth)) return `${describe(cur)} (max-width:${cs.maxWidth})`
      if (/px$/.test(cs.minWidth) && cs.minWidth !== '0px') return `${describe(cur)} (min-width:${cs.minWidth})`
      if (cs.width === cs.maxWidth && /px$/.test(cs.width)) return `${describe(cur)} (fixed width:${cs.width})`
      cur = cur.parentElement
    }
    return ''
  }

  /** Longest unbreakable token, measured with a Range rather than guessed. */
  const longestTokenWidth = el => {
    const node = [...el.childNodes].find(n => n.nodeType === 3 && n.textContent.trim())
    if (!node) return 0
    const text = node.textContent
    let best = 0
    const rx = /\S+/g
    let m = rx.exec(text)
    const range = document.createRange()
    while (m) {
      range.setStart(node, m.index)
      range.setEnd(node, m.index + m[0].length)
      best = Math.max(best, range.getBoundingClientRect().width)
      m = rx.exec(text)
    }
    range.detach && range.detach()
    return best
  }

  for (const el of document.body.querySelectorAll('*')) {
    if (!visible(el) || inVirtualList(el) || isOpaque(el)) continue
    const cs = getComputedStyle(el)
    // The element is itself a scroll container, so horizontal overflow is the
    // designed behaviour, not a layout failure.
    if (cs.overflowX === 'auto' || cs.overflowX === 'scroll') continue
    // Only elements that own text directly. Without this every ancestor of one
    // overflowing leaf reports the same finding.
    const own = [...el.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim())
    if (!own.length) continue

    // Visually-hidden patterns — skip links, `sr-only`, collapsed readouts — are
    // clamped to ~1px or pushed off-screen rather than `display:none`, so
    // checkVisibility() correctly reports them as visible. Measuring them yields a
    // 1px box "overflowing" by 196x: pure noise that drowns the real findings.
    const rect = el.getBoundingClientRect()
    if (el.clientWidth < 8 || el.clientHeight < 4) continue
    if (rect.right < 0 || rect.bottom < 0) continue

    const client = Math.max(el.clientWidth, 1)
    // scrollWidth rounds up while clientWidth truncates, so sub-pixel layouts
    // report a 1px delta with nothing wrong.
    const overflowing = el.scrollWidth - el.clientWidth > 1
    const ratio = el.scrollWidth / client
    const source = own.map(n => n.textContent).join('').trim()
    const budget = expansionBudget(source.length)

    const clips = cs.overflowX === 'hidden' || cs.overflowX === 'clip'
    const ellipsis = cs.textOverflow === 'ellipsis'
    const named = !!(el.title || el.getAttribute('aria-label') || el.closest('[title]'))

    // The highest-value signature in this codebase, and it does not need the
    // element to be overflowing YET: an ellipsis on a flex child whose min-width
    // is still `auto` silently does nothing (CSS Flexbox 4.5), so the text will
    // push its sibling out instead of truncating.
    const parentCs = el.parentElement ? getComputedStyle(el.parentElement) : null
    if (ellipsis && parentCs && /flex/.test(parentCs.display) && cs.minWidth === 'auto') {
      push({
        kind: 'layout', signature: 'ellipsis-with-flex-parent', ...at(el),
        text: clip(source), ratio: Number(ratio.toFixed(2)), fixedAncestor: fixedAncestor(el),
        fix: 'Add `min-w-0` to this flex child — text-overflow:ellipsis cannot apply while min-width is auto.',
      })
    }

    if (!overflowing) continue

    let signature = clips ? 'clipped-without-title' : 'whole-toolbar-overflow'
    let fix = 'Let the container grow, or wrap the label.'
    if (clips && named) {
      // Truncation with the full text reachable is a legitimate pattern — 325
      // `truncate` sites rely on it. Only advisory, and only past budget.
      if (ratio <= budget) continue
      signature = 'over-budget-truncation'
      fix = `Truncation is fine, but ${ratio.toFixed(2)}x exceeds the ${budget}x budget for a ${source.length}-char source — shorten the source or widen the container.`
    } else if (clips) {
      fix = 'Text is silently clipped with no `title`/`aria-label` carrying the full string — add one, or let it wrap.'
    } else if (longestTokenWidth(el) > client && cs.overflowWrap === 'normal' && cs.wordBreak === 'normal') {
      signature = 'unbreakable-token'
      fix = 'One token is wider than the box; no wrapping can fix it. Add `overflow-wrap:anywhere`.'
    } else if (cs.whiteSpace === 'nowrap' || cs.whiteSpace === 'pre') {
      signature = 'nowrap-overflow'
      fix = 'Remove `whitespace-nowrap`, or give the container room for the longest locale.'
    }
    push({
      kind: 'layout', signature, ...at(el), text: clip(source),
      ratio: Number(ratio.toFixed(2)), budget, fixedAncestor: fixedAncestor(el), fix,
    })
  }

  // Portaled overlays (nav flyouts, composer dropdowns) are `position:fixed` and
  // `nowrap`: they overflow the VIEWPORT, not a parent, so scrollWidth never fires.
  for (const el of document.body.querySelectorAll('*')) {
    if (!visible(el) || isOpaque(el)) continue
    if (getComputedStyle(el).position !== 'fixed') continue
    const r = el.getBoundingClientRect()
    if (r.width === 0) continue
    if (r.right <= window.innerWidth + 1 && r.left >= -1) continue
    push({
      kind: 'layout', signature: 'fixed-overlay-off-viewport', ...at(el),
      text: clip((el.textContent || '').trim()),
      ratio: Number((r.width / window.innerWidth).toFixed(2)), fixedAncestor: '',
      fix: 'A fixed overlay extends past the viewport in this locale — clamp its max-width or flip its anchor side.',
    })
  }

  return findings
}

// __BROWSER_BUNDLE_CUT__

// ---------------------------------------------------------------------------
// Browser bundling (Node-only — everything below the cut is excluded from the
// injected script)
// ---------------------------------------------------------------------------

/**
 * Rewrite this module into a plain script for `page.addScriptTag`.
 *
 * `page.evaluate` cannot close over module scope, so the analysis has to reach the
 * page somehow. Pasting a second copy into a template string is how the two halves
 * drift apart — instead we take THIS file's own source, strip the `export`
 * keywords, and cut everything below the marker (which is Node-only and whose own
 * body contains the rewriting regex). One implementation, two runtimes.
 */
export function browserBundle(source) {
  const body = source.split('// __BROWSER_BUNDLE_CUT__')[0]
    .replace(/^export (function|const|class)/gm, '$1')
  return `(() => {\n${body}\nwindow.__I18N_SCAN = { scanDocument };\n})()`
}

