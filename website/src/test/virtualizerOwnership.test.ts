// The chat virtualizer is a facade (`useVirtualChat.ts`) over composed owners,
// and three properties of that composition are invisible to behavioural tests
// until a real browser pays for them. This file pins them at the source level,
// the way virtualizerHeightOwner.test.ts pins the single height owner:
//
//   1. PHASE ORDER. React runs render-phase code, layout effects and passive
//      effects in hook-call order, and the owners cooperate through that order
//      (the shift capture reads the DOM before the height owner reprices it; the
//      compensation writes before follow's pins, which precede the slot-entry
//      placement; the scroller sync follows the height owner's store
//      subscription). A reordering is a behaviour change with every owner
//      untouched, so the facade's call order is pinned, and so is each owner's
//      own effect sequence: the kind, number and identity of every effect.
//   2. DELIBERATE TRIGGER SETS. Several effects list fewer dependencies than
//      they read, on purpose (re-running them would re-fire a consumed
//      correction or re-place a reader). Their dependency arrays are pinned
//      byte-for-byte with the lint exemption that marks them deliberate, and
//      so is the chain that keeps the height sync keyed on the scroller ref
//      alone (the resize observer re-registers whenever it re-keys).
//   3. DEPENDENCY DIRECTION. Owners exchange values only through the facade:
//      an owner may import another owner's TYPES, never its values, and the
//      helpers import no owner at all -- so there is no cycle and no second
//      wiring point.

import { describe, it, expect } from 'vitest'
import { readdirSync } from 'node:fs'
import { join } from 'node:path'
import { readSource as readSourceText } from './readSource'
import * as facade from '../hooks/virtualizer/useVirtualChat'
import * as followController from '../hooks/virtualizer/FollowController'

const DIR = join(__dirname, '..', 'hooks', 'virtualizer')
const read = (file: string) => readSourceText(join(DIR, file))
/** Strip comments so prose naming a symbol is not a match. Only a block that
 *  OPENS a line is stripped: a `/*` inside a `//` comment or after code must
 *  not open a span that hides real code. */
const code = (file: string) => read(file).replace(/^\s*\/\*[\s\S]*?\*\//gm, '').replace(/^\s*\/\/.*$/gm, '')

const OWNER_MODULES = [
  'followPolicy.ts',
  'geometryScheduling.ts',
  'measurement.ts',
  'observers.ts',
  'readingPosition.ts',
  'shiftCompensation.ts',
  'windowRange.ts',
]
/** The helpers the owners share. None holds React owner state, and none may
 *  import an owner. */
const HELPER_MODULES = [
  'FollowController.ts',
  'HeightCache.ts',
  'HeightIndex.ts',
  'MeasureFarm.tsx',
  'ScrollAnchorCache.ts',
  'WindowCalculator.ts',
  'anchorGeometry.ts',
  'inPlaceResize.ts',
  'types.ts',
]

describe('the virtualizer directory is fully classified', () => {
  it('every module is the facade, an owner, or a helper', () => {
    // A new file must be placed in one of the lists above, which is what puts
    // it under the direction rules below.
    const files = readdirSync(DIR).filter((f) => /\.tsx?$/.test(f)).sort()
    expect(files).toEqual(['useVirtualChat.ts', ...OWNER_MODULES, ...HELPER_MODULES].sort())
  })
})

describe('phase order (source guard)', () => {
  it('the facade composes its owners in phase order', () => {
    const owners = [
      'useStreamingSettleGrace', 'useScrollerElement', 'useFollowState', 'useWindowState', 'useShiftCapture',
      'useReadingPositionEntry', 'useHeightOwner', 'useWindowOperations', 'usePinning', 'useGeometrySync',
      'useRowMeasurement', 'useShiftCompensation', 'useFollowPlacementPins', 'useVisibilityReplacement',
      'useScrollListener', 'useCoverageWatchdog', 'useResizeObserver', 'useWindowEdgeTriggers',
      'useReadingPositionRestore', 'useMeasurementScopeReseed',
    ]
    const src = code('useVirtualChat.ts')
    const body = src.slice(src.indexOf('export function useVirtualChat<T>('))
    const calls = [...body.matchAll(/\b(use[A-Z]\w*)\(/g)]
      .map((m) => m[1])
      .filter((name) => owners.includes(name) || name === 'useEffect' || name === 'useLayoutEffect')
    expect(calls).toEqual([
      'useEffect', // the onTopReached ref sync: the first passive effect
      'useStreamingSettleGrace', // the first layout effect
      'useScrollerElement',
      'useFollowState',
      'useWindowState',
      'useShiftCapture', // render-phase capture; the prepend baseline mirror
      'useLayoutEffect', // the committed-window mirror, right after it
      'useReadingPositionEntry', // render-phase latch and session transition
      'useHeightOwner', // render-phase reprice; its store subscription's effects
      'useEffect', // the scroller-element sync, after that subscription
      'useWindowOperations',
      'usePinning',
      'useGeometrySync',
      'useRowMeasurement',
      'useShiftCompensation', // pre-paint: part 1, promotion, part 2, height anchor
      'useFollowPlacementPins', // pre-paint: leading chrome, append pin
      'useVisibilityReplacement',
      'useScrollListener',
      'useCoverageWatchdog',
      'useResizeObserver',
      'useWindowEdgeTriggers',
      'useReadingPositionRestore', // the last placing layout effect: slot entry
      'useMeasurementScopeReseed', // after placement: width-scope reseed (measurements only)
      'useEffect', // recompute on count
      'useEffect', // the dev probe
      'useEffect', // the unmount teardown
    ])
  })

  it('each owner keeps its effects in order, with their deliberate trigger sets', () => {
    // [module, dependency arrays in the order their effects must appear]. The
    // ones marked `true` are deliberate trigger sets and must carry the lint
    // exemption on the line above.
    const expected: [string, [string, boolean][]][] = [
      ['shiftCompensation.ts', [
        // The height-sync capture chain: varies only with the scroller ref.
        ['[anchorIdOf, elIndexRef, itemsRef],', false], // captureAnchorCands
        ['}, [scrollerRef, captureAnchorCands, stickRef, elIndexRef, itemsRef, getKeyRef])', false], // captureHeightSyncAnchor
        ['}, [itemCount])', true], // part 1: re-running would re-fire a consumed prepend
        ['}, [windowRange, rebaseScheduledRef, shiftStageRef])', false], // stage promotion
        ['}, [windowRange, itemCount, spliceCommit, scrollerRef, writeScrollTop, recomputeWindow])', true], // part 2
        ['}, [heightCommit, offsetIndex, scrollerRef, writeScrollTop])', true], // height-sync anchor (+ the owner identity: TRIGGER 7 stands down on the swap commit)
      ]],
      ['geometryScheduling.ts', [
        // syncHeightsNow: BASE froze this callback on `[scrollerRef]` behind an
        // exemption; here the linter checks it, and the chain above keeps its
        // identity keyed on the scroller ref alone.
        ['}, [captureHeightSyncAnchor, heightIndexRef, itemsRef])', false],
      ]],
      ['followPolicy.ts', [
        ['}, [itemCount, overscan, pinAuto, forcePin, scrollerRef])', true], // append / bulk pin
      ]],
      ['readingPosition.ts', [
        ['}, [sessionId, scrollerEl, itemCount, restoreEval])', true], // slot entry
      ]],
      ['windowRange.ts', [
        ['}, [windowRange.start, itemCount, prefetchStartIndex, sessionId])', true], // older-history crossing
      ]],
      ['measurement.ts', [
        ['}, [heightIndex, itemCount, estimatedHeight])', true], // tree sync keyed on the estimate
      ]],
      ['useVirtualChat.ts', [
        ['}, [sessionId, getH, estimatedHeight])', true], // dev probe
      ]],
    ]
    for (const [file, arrays] of expected) {
      const lines = read(file).split('\n')
      let last = -1
      for (const [deps, deliberate] of arrays) {
        const at = lines.findIndex((l, i) => i > last && l.trim() === deps)
        expect(at, `${file}: ${deps}`).toBeGreaterThan(last)
        if (deliberate) {
          expect(lines[at - 1], `${file}: ${deps} carries its exemption`).toMatch(/eslint-disable-next-line react-hooks\/exhaustive-deps/)
        }
        last = at
      }
    }
  })

  it('the scroller-element sync keeps its first-ref closure', () => {
    // `[]` is preserved from BASE as observed, not as a design claim: after an
    // external ref swap the ref-bound callbacks re-key while the element-keyed
    // listeners stay on the element first promoted to state
    // (useVirtualChat.surfaceContract.test.tsx pins that behaviour).
    const src = read('observers.ts')
    const at = src.indexOf('const syncScrollerEl = useCallback(')
    expect(at).toBeGreaterThan(-1)
    expect(src.slice(at, src.indexOf('\n  }, [])', at) + '\n  }, [])'.length)).toMatch(/eslint-disable-next-line react-hooks\/exhaustive-deps\n  \}, \[\]\)$/)
  })

  it('each owner runs exactly its effects, in phase order', () => {
    // [kind, a statement only that effect's body holds], in source order. An
    // added, removed, re-kinded or swapped effect fails here -- including one in
    // an owner the facade calls early, which would shift every later effect.
    const L = 'layout' as const
    const P = 'passive' as const
    const expected: Record<string, [typeof L | typeof P, string][]> = {
      'geometryScheduling.ts': [[L, 'prevStreamingIndexRef.current = streamingIndex']], // streaming grace
      'measurement.ts': [[L, 'reseedMounted()']], // width-scope reseed; the store subscription is useSyncExternalStore's own
      'shiftCompensation.ts': [
        [L, 'prependPrevRef.current = prependMirrorNext'], // prepend baseline mirror
        [L, 'prependNetRef.current = 0'], // part 1
        [L, "shiftStageRef.current = 'ready'"], // stage promotion
        [L, 'shiftInsertedRef.current = 0'], // part 2
        [L, 'heightAnchorPendingRef.current = null'], // height-sync anchor
      ],
      'followPolicy.ts': [
        [L, 'leadingChromeRef.current = lead'], // leading chrome
        [L, 'prevItemCountRef.current = itemCount'], // append / bulk pin
      ],
      'readingPosition.ts': [
        [P, 'let wasHidden = document.hidden'], // visibility re-placement
        [P, 'if (restoreTimerRef.current !== null) clearTimeout(restoreTimerRef.current)'], // restore cleanup
        [L, 'slotPinDoneRef.current = null'], // slot entry
      ],
      'observers.ts': [
        [P, 'const onScroll = () =>'], // scroll listener
        [P, 'new ResizeObserver('], // resize observer
        [P, 'resizeObserverRef.current?.observe(el)'], // late scroller
      ],
      'windowRange.ts': [
        [P, 'window.setInterval('], // coverage watchdog
        [P, 'prevWindowStartRef.current = windowRange.start'], // older-history crossing
        [P, 'new IntersectionObserver('], // sentinels
      ],
    }
    expect(Object.keys(expected).sort()).toEqual([...OWNER_MODULES].sort())
    for (const [file, effects] of Object.entries(expected)) {
      const src = code(file)
      const opens = [...src.matchAll(/^\s*use(Layout)?Effect\(/gm)]
      expect(opens.map((m) => (m[1] ? L : P)), file).toEqual(effects.map(([kind]) => kind))
      opens.forEach((m, i) => {
        const body = src.slice(m.index, opens[i + 1]?.index ?? src.length)
        expect(body, `${file}: effect ${i + 1}`).toContain(effects[i][1])
      })
    }
  })
})

describe('the restore settle speaks both identities (source guard)', () => {
  it('asks the facade predicate, and never compares the bare anchor key', () => {
    const reading = code('readingPosition.ts')
    expect(reading).toContain('anchoredRowIdentity(index, anchor)')
    expect(reading).not.toContain('anchorMatchesRow(')
    expect(reading).not.toMatch(/rowId\s*!==\s*anchor\.key/)
    const hook = code('useVirtualChat.ts')
    const at = hook.indexOf('const anchoredRowIdentity = useCallback(')
    expect(at).toBeGreaterThan(-1)
    expect(hook.slice(at, hook.indexOf('}, [altIdAtIndex])', at))).toContain(
      'anchorMatchesRow({ anchor, tailId: rowId, altId: it ? altIdAtIndex(index) : null })',
    )
  })
})

describe('retired APIs stay retired', () => {
  it('no virtualizer module reintroduces scrollToIndexSmooth', () => {
    for (const file of readdirSync(DIR).filter((f) => /\.tsx?$/.test(f))) {
      expect(read(file), file).not.toContain('scrollToIndexSmooth')
    }
  })
})

describe('dependency direction (source guard)', () => {
  // One statement at a time: the clause before `from` holds no quote, so the
  // match cannot run on into the next import.
  const imports = (file: string) =>
    [...code(file).matchAll(/^import\s+(type\s+)?[^']*?\bfrom\s+'([^']+)'/gm)]
      .filter((m) => m[2].startsWith('./'))
      .map((m) => ({
        typeOnly: Boolean(m[1]),
        module: `${m[2].slice(2)}.ts${m[2] === './MeasureFarm' ? 'x' : ''}`,
      }))

  it('owners import one another only as types, and never the facade', () => {
    for (const file of OWNER_MODULES) {
      for (const imp of imports(file)) {
        expect(imp.module, `${file} imports the facade`).not.toBe('useVirtualChat.ts')
        if (OWNER_MODULES.includes(imp.module)) {
          expect(imp.typeOnly, `${file} imports a VALUE from ${imp.module}`).toBe(true)
        }
      }
    }
  })

  it('pure helpers import no owner and not the facade', () => {
    for (const file of HELPER_MODULES) {
      for (const imp of imports(file)) {
        expect([...OWNER_MODULES, 'useVirtualChat.ts'], `${file} imports ${imp.module}`).not.toContain(imp.module)
      }
    }
  })

  it('the facade re-exports the public helpers by identity', () => {
    expect(facade.SCROLL_SETTLE_MS).toBe(followController.SCROLL_SETTLE_MS)
    expect(facade.shiftCompensationAllowed).toBe(followController.shiftCompensationAllowed)
    expect(facade.pinSuppressedNow).toBe(followController.pinSuppressedNow)
  })
})
