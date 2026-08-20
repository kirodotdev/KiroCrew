import { readdir, readFile } from 'node:fs/promises'
import { join } from 'node:path'

// The narrow-first baseline is a convention, and a convention with no executable
// guard drifts back. These three assertions each pin a failure this sweep actually
// hit, not a hypothetical one.

const SRC = join(__dirname, '..')
const SKIP = new Set(['test', 'node_modules', '__snapshots__'])

async function* walkSource(dir: string): AsyncGenerator<string> {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (SKIP.has(entry.name)) continue
      yield* walkSource(join(dir, entry.name))
    } else if (/\.tsx?$/.test(entry.name)) {
      yield join(dir, entry.name)
    }
  }
}

/**
 * Every source file under `src/`, read ONCE and shared by the assertions below.
 *
 * Two things here are load-bearing, and both were measured on this tree (1285
 * files):
 *
 *  1. **Read once, not once per test.** Each assertion below used to walk and
 *     read the whole tree itself, so it was read four times over plus a fifth
 *     pass for `src/apps`.
 *  2. **Reads are CONCURRENT.** `for (const f of files) await readFile(f)` takes
 *     **12.7s** here even with a warm page cache, while the same reads issued
 *     together take **209ms** — a 60x gap, because the cost is per-file I/O
 *     latency rather than throughput, and awaiting in a loop serializes it. That
 *     serialization is what pushed these tests past the 15s per-test budget on a
 *     Windows checkout, where per-file latency is higher: they failed for every
 *     Windows contributor while passing on CI's Linux runner.
 *
 * Contents are normalized to LF because these assertions match multi-line shapes
 * and split on '\n', which a CRLF checkout would otherwise break independently
 * of the timing.
 *
 * A module-level promise rather than a `beforeAll`, so the work happens once per
 * FILE and every test simply awaits the same result.
 */
const SOURCES: Promise<ReadonlyArray<{ file: string; src: string }>> = (async () => {
  const files: string[] = []
  for await (const file of walkSource(SRC)) files.push(file)
  // Read in bounded windows rather than one `Promise.all` over all ~1288 files:
  // an unbounded fan-out opens that many descriptors at once, which can exceed
  // the per-process limit on a constrained runner. A window of 64 keeps almost
  // all of the concurrency win (the 60x over serial is gone by ~8-16 in flight)
  // while capping open descriptors.
  const WINDOW = 64
  const out: { file: string; src: string }[] = []
  for (let i = 0; i < files.length; i += WINDOW) {
    const batch = await Promise.all(
      files.slice(i, i + WINDOW).map(async (file) => ({
        file,
        src: (await readFile(file, 'utf8')).replace(/\r\n/g, '\n'),
      })),
    )
    out.push(...batch)
  }
  return out
})()

describe('narrow-first layout baseline', () => {
  it('never puts two conflicting horizontal paddings at the SAME breakpoint', async () => {
    // A literal sweep left `px-2 md:px-2 md:px-6` behind on one page. Both `md:`
    // rules are live at that breakpoint, so which one wins is decided by the
    // ORDER TAILWIND EMITS THEM in the stylesheet, not by the order in the
    // attribute -- and these are plain className strings, so twMerge is not
    // there to collapse them. It happened to resolve to the intended 24px
    // because Tailwind sorts by scale value, which means the desktop gutter was
    // being held by an implementation detail rather than by the code saying so.
    //
    // Scans EVERY string literal, not just `className=` attributes: class lists
    // in this repo are routinely held in module consts (`PANE_SHELL_CLASS` in the
    // two files this very change edits), and an attribute-only matcher cannot see
    // those. Widening only adds candidates -- a candidate fails solely on a real
    // same-breakpoint collision.
    const offenders: string[] = []
    for (const { file, src } of await SOURCES) {
      for (const m of src.matchAll(/"([^"\n]*)"|'([^'\n]*)'|`([^`]*)`/g)) {
        const cls = m[1] ?? m[2] ?? m[3] ?? ''
        for (const prefix of ['md:', 'sm:', 'lg:', 'xl:']) {
          const hits = [...cls.matchAll(new RegExp(`(?<![\\w:-])${prefix}px-[\\d.]+`, 'g'))]
          if (hits.length > 1) {
            offenders.push(`${file.replace(SRC, 'src')}: ${hits.map(h => h[0]).join(' + ')}`)
          }
        }
      }
    }
    expect(offenders, 'two paddings at one breakpoint: the winner is emit order, not intent')
      .toEqual([])
  })

  it('keeps the baseline narrow-first -- no `max-md:` reaching back for the phone', async () => {
    // `max-md:` is the tell of a desktop-first rule: it means the unprefixed
    // value was written for the desktop and the phone is being treated as the
    // exception. That shape is what forced every narrow fix to pair an override
    // with a hand-synchronized negative margin somewhere else.
    const offenders: string[] = []
    for (const { file, src } of await SOURCES) {
      if (/\bmax-(?:md|sm|lg):/.test(src)) offenders.push(file.replace(SRC, 'src'))
    }
    expect(offenders, 'write `foo md:bar` instead: unprefixed is the phone')
      .toEqual([])
  })

  it('never half-converts a file: no bare px-6 left where the narrow gutter landed', async () => {
    // The original sweep matched the CONTAINER SIGNATURE (`px-6 pb-8`) rather than
    // the gutter VALUE, so sibling rows in the same page shells kept their 24px
    // while the header and content moved to the narrow gutter -- five rows across four
    // already converted files, plus a seventh page carrying the same
    // `${embedded ? '' : 'px-6'}` template-literal form the sweep claimed to cover.
    //
    // Scope is deliberately per-file rather than repo-wide: a bare `px-6` in an
    // UNCONVERTED file is usually legitimate (a centered empty state, a modal
    // header, an app shell with its own gutter). What is never legitimate is one
    // file holding both spellings, because that is a visible misalignment between
    // a header and the rows under it.
    const offenders: string[] = []
    for (const { file, src } of await SOURCES) {
      if (!src.includes('px-4 md:px-6')) continue
      const stripped = src.replace(/(?<![\w:-])(?:md|sm|lg|xl):px-6/g, '')
      for (const [i, line] of stripped.split('\n').entries()) {
        if (/(?<![\w:-])px-6/.test(line)) {
          offenders.push(`${file.replace(SRC, 'src')}:${i + 1}`)
        }
      }
    }
    expect(
      offenders,
      'this file already uses the narrow gutter; a bare px-6 here misaligns it',
    ).toEqual([])
  })

  it('leaves only centered placeholders holding a bare px-6 in the builtin apps', async () => {
    // The app sweep converted page gutters and deliberately did NOT convert
    // centered empty states, where `px-6` is the element's ONLY inset: flushing
    // it to 8px pushes centered copy toward the screen edge for no width gain,
    // because the copy is already narrower than the pane. Stating that here is
    // the point -- without it, the next pass reads those four lines as misses
    // and "finishes" a sweep that was already complete.
    //
    // The rule is per-LINE rather than per-file, unlike the half-conversion
    // check above, because these placeholders legitimately sit in files that
    // carry no gutter at all. A pill's own padding (`rounded-full px-6`) is not
    // a gutter either.
    const offenders: string[] = []
    const appsRoot = join(SRC, 'apps')
    for (const { file, src } of (await SOURCES).filter(s => s.file.startsWith(appsRoot))) {
      const stripped = src.replace(/(?<![\w:-])(?:md|sm|lg|xl):px-6/g, '')
      for (const [i, line] of stripped.split('\n').entries()) {
        if (!/(?<![\w:-])px-6/.test(line)) continue
        const centered = /items-center/.test(line)
          && /justify-center/.test(line)
          && /text-center/.test(line)
        if (!centered && !/rounded-full/.test(line)) {
          offenders.push(`${file.replace(SRC, 'src')}:${i + 1}`)
        }
      }
    }
    expect(
      offenders,
      'a bare px-6 in a builtin app is a 24px phone gutter: write `px-2 md:px-6`',
    ).toEqual([])
  })

  it('keeps the page title in the SAME column as the content it labels', async () => {
    // The title belongs to the content column, not to the chrome above it: it shares
    // its left edge with the cards and rows beneath it, so those all read as one
    // column. An earlier round tried the opposite -- matching the top bar's 20px --
    // and it read worse, because the title then sat 12px inside the very cards it
    // labels. The doc is what the next page is copied from, so the two are pinned
    // to each other rather than to two independent literals.
    const ui = await readFile(join(SRC, 'components', 'ui.tsx'), 'utf8')
    const header = ui.match(/px-(\d+(?:\.\d+)?) md:px-(\d+(?:\.\d+)?) pt-2 pb-3/)
    expect(header, 'PageHeader should carry a narrow-first horizontal gutter').toBeTruthy()

    const doc = await readFile(join(SRC, '..', 'docs', 'page-layout.md'), 'utf8')
    const skeleton = doc.match(/px-(\d+(?:\.\d+)?) md:px-(\d+(?:\.\d+)?) pb-8 overflow-y-auto/)
    expect(skeleton, 'page-layout.md should show the container gutter in its skeleton').toBeTruthy()

    expect(
      [header![1], header![2]],
      `PageHeader px-${header![1]}/md:px-${header![2]} vs the documented container `
        + `px-${skeleton![1]}/md:px-${skeleton![2]} -- a header that does not share the `
        + 'container gutter insets the title from the content below it',
    ).toEqual([skeleton![1], skeleton![2]])
  })

  it('leaves the top bar left cluster without a redundant mobile inset', async () => {
    // `.tb-left`'s icon buttons carry their own 8px inside the header's inset, so a
    // mobile-only `px-2` on the cluster stacked to push the hamburger out past the
    // page's own left edge, which is what made it read as indented on every page.
    // The RIGHT cluster keeps its own padding/negative-margin pair, which exists to
    // stop the notification badge's 4px overhang being clipped.
    const app = await readFile(join(SRC, 'App.tsx'), 'utf8')
    const cluster = app.match(/className=[^\n]*tb-left[^\n]*/)
    expect(cluster, 'App.tsx should render the tb-left cluster').toBeTruthy()
    expect(
      cluster![0],
      'a mobile-only inset here stacks on the header and pushes the hamburger out',
    ).not.toMatch(/isMobile[^\n]*px-/)
  })

  it('keeps the chat transcript on the same gutter as a page', async () => {
    // The doc's claim is that one vertical line runs through the whole app: the
    // hamburger glyph, a page title, a page row, a card's left edge and the agent's
    // own text. Chat is the surface the rest was lined up WITH, so its gutter and
    // `PageHeader`'s are one number -- asserted across the two files rather than as
    // two literals, because a drift here is invisible to every other check: both
    // sides still render, nothing overflows, and only the eye sees the step.
    const chat = await readFile(join(SRC, 'pages', 'ChatPage.tsx'), 'utf8')
    const row = chat.match(/className=\{`px-(\d+(?:\.\d+)?) mx-auto w-full py-1`\}/)
    expect(row, 'ChatPage should render its message rows with an explicit gutter').toBeTruthy()

    const input = await readFile(join(SRC, 'components', 'ChatInput.tsx'), 'utf8')
    const composer = input.match(/`input-area px-(\d+(?:\.\d+)?) pb-1 /)
    expect(composer, 'ChatInput should give the composer an explicit gutter').toBeTruthy()

    const ui = await readFile(join(SRC, 'components', 'ui.tsx'), 'utf8')
    const gutter = ui.match(/px-(\d+(?:\.\d+)?) md:px-\d+(?:\.\d+)? pt-2 pb-3/)
    expect(gutter, 'PageHeader should carry a narrow-first gutter').toBeTruthy()

    expect(
      [row![1], composer![1]],
      `transcript px-${row![1]} / composer px-${composer![1]} vs the page gutter `
        + `px-${gutter![1]} -- chat and a page would read as two different columns`,
    ).toEqual([gutter![1], gutter![1]])

    // Pinning members by name is what let this drift twice. Round 2 lost a row because the
    // scan wanted `pt-2`/`pb-2` and the row said `py-2`; Round 4 lost nine wrappers because
    // the scan named two sites; Round 5 lost four more because the pattern demanded
    // `px-N mx-auto w-full` ADJACENT and in that order, so `px-5 py-1 mx-auto w-full` and
    // `mx-auto w-full px-5` both walked straight through it. Every one of those was a new
    // SPELLING of the same element, so stop matching spellings: split each class list into
    // tokens and ask what it IS -- a self-centring full-width wrapper -- then require its
    // gutter to be the page's. Order, interleaving and future siblings are all covered.
    //
    // But `mx-auto w-full` on its own is a generic centring idiom, not proof of chat-column
    // membership: a `max-w-*` modal body at `px-6` centres itself the same way and would
    // fail an assertion about "the chat content column" for the wrong reason -- and the
    // tempting dodge is to split the tokens, the exact evasion this guard was rewritten to
    // close. So key the scan on `--mc-content-width`, the variable that actually SIZES the
    // column (the wrappers all carry `style={{ maxWidth: 'var(--mc-content-width, ...)' }}`
    // right next to the class list): a wrapper counts only if that variable appears within
    // NEAR chars of its className. Measured over the current tree this covers 18 of the 19
    // px-carrying wrappers; the one it misses is the composer's `input-area` in
    // ChatInput.tsx, which the transcript==composer test above already pins by name, so
    // narrowing loses no coverage. It also stops three wrappers that carry `mx-auto w-full`
    // with no content-width var at all (WelcomeView, auto-improvement/AutoImprovementPage,
    // one ChatPage node) from failing for the wrong reason if they ever gain a non-standard
    // `px-*` -- they are centred, but they are not the chat content column.
    const CONTENT_WIDTH_VAR = '--mc-content-width'
    const NEAR = 200
    const offenders: string[] = []
    for (const { file, src } of await SOURCES) {
      for (const m of src.matchAll(/className=(?:"([^"]*)"|\{`([^`]*)`\})/g)) {
        const tokens = (m[1] ?? m[2] ?? '').split(/\s+/)
        if (!tokens.includes('mx-auto') || !tokens.includes('w-full')) continue
        const near = src.slice(Math.max(0, m.index! - NEAR), m.index! + m[0].length + NEAR)
        if (!near.includes(CONTENT_WIDTH_VAR)) continue
        const px = tokens.find((t) => /^px-\d+(?:\.\d+)?$/.test(t))
        if (px && px !== `px-${gutter![1]}`) {
          offenders.push(`${file.slice(SRC.length + 1)}: ${px}`)
        }
      }
    }
    expect(
      offenders,
      `these wrappers centre themselves in the chat content column (sized by `
        + `${CONTENT_WIDTH_VAR}) but do not carry its gutter (px-${gutter![1]}), so they `
        + `render a second left edge inside one column`,
    ).toEqual([])
  })

  it('lands the top bar glyphs on the page gutter, derived not hand-typed', async () => {
    // The hamburger, the page title and every card's left edge read as one vertical
    // line. That line is arithmetic across three files, and what has to land on it is
    // the glyph's INK, not the button's box: `Menu` is the one icon here whose artwork
    // does not fill its viewBox, so a box sitting correctly on the gutter still draws
    // 2.5px right of it. Asserted as a SUM rather than as literals, because every part
    // of this failure is silent -- moving any one number just makes the chrome look
    // indented, which no overflow or scroll assertion can see.
    //
    // The icon's own inset is re-derived from lucide's shipped path data rather than
    // hand-typed, so upgrading lucide to a `Menu` drawn on different coordinates fails
    // here instead of quietly making the correction wrong.
    const app = await readFile(join(SRC, 'App.tsx'), 'utf8')
    const header = app.match(/topbar topbar-glass relative pl-(\d+(?:\.\d+)?) /)
    expect(header, 'App.tsx should give the topbar an explicit left inset').toBeTruthy()
    const btn = app.match(/className="p-(\d+(?:\.\d+)?) rounded-md bg-transparent[^\n]*aria-label=\{i18nT\('app\.open_menu'\)\}/)
      ?? app.match(/p-(\d+(?:\.\d+)?) rounded-md bg-transparent border-none cursor-pointer text-muted hover:text-text shrink-0/)
    expect(btn, 'the hamburger should carry its own padding').toBeTruthy()

    const glyph = app.match(/<Menu size=\{(\d+)\} className="-translate-x-\[(\d+(?:\.\d+)?)px\]" \/>/)
    expect(
      glyph,
      'the hamburger glyph should declare its size and its optical correction together',
    ).toBeTruthy()
    const [, sizePx, correction] = glyph!

    const menuIcon = await readFile(
      join(SRC, '..', 'node_modules', 'lucide-react', 'dist', 'esm', 'icons', 'menu.js'),
      'utf8',
    )
    const startXs = [...menuIcon.matchAll(/d: "M(\d+(?:\.\d+)?)/g)].map((m) => Number(m[1]))
    expect(startXs.length, "lucide's menu icon should expose its path data").toBeGreaterThan(0)
    // lucide's default stroke is 2 units with a round cap, so the ink reaches half a
    // stroke beyond the geometry; the viewBox is 24 units wide at any rendered size.
    const inkInsetPx = (Math.min(...startXs) - 1) * (Number(sizePx) / 24)

    const ui = await readFile(join(SRC, 'components', 'ui.tsx'), 'utf8')
    const gutter = ui.match(/px-(\d+(?:\.\d+)?) md:px-\d+(?:\.\d+)? pt-2 pb-3/)
    expect(gutter, 'PageHeader should carry a narrow-first gutter').toBeTruthy()

    const px = (rem: string) => Number(rem) * 4
    const boxLeft = px(header![1]) + px(btn![1])
    const inkLeft = boxLeft - Number(correction) + inkInsetPx
    expect(
      inkLeft,
      `topbar pl-${header![1]} + hamburger p-${btn![1]} puts the button box at ${boxLeft}px; `
        + `less the ${correction}px correction plus Menu's own ${inkInsetPx}px of empty box, `
        + `the GLYPH lands at ${inkLeft}px, but the page gutter is px-${gutter![1]} `
        + `(${px(gutter![1])}px) -- the chrome would read as indented from the title`,
    ).toBe(px(gutter![1]))
  })
})
