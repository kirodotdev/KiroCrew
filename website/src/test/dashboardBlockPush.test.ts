/**
 * The block-patch receive side: the wire name, the frame it reads, and what the
 * page does with each one.
 *
 * ## THE WIRE NAME IS PINNED TO THE SERVER'S OWN CONSTANT
 *
 * The frame type is the CONTROLLER's (`handlers/member_dashboard_push.py`,
 * `BLOCK_FRAME`). This bundle holds a copy because the socket router dispatches
 * on a static `case`, and a second spelling of a wire name is how a push path
 * turns off silently -- a frame nobody routes looks exactly like a fold that
 * never advanced, with no error anywhere and a page that simply stops moving.
 *
 * So the first case below reads the VALUE out of the python source and compares
 * it. It SKIPS WITH A REASON while that module is still on the controller line's
 * branch, because a frozen copy of the string asserted against itself would be a
 * green test that proves nothing -- and it becomes a hard cross-language pin the
 * moment the file lands in this tree, with no edit here.
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

import {
  DASHBOARD_BLOCK_PATCH_FRAME,
  PAGE_FULL_PAINT_MESSAGE_TYPE,
  PATCH_REASON_LAYOUT,
  PATCH_REASON_UNBOUND,
  __resetBlockPatchForTests,
  fullPaintMessage,
  patchFitsPage,
  publishBlockPatch,
  readBlockPatchFrame,
  seedVersion,
  subscribeBlockPatch,
  verdictFor,
  type DashboardBlockPatch,
} from '../pages/members/dashboardBlockPush'

const SRC = resolve(__dirname, '..')
const PY_PUSH = join(SRC, '../../src/kiro_crew/dashboard/handlers/member_dashboard_push.py')
/** The document's own bootstrap, which owns the refill message type. Already in
 *  this tree, so the pin on it is hard rather than skipped. */
const PY_FRAME = join(SRC, '../../src/kiro_crew/dashboard_frame.py')
/** The renderer, which owns the BLOCK-PATCH message type. D1's file; on their
 *  branch today, so the pin on it skips with a reason until it lands. */
const PY_RENDER = join(SRC, '../../src/kiro_crew/dashboard_package_render.py')

/** The renderer's block-patch type, as `dashboard_package_render` spells it.
 *  Distinct from the full-paint type, which is the whole point. */
const PATCH_TYPE = 'kirocrew-dashboard:block-patch'

/** A frame as the controller composes it, with every key it sends.
 *
 *  `blocks` carries field NAMES and is DERIVED from `patch` by the server;
 *  `patch` is the renderer's own payload, forwarded verbatim and never read
 *  past its `type`. */
function frame(over: Partial<DashboardBlockPatch> = {}): DashboardBlockPatch {
  return {
    slug: 'oncall',
    dashboard: 'oncall-dashboard',
    version: 4,
    layout: 7,
    fold: 'work',
    blocks: { prs: ['open_prs'] },
    missing: [],
    patch: {
      type: PATCH_TYPE,
      blocks: { prs: { fields: { open_prs: 12 }, display: { open_prs: '12' } } },
      seq: 31,
      stale: false,
      missing: [],
    },
    refetch: false,
    reason: '',
    ...over,
  }
}

describe('the wire name is the server\'s', () => {
  const present = existsSync(PY_PUSH)

  it.skipIf(!present)('equals BLOCK_FRAME in the controller\'s own module', () => {
    const py = readFileSync(PY_PUSH, 'utf-8')
    const match = py.match(/^BLOCK_FRAME: Final\[str\] = "([a-z_]+)"/m)
    expect(match, 'BLOCK_FRAME is gone from the controller module').not.toBeNull()
    expect(DASHBOARD_BLOCK_PATCH_FRAME).toBe(match![1])
  })

  it.skipIf(!present)('and so are the two refetch reasons', () => {
    const py = readFileSync(PY_PUSH, 'utf-8')
    const reason = (name: string) => {
      const match = py.match(new RegExp(`^${name}[^=]*= "([a-z_]+)"`, 'm'))
      expect(match, `${name} is gone from the controller module`).not.toBeNull()
      return match![1]
    }
    expect(PATCH_REASON_LAYOUT).toBe(reason('REASON_LAYOUT'))
    expect(PATCH_REASON_UNBOUND).toBe(reason('REASON_UNBOUND'))
  })

  it('is recorded here even while the controller module is on another branch', () => {
    // The value this build listens for, stated once so the skip above cannot make
    // this file silently assert nothing at all. Relayed by conductor chat-2620 and
    // read back out of the controller's own commit 1584bce115 on 2026-10-10.
    expect(DASHBOARD_BLOCK_PATCH_FRAME).toBe('dashboard_block_patch')
  })

  it.skipIf(!existsSync(PY_FRAME))('and the FULL-PAINT type is the document\'s own', () => {
    // The other half of the seam, and the one whose failure is silent: the
    // document's bootstrap compares `data.type` to this exact string and returns
    // without a word otherwise.
    const py = readFileSync(PY_FRAME, 'utf-8')
    const match = py.match(/^DATA_MESSAGE_TYPE: Final\[str\] = "([a-z:-]+)"/m)
    expect(match, 'DATA_MESSAGE_TYPE is gone from dashboard_frame').not.toBeNull()
    expect(PAGE_FULL_PAINT_MESSAGE_TYPE).toBe(match![1])
  })

  it.skipIf(!existsSync(PY_RENDER))('and the two python constants differ, as this side assumes', () => {
    // THE PIN THAT WOULD HAVE CAUGHT THE BUG. This side only forwards the patch
    // verbatim, so it does not hold the patch type -- but it DOES rely on the two
    // being different messages, because that is the whole reason a fold push is
    // not a full paint. Read both out of python and compare them there.
    const render = readFileSync(PY_RENDER, 'utf-8')
    const patchType = render.match(/^BLOCK_PATCH_MESSAGE_TYPE: Final\[str\] = "([a-z:-]+)"/m)
    expect(patchType, 'BLOCK_PATCH_MESSAGE_TYPE is gone from the renderer').not.toBeNull()
    expect(patchType![1]).toBe(PATCH_TYPE)
    expect(patchType![1]).not.toBe(PAGE_FULL_PAINT_MESSAGE_TYPE)
  })
})

describe('readBlockPatchFrame', () => {
  it('reads every key the controller sends', () => {
    expect(readBlockPatchFrame(frame())).toEqual(frame())
  })

  it('reads a refetch frame, which carries no values', () => {
    // The controller's own shape for this: `blocks: {}`, `patch: {}`, `fold: ""`, a
    // reason. It must PARSE -- dropping it would lose the only signal that says
    // re-read.
    const refetch = frame({
      fold: '',
      blocks: {},
      missing: [],
      patch: {},
      refetch: true,
      reason: PATCH_REASON_LAYOUT,
    })
    expect(readBlockPatchFrame(refetch)).toEqual(refetch)
  })

  it('refuses the OLD value-carrying blocks shape instead of coercing it', () => {
    // `blocks` carries field NAMES now. A gateway still sending `{field: value}`
    // there is one that predates the reshape, so it has no `read` to apply either
    // -- and reading its values would be exactly the second, UNFORMATTED home the
    // reshape removed: a bare `1200000000` under a label that said `1.2 GB`.
    expect(readBlockPatchFrame({ ...frame(), blocks: { prs: { open_prs: 12 } } })).toBeNull()
    expect(readBlockPatchFrame({ ...frame(), blocks: { prs: 'open_prs' } })).toBeNull()
    expect(readBlockPatchFrame({ ...frame(), blocks: { prs: ['open_prs', 7] } })).toBeNull()
  })

  it('refuses a frame with no patch object at all', () => {
    expect(readBlockPatchFrame({ ...frame(), patch: undefined })).toBeNull()
    expect(readBlockPatchFrame({ ...frame(), patch: [] })).toBeNull()
    expect(readBlockPatchFrame({ ...frame(), patch: 'nope' })).toBeNull()
  })

  it('keeps the renderer\'s patch byte-for-byte, display strings included', () => {
    // THE ONE PROPERTY THIS SIDE MUST NOT TOUCH. The patch is the renderer's own
    // output -- its per-block narrowing, its formatted strings and its message
    // type -- and it is forwarded unchanged. Nothing here parses, rounds,
    // localises, re-keys or re-derives any of it, so a reader sees one spelling of
    // "a number with a unit and a precision" on a first paint and on a refill.
    const payload = {
      type: PATCH_TYPE,
      blocks: { disk: { fields: { disk: 1200000000 }, display: { disk: '1.2 GB' } } },
      seq: 44,
      stale: false,
      missing: ['blocked_on'],
    }
    const read = readBlockPatchFrame(frame({ patch: payload }))
    expect(read?.patch).toEqual(payload)
  })

  it('refuses a frame with no slug, version or layout', () => {
    // Each of the three is load-bearing and none can be defaulted: without `slug`
    // the frame reaches no tab, and without the two numbers it cannot be placed in
    // the page's history, so applying it would write values into a layout they may
    // not belong to.
    expect(readBlockPatchFrame({ ...frame(), slug: '' })).toBeNull()
    expect(readBlockPatchFrame({ ...frame(), version: undefined })).toBeNull()
    expect(readBlockPatchFrame({ ...frame(), layout: undefined })).toBeNull()
    expect(readBlockPatchFrame({ ...frame(), version: Number.NaN })).toBeNull()
  })

  it('refuses a blocks map that is not block -> field names', () => {
    expect(readBlockPatchFrame({ ...frame(), blocks: [] })).toBeNull()
    expect(readBlockPatchFrame({ ...frame(), blocks: { prs: 12 } })).toBeNull()
  })

  it('refuses anything that is not a frame', () => {
    for (const raw of [null, undefined, 0, '', 'dashboard_block_patch', []]) {
      expect(readBlockPatchFrame(raw)).toBeNull()
    }
  })

  it('drops a non-string name out of missing rather than the whole frame', () => {
    // `missing` NAMES cells the page dims. A bad entry there costs one dimmed cell;
    // refusing the frame over it would cost every value in it.
    expect(readBlockPatchFrame({ ...frame(), missing: ['ok', 7, null] })?.missing).toEqual(['ok'])
    expect(readBlockPatchFrame({ ...frame(), missing: 'nope' })?.missing).toEqual([])
  })
})

describe('verdictFor', () => {
  it('applies a contiguous frame at the layout on screen', () => {
    expect(verdictFor(frame({ version: 5 }), { layout: 7, lastVersion: 4 })).toBe('apply')
  })

  it('re-reads a refetch frame whatever its numbers say', () => {
    // The server already decided. A refetch frame carries no values, so there is
    // nothing to apply even when the version is contiguous and the layout matches
    // -- which is exactly the case `package_unbound` produces.
    expect(
      verdictFor(
        frame({ version: 5, refetch: true, blocks: {}, patch: {}, reason: PATCH_REASON_UNBOUND }),
        { layout: 7, lastVersion: 4 },
      ),
    ).toBe('refetch')
  })

  it('re-reads rather than forwarding a frame with no payload', () => {
    // The server sends no frame at all when nothing subscribes, so this shape is a
    // gateway and a bundle that disagree. Forwarding `{}` would post a typeless
    // message the document drops in silence while this side spent a version on it.
    expect(verdictFor(frame({ version: 5, patch: {} }), { layout: 7, lastVersion: 4 })).toBe(
      'refetch',
    )
  })

  it('re-reads when the LAYOUT differs, even with a contiguous version', () => {
    // The second, independent trigger. A recompose produces a perfectly
    // contiguous version, so a page that checked only the counter would patch
    // the new view's values into the old view's blocks.
    expect(verdictFor(frame({ version: 5, layout: 8 }), { layout: 7, lastVersion: 4 })).toBe(
      'refetch',
    )
  })

  it('re-reads on a GAP', () => {
    // A frame between the two never arrived, so the blocks on screen are not what
    // the server composed and no later patch repairs them.
    expect(verdictFor(frame({ version: 6 }), { layout: 7, lastVersion: 4 })).toBe('refetch')
  })

  it('re-reads a LOWER version, which is what a gateway restart sends', () => {
    // THE CASE A REPLAY-TOLERANT READING GETS WRONG, and this was written that way
    // first. The push counter lives in memory on the server, per live page, so a
    // restart arms a fresh page at 0 and the next frame arrives below whatever the
    // tab holds. Ignoring it as a replay drops it, drops every frame after it, and
    // freezes the tab on pre-restart values with nothing on screen saying so --
    // until the counter climbs back past the held number and the page silently
    // resumes, having missed everything in between.
    expect(verdictFor(frame({ version: 1 }), { layout: 7, lastVersion: 9 })).toBe('refetch')
    expect(verdictFor(frame({ version: 0 }), { layout: 7, lastVersion: 9 })).toBe('refetch')
  })

  it('re-reads a repeat of the frame it already applied', () => {
    // Strict equality, so a duplicate is not special-cased either. One extra read
    // is the cheaper mistake than a branch that also swallows a restart.
    expect(verdictFor(frame({ version: 4 }), { layout: 7, lastVersion: 4 })).toBe('refetch')
  })

  it('applies ONLY the very next version', () => {
    // The whole rule in one case: one value applies and its neighbours do not.
    expect(verdictFor(frame({ version: 5 }), { layout: 7, lastVersion: 4 })).toBe('apply')
    expect(verdictFor(frame({ version: 4 }), { layout: 7, lastVersion: 4 })).toBe('refetch')
    expect(verdictFor(frame({ version: 6 }), { layout: 7, lastVersion: 4 })).toBe('refetch')
  })

  it('trusts the first frame when the read carried no counter', () => {
    // There is no gap to measure yet. Seeded from the frame rather than refused,
    // so a tab that opened between the read and the first frame is live at once.
    expect(verdictFor(frame({ version: 9 }), { layout: 7, lastVersion: null })).toBe('apply')
  })

  it('checks the first frame for a gap when the read DID carry one', () => {
    // `push_version` is what makes even the first frame checkable: the counter the
    // body was composed at, so a frame that skipped one in the window between the
    // read and the socket is caught instead of trusted.
    expect(verdictFor(frame({ version: 11 }), { layout: 7, lastVersion: 9 })).toBe('refetch')
    expect(verdictFor(frame({ version: 10 }), { layout: 7, lastVersion: 9 })).toBe('apply')
  })
})

describe('seedVersion', () => {
  it('starts from the read\'s own push_version', () => {
    expect(seedVersion({ push_version: 9 })).toBe(9)
    expect(seedVersion({ push_version: 0 })).toBe(0)
  })

  it('answers null when the body does not carry one', () => {
    // An older gateway, or the template path, which has no push stream. The page
    // then trusts its first frame -- see `verdictFor`.
    expect(seedVersion({})).toBeNull()
    expect(seedVersion(null)).toBeNull()
    expect(seedVersion(undefined)).toBeNull()
    expect(seedVersion({ push_version: Number.NaN })).toBeNull()
  })
})

describe('patchFitsPage', () => {
  it('takes a patch whose blocks the page is showing', () => {
    expect(patchFitsPage(frame(), ['prs', 'notes'])).toBe(true)
  })

  it('refuses a patch naming a block the page does not have', () => {
    // The page and the controller disagree about the view while `layout` says they
    // do not -- the one case that number cannot catch.
    expect(patchFitsPage(frame({ blocks: { ghost: [] } }), ['prs'])).toBe(false)
    expect(patchFitsPage(frame({ blocks: { prs: [], ghost: [] } }), ['prs'])).toBe(false)
  })

  it('takes a refetch frame, which names no blocks at all', () => {
    // `{}` fits any page vacuously, which is right: the refetch decision is made
    // before this and must not depend on a block list.
    expect(patchFitsPage(frame({ blocks: {}, patch: {}, refetch: true }), [])).toBe(true)
  })
})

describe('the two message types are not the same message', () => {
  it('a fold push never carries the FULL-PAINT type', () => {
    // THE BUG THIS SPLIT EXISTS FOR, and it shipped in this file under the old
    // name: the constant was called `BLOCK_PATCH_MESSAGE_TYPE` and held the
    // full-paint value, and every patch was posted with it. That listener replaces
    // the whole read and RE-INITIALISES every block, so a block owning a canvas
    // gets a second canvas and two scenes animate over each other.
    //
    // Asserted as an inequality rather than on either value alone, because that is
    // the property: whatever the two strings become, they must not converge.
    expect(PATCH_TYPE).not.toBe(PAGE_FULL_PAINT_MESSAGE_TYPE)
    expect(frame().patch.type).toBe(PATCH_TYPE)
    expect(frame().patch.type).not.toBe(PAGE_FULL_PAINT_MESSAGE_TYPE)
  })

  it('names the full-paint type for a real full repaint', () => {
    // Still needed, and only here: a deliberate repaint, where re-initialising
    // every block is the correct behaviour rather than the bug.
    expect(fullPaintMessage({ fields: { a: 1 } })).toEqual({
      type: PAGE_FULL_PAINT_MESSAGE_TYPE,
      read: { fields: { a: 1 } },
    })
  })

  it('prefers the type the SERVER named over the one held here', () => {
    // The body carries `page_message` precisely so the frontend need not hold the
    // constant. Holding it as a fallback is for a body that does not send one.
    expect(fullPaintMessage({ fields: {} }, 'kirocrew-dashboard:data-v2').type).toBe(
      'kirocrew-dashboard:data-v2',
    )
    expect(fullPaintMessage({ fields: {} }, '').type).toBe(PAGE_FULL_PAINT_MESSAGE_TYPE)
  })
})

describe('the router-to-tab bridge', () => {
  beforeEach(__resetBlockPatchForTests)

  it('hands a frame to the tab open on that crewmate', () => {
    const seen: DashboardBlockPatch[] = []
    subscribeBlockPatch('oncall', patch => seen.push(patch))
    expect(publishBlockPatch(frame())).toBe(1)
    expect(seen).toEqual([frame()])
  })

  it('leaves another crewmate\'s open tab alone', () => {
    // Without the slug in the key, one crewmate's fold advance would repaint every
    // open dashboard -- and every one of them would then find the blocks unknown
    // and re-read, turning one push into a read per open tab.
    const seen: string[] = []
    subscribeBlockPatch('oncall', () => seen.push('oncall'))
    subscribeBlockPatch('release-captain', () => seen.push('release-captain'))
    publishBlockPatch(frame({ slug: 'release-captain' }))
    expect(seen).toEqual(['release-captain'])
  })

  it('drops a frame for a crewmate nobody is looking at', () => {
    // Correct rather than lossy: the next read composes the page from the crew log,
    // so a patch is never the only path a value has.
    expect(publishBlockPatch(frame({ slug: 'nobody' }))).toBe(0)
  })

  it('stops delivering after unsubscribe', () => {
    const seen: number[] = []
    const off = subscribeBlockPatch('oncall', patch => seen.push(patch.version))
    publishBlockPatch(frame({ version: 1 }))
    off()
    publishBlockPatch(frame({ version: 2 }))
    expect(seen).toEqual([1])
  })

  it('survives a listener that unsubscribes during delivery', () => {
    // The tab unmounts on the frame that re-reads, so this is the ordinary path
    // and not an edge: iterating the live set would skip the second listener.
    const seen: string[] = []
    const off = subscribeBlockPatch('oncall', () => {
      seen.push('first')
      off()
    })
    subscribeBlockPatch('oncall', () => seen.push('second'))
    publishBlockPatch(frame())
    expect(seen).toEqual(['first', 'second'])
  })

  it('still delivers to the others when one listener THROWS', () => {
    // Found by a test that failed only when run after its neighbours, which is the
    // honest way to find this one: two tabs can be open on the same crewmate, and a
    // listener whose component is mid-teardown throws out of the refetch it starts.
    // Delivery stopping there cost every later tab its frame -- and a missed frame
    // looks exactly like a fold that did not advance, with nothing to see.
    const seen: string[] = []
    subscribeBlockPatch('oncall', () => {
      throw new Error('mid-teardown')
    })
    subscribeBlockPatch('oncall', () => seen.push('after the thrower'))
    expect(() => publishBlockPatch(frame())).not.toThrow()
    expect(seen).toEqual(['after the thrower'])
  })
})
