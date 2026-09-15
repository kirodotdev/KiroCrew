/**
 * The topbar's glass must rasterize into its own compositing layer.
 *
 * `.topbar-glass` carries `backdrop-filter`, and the two readout groups inside it
 * carry `container-type: inline-size` — which implies layout containment. When a
 * capsule segment mounts or unmounts (the metrics readout arriving, an update
 * pill shifting the ladder) the region it vacated has to be re-sampled through
 * that blur. With the glass sharing a layer with the content beneath it, the
 * damage from a contained subtree did not always reach the backdrop, and the old
 * blurred pixels stayed on screen — the reporter's "has something left in the
 * background".
 *
 * `translateZ(0)` is the repo's established promotion for this class of engine
 * bug (#7931 promoted every sandbox-doc frame the same way). It is asserted from
 * the stylesheet source rather than a computed style because jsdom does not load
 * `index.css`, so a `getComputedStyle` assertion here would be vacuous — the same
 * reason `topbarBadgeOverhang.test.ts` reads the file.
 *
 * Refs #7967
 */
import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const read = (f: string) => readFile(join(__dirname, '..', f), 'utf8')
// Strip comments before matching: the rule is explained in prose that quotes the
// very declarations being asserted, so a raw-text match would hit the comment.
const css = async () => (await read('index.css')).replace(/\/\*[\s\S]*?\*\//g, '')

// Anchor on the declaration only the base rule carries; `.topbar-glass` also
// appears in a dozen platform-scoped and focus-mode rules that must not match.
const GLASS_RULE = /\.topbar-glass\{[^}]*backdrop-filter:blur\(18px\)[^}]*\}/

describe('topbar glass compositing layer', () => {
  it('promotes the glass to its own layer so a contained subtree cannot strand its backdrop', async () => {
    const m = (await css()).match(GLASS_RULE)
    expect(m, 'expected the base .topbar-glass rule').not.toBeNull()
    expect(m![0]).toMatch(/transform:translateZ\(0\)/)
  })

  it('keeps the promotion off the transform property focus mode drives', async () => {
    // Focus mode slides the header with an INLINE `transform: translateY(...)`,
    // which wins over this rule — so the promotion has to survive being
    // overridden there. `backface-visibility:hidden` is the second, independent
    // spelling: it promotes on its own and focus mode never writes it, so the
    // glass stays on its own layer while sliding.
    const m = (await css()).match(GLASS_RULE)
    expect(m![0]).toMatch(/backface-visibility:hidden/)
  })

  it('leaves the container-query ladder clip and containment alone', async () => {
    // #7851 fixed the collapse ladder by tuning these rungs. The promotion is
    // additive on the glass; it must not have moved containment off the groups
    // the ladder measures, or every rung stops firing.
    const s = await css()
    expect(s).toMatch(/\.tb-left,\.tb-right\{container-type:inline-size[^}]*overflow:hidden\}/)
  })
})
