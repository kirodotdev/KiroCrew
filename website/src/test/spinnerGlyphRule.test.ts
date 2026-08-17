import { readdir, readFile } from 'node:fs/promises'
import { join } from 'node:path'

// One stated rule, enforced: below 15px use lucide `Loader`, at 15px and above
// use `LoaderCircle`. Without a guard this is per-call-site taste again by the
// third PR, which is the state the rule was written to end -- the glyph choice
// is a property of the SIZE, so it can be checked rather than reviewed.
//
// See website/docs/page-layout.md -> Spinners for the reasoning.

const SRC = join(__dirname, '..')
const SKIP = new Set(['test', 'node_modules', '__snapshots__'])
const SMALL_ARC = /<(?:Loader2|LoaderCircle)\s+size=\{(\d+)\}/g
const LARGE_SPOKES = /<Loader\s+size=\{(\d+)\}/g

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

describe('small-size spinner glyph rule', () => {
  it('uses Loader, not the arc, below 15px', async () => {
    const offenders: string[] = []
    for await (const file of walkSource(SRC)) {
      const src = await readFile(file, 'utf8')
      for (const m of src.matchAll(SMALL_ARC)) {
        if (Number(m[1]) < 15) {
          offenders.push(`${file.replace(SRC, 'src')}: size={${m[1]}}`)
        }
      }
    }
    expect(
      offenders,
      'the arc reads as a broken ring below 15px -- use `Loader` (see docs/page-layout.md)',
    ).toEqual([])
  })

  it('uses the arc, not Loader, at 15px and above', async () => {
    // The rule runs both ways on purpose. A one-directional check would let the
    // spoke glyph spread upward into sizes where the arc is the better mark,
    // and the app would end up with two spinners at one size again -- the exact
    // inconsistency this rule replaced, just pointing the other way.
    const offenders: string[] = []
    for await (const file of walkSource(SRC)) {
      const src = await readFile(file, 'utf8')
      for (const m of src.matchAll(LARGE_SPOKES)) {
        if (Number(m[1]) >= 15) {
          offenders.push(`${file.replace(SRC, 'src')}: size={${m[1]}}`)
        }
      }
    }
    expect(
      offenders,
      'at 15px+ the arc is unambiguous -- use `LoaderCircle` (see docs/page-layout.md)',
    ).toEqual([])
  })

  it('applies the rule to a spinner passed as a VALUE, not only as JSX', async () => {
    // The size-keyed assertions above see `<Loader2 size={13} />` and miss
    // `{ Icon: Loader2 }` in a phase/status map, because the size lives at the
    // `<Icon size={13} />` render site instead. Two such maps were the only
    // sites the first sweep of this rule missed, and they broke at RUNTIME
    // rather than in review, so the case is worth checking rather than
    // remembering.
    //
    // The check is per-file and deliberately narrow: an icon map naming the arc
    // is an offender only when the file also renders `<Icon>` below 15px.
    const offenders: string[] = []
    for await (const file of walkSource(SRC)) {
      const src = await readFile(file, 'utf8')
      if (!/\bIcon:\s*(?:Loader2|LoaderCircle)\b/.test(src)) continue
      const small = [...src.matchAll(/<Icon\s[^>]*?size=\{(\d+)\}/gs)]
        .some(m => Number(m[1]) < 15)
      if (small) offenders.push(file.replace(SRC, 'src'))
    }
    expect(
      offenders,
      'this icon map renders below 15px -- name `Loader` in the map',
    ).toEqual([])
  })

  it('states the threshold in the doc the next page is copied from', async () => {
    // Pinning the doc to the same number the assertions use: a rule that lives
    // only in a test is a rule nobody reads before writing the call site.
    const doc = await readFile(join(SRC, '..', 'docs', 'page-layout.md'), 'utf8')
    expect(doc, 'page-layout.md should carry the spinner rule')
      .toMatch(/Below 15px use lucide `Loader`\. At 15px and above use `LoaderCircle`\./)
  })
})
