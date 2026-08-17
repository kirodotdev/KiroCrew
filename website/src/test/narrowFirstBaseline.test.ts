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
    for await (const file of walkSource(SRC)) {
      const src = await readFile(file, 'utf8')
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
    for await (const file of walkSource(SRC)) {
      const src = await readFile(file, 'utf8')
      if (/\bmax-(?:md|sm|lg):/.test(src)) offenders.push(file.replace(SRC, 'src'))
    }
    expect(offenders, 'write `foo md:bar` instead: unprefixed is the phone')
      .toEqual([])
  })

  it('never half-converts a file: no bare px-6 left where the narrow gutter landed', async () => {
    // The original sweep matched the CONTAINER SIGNATURE (`px-6 pb-8`) rather than
    // the gutter VALUE, so sibling rows in the same page shells kept their 24px
    // while the header and content moved to 8px -- five rows across four already
    // converted files, plus a seventh page carrying the same
    // `${embedded ? '' : 'px-6'}` template-literal form the sweep claimed to cover.
    //
    // Scope is deliberately per-file rather than repo-wide: a bare `px-6` in an
    // UNCONVERTED file is usually legitimate (a centered empty state, a modal
    // header, an app shell with its own gutter). What is never legitimate is one
    // file holding both spellings, because that is a visible misalignment between
    // a header and the rows under it.
    const offenders: string[] = []
    for await (const file of walkSource(SRC)) {
      const src = await readFile(file, 'utf8')
      if (!src.includes('px-2 md:px-6')) continue
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
    // `.tb-left`'s icon buttons carry their own 8px inside the header's 12px, so a
    // mobile-only `px-2` on the cluster stacked to push the hamburger GLYPH out to
    // 28px -- 20px right of the page title, which is what made it read as indented
    // on every page. Measured at 390px and 320px: glyph 28px -> 20px with the class
    // gone. The RIGHT cluster keeps its own padding/negative-margin pair, which
    // exists to stop the notification badge's 4px overhang being clipped.
    const app = await readFile(join(SRC, 'App.tsx'), 'utf8')
    const cluster = app.match(/className=[^\n]*tb-left[^\n]*/)
    expect(cluster, 'App.tsx should render the tb-left cluster').toBeTruthy()
    expect(
      cluster![0],
      'a mobile-only inset here stacks on the header and pushes the hamburger out',
    ).not.toMatch(/isMobile[^\n]*px-/)
  })
})
