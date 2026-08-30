/**
 * The shared dnd-kit sensor set, and the rule that keeps it the only one.
 *
 * The hook exists because the mouse/touch split is a WebKit BUG FIX carrying a
 * paragraph of reasoning, and it had been copy-pasted into three surfaces. A
 * behavioural test cannot catch a fourth copy appearing in a file nobody wrote
 * a drag test for, so the scan at the bottom reads the tree instead - the same
 * shape as `ImeEnterClaimRatchet`.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { renderHook, cleanup } from '@testing-library/react'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { KeyboardSensor, MouseSensor, TouchSensor } from '@dnd-kit/core'
import { sortableKeyboardCoordinates } from '@dnd-kit/sortable'
import { useDndSensors } from './useDndSensors'

const SRC = join(__dirname, '..')

afterEach(cleanup)

function sensorFor(descriptors: ReturnType<typeof useDndSensors>, sensor: unknown) {
  return descriptors.find((d) => d.sensor === sensor)
}

describe('useDndSensors', () => {
  it('splits mouse and touch into separate sensors', () => {
    // The whole point: NOT one PointerSensor. A single pointer sensor past its
    // activation distance preventDefault()s every move through dnd-kit's
    // non-passive touchmove listener, so a swipe starting on a draggable row
    // cannot scroll the list on WebKit.
    const { result } = renderHook(() => useDndSensors({ distance: 6 }))
    expect(sensorFor(result.current, MouseSensor)).toBeDefined()
    expect(sensorFor(result.current, TouchSensor)).toBeDefined()
  })

  it('takes the mouse activation distance from the caller', () => {
    // The one knob a surface owns: it trades click latency against accidental
    // drags, and what a plain click does differs per surface.
    const { result } = renderHook(() => useDndSensors({ distance: 8 }))
    expect(sensorFor(result.current, MouseSensor)?.options).toEqual({ activationConstraint: { distance: 8 } })
  })

  it('gives every surface the same press-and-hold touch constraint', () => {
    // Shared, not per-surface: under a delay constraint, movement past the
    // tolerance CANCELS the sensor, which is what hands a swipe back to the
    // browser for scrolling. Retuning it per call site is how a list silently
    // stops scrolling on one page and not another.
    //
    // Asserted as the LITERAL rather than against the hook's own constant:
    // comparing the hook's output to the value the hook used would pass for any
    // value, so it would not notice the constraint being retuned.
    const hold = { delay: 250, tolerance: 5 }
    const a = renderHook(() => useDndSensors({ distance: 5 }))
    const b = renderHook(() => useDndSensors({ distance: 8 }))
    expect(sensorFor(a.result.current, TouchSensor)?.options).toEqual({ activationConstraint: hold })
    expect(sensorFor(b.result.current, TouchSensor)?.options).toEqual({ activationConstraint: hold })
  })

  it('adds the keyboard sensor only when asked, with the sortable coordinate getter', () => {
    const off = renderHook(() => useDndSensors({ distance: 5 }))
    expect(sensorFor(off.result.current, KeyboardSensor)).toBeUndefined()
    expect(off.result.current).toHaveLength(2)

    const on = renderHook(() => useDndSensors({ distance: 5, keyboard: true }))
    expect(on.result.current).toHaveLength(3)
    expect(sensorFor(on.result.current, KeyboardSensor)?.options)
      .toEqual({ coordinateGetter: sortableKeyboardCoordinates })
  })
})

/**
 * Source-level ratchet. `useSensors` is dnd-kit's sensor-set constructor, so a
 * call to it OUTSIDE this hook is by definition a second sensor policy - which
 * is what the three copies were.
 */
function sourceFiles(): string[] {
  return readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .map((p) => p.split('\\').join('/'))
    .filter((p) => /\.tsx?$/.test(p))
    .filter((p) => !/\.test\.tsx?$/.test(p))
}

describe('dnd sensor ratchet', () => {
  it('builds a dnd-kit sensor set in exactly one place', () => {
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (rel === 'hooks/useDndSensors.ts') continue
      readFileSync(join(SRC, rel), 'utf8').split('\n').forEach((line, i) => {
        const code = line.trim()
        if (code.startsWith('//') || code.startsWith('*') || code.startsWith('/*')) return
        if (/\buseSensors\s*\(/.test(code)) offenders.push(`${rel}:${i + 1}`)
      })
    }
    // A surface needing different activation should pass an option to
    // `useDndSensors`, so the WebKit reasoning stays in one place with it.
    expect(offenders).toEqual([])
  })

  it('keeps the sensor classes out of feature files too', () => {
    // Importing MouseSensor/TouchSensor/KeyboardSensor outside the hook is the
    // step BEFORE re-spelling the split - catching the import catches the copy
    // while it is still half-written.
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (rel === 'hooks/useDndSensors.ts') continue
      const src = readFileSync(join(SRC, rel), 'utf8')
      for (const m of src.matchAll(/^import\s*\{([^}]*)\}\s*from\s*'@dnd-kit\/core'/gm)) {
        if (/\b(MouseSensor|TouchSensor|KeyboardSensor|PointerSensor|useSensor)\b/.test(m[1])) {
          offenders.push(`${rel}: ${m[1].trim().slice(0, 60)}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })
})
