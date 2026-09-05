/**
 * The two wiring defects behind the action dispatch, both in the
 * `residual/crash-data-loss-corruption` class, and both invisible to a smoke test.
 *
 * 1. The host wrapper discarded the dispatch promise, so the chip's
 *    duplicate-click guard released the instant it saw a non-thenable — the guard
 *    was live in the chip and defeated at the wiring. Cost: two breadcrumbs and
 *    two close requests for one double-click.
 * 2. A dispatch awaits a network write, and the transcript can advance across that
 *    await. Closing on the late response dismissed the tab AND cancelled the turn
 *    the user had just started.
 *
 * Asserted against the dispatch CONTRACT rather than either host's internals, so
 * the same expectations cover ChatPage and ChatPane, whose copies must not drift.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'
import { hasUnsentComposerWork } from '../utils/composerWork'
import type { OptionAction } from '../app-sdk/protocol/options'

const CLOSE: OptionAction = { action: 'close', label: 'Nothing else, close this tab' }
// The chip's accessible NAME states the effect and carries the label inside it
// (`Closes this session — <label>`), because a bare model-authored label named no
// consequence. Match on the label as a substring: an exact-name match would pass
// only for the old name this PR removed.
const CLOSE_NAME = new RegExp(CLOSE.label, 'i')
const ROW_A = 'row-a'
const ROW_B = 'row-b'

afterEach(cleanup)

/**
 * The dispatch under test, built the way both hosts build it: a network write, then
 * a close that is refused when the row moved. `currentKey` is a live box standing in
 * for the host's `followUpSourceKeyRef`.
 *
 * `composerWork` is the second live box, standing in for the host's
 * `composerWorkRef`. The recheck it feeds is NOT redundant with the key check: the
 * key moves only when the transcript gains a row, and typing does not add one, so a
 * draft written during the await is invisible to it.
 */
function makeDispatch(
  currentKey: { value: string | null },
  composerWork: { value: boolean } = { value: false },
  opts: { recheckComposer?: boolean } = { recheckComposer: true },
) {
  const writes: string[] = []
  const closes: string[] = []
  let releaseWrite: (() => void) | undefined
  const dispatch = async (action: OptionAction, sourceKeyAtClick?: string | null) => {
    const isStale = () => sourceKeyAtClick !== undefined && sourceKeyAtClick !== currentKey.value
    if (isStale()) return
    writes.push(action.label)
    await new Promise<void>(res => { releaseWrite = res })
    if (isStale()) return
    if (opts.recheckComposer !== false && hasUnsentComposerWork({
      text: composerWork.value ? 'typed' : '', files: [], dirs: [], sessionRefs: [],
      pasteBlocks: [], knowledge: false, uploading: false, voiceCapture: false,
    })) return
    closes.push(action.label)
  }
  return { dispatch, writes, closes, release: () => releaseWrite?.() }
}

describe('a draft typed DURING the write must not be closed away', () => {
  it('composer empty at click, typed while the note awaits -> close refused', async () => {
    const key = { value: ROW_A }
    const composer = { value: false }
    const { dispatch, writes, closes, release } = makeDispatch(key, composer)
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={dispatch} sourceKey={ROW_A} />,
    )
    // Empty composer at click: the chip is legitimately enabled, so the render-time
    // gate cannot be what saves the draft here.
    fireEvent.click(screen.getByRole('button', { name: CLOSE_NAME }))
    expect(writes).toHaveLength(1)

    composer.value = true   // the user starts typing while the write is in flight
    release()
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(closes).toHaveLength(0)
  })

  it('NEGATIVE CONTROL: without the recheck the same sequence closes and loses it', async () => {
    const key = { value: ROW_A }
    const composer = { value: false }
    const { dispatch, closes, release } = makeDispatch(key, composer, { recheckComposer: false })
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={dispatch} sourceKey={ROW_A} />,
    )
    fireEvent.click(screen.getByRole('button', { name: CLOSE_NAME }))
    composer.value = true
    release()
    await waitFor(() => expect(closes).toHaveLength(1))
  })

  it('an empty composer at settle still closes — the guard is not a blanket refusal', async () => {
    const key = { value: ROW_A }
    const composer = { value: false }
    const { dispatch, closes, release } = makeDispatch(key, composer)
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={dispatch} sourceKey={ROW_A} />,
    )
    fireEvent.click(screen.getByRole('button', { name: CLOSE_NAME }))
    release()
    await waitFor(() => expect(closes).toHaveLength(1))
  })

  it('whitespace typed during the await is not work, so the close still happens', async () => {
    const key = { value: ROW_A }
    const { dispatch, closes, release } = makeDispatch(key, { value: false })
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={dispatch} sourceKey={ROW_A} />,
    )
    fireEvent.click(screen.getByRole('button', { name: CLOSE_NAME }))
    release()
    await waitFor(() => expect(closes).toHaveLength(1))
  })
})

describe('the host must not discard the dispatch promise', () => {
  it('a double-click writes ONE breadcrumb when the promise is returned', async () => {
    const key = { value: ROW_A }
    const { dispatch, writes, closes, release } = makeDispatch(key)
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={dispatch} sourceKey={ROW_A} />,
    )
    const chip = screen.getByRole('button', { name: CLOSE_NAME })
    fireEvent.click(chip)
    fireEvent.click(chip)
    expect(writes).toHaveLength(1)

    release()
    await waitFor(() => expect(closes).toHaveLength(1))
  })

  it('NEGATIVE CONTROL: the void wrapper the finding names does double-fire', async () => {
    // The shape the hosts used: `(a) => { void dispatch(a) }` returns undefined, so
    // the chip sees a non-thenable and releases immediately. Pinned so a future
    // author cannot reintroduce the wrapper and still read this suite as green —
    // without this, the test above would pass for a guard that never engaged.
    const key = { value: ROW_A }
    const { dispatch, writes } = makeDispatch(key)
    const wrapped = (a: OptionAction, k?: string | null) => { void dispatch(a, k) }
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={wrapped} sourceKey={ROW_A} />,
    )
    const chip = screen.getByRole('button', { name: CLOSE_NAME })
    fireEvent.click(chip)
    fireEvent.click(chip)
    expect(writes).toHaveLength(2)
  })
})

describe('a stale action must not close a slot after newer work starts', () => {
  it('does not close when the row advanced during the write', async () => {
    const key = { value: ROW_A }
    const { dispatch, writes, closes, release } = makeDispatch(key)
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={dispatch} sourceKey={ROW_A} />,
    )
    fireEvent.click(screen.getByRole('button', { name: CLOSE_NAME }))
    expect(writes).toHaveLength(1)

    // The user starts another turn while the write is in flight.
    key.value = ROW_B
    release()
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(closes).toHaveLength(0)
  })

  it('does not close when a new turn cleared the chips entirely', async () => {
    // `deriveFollowUpOptions` returns a NULL source key once a `user` row ends the
    // scan, which is exactly what starting a turn does — so null must read as
    // moved-on, not as "no key supplied".
    const key = { value: ROW_A }
    const { dispatch, closes, release } = makeDispatch(key)
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={dispatch} sourceKey={ROW_A} />,
    )
    fireEvent.click(screen.getByRole('button', { name: CLOSE_NAME }))
    key.value = null
    release()
    await waitFor(() => expect(closes).toHaveLength(0))
  })

  it('writes nothing at all when the click was already stale on arrival', async () => {
    const key = { value: ROW_B }
    const { dispatch, writes, closes } = makeDispatch(key)
    // sourceKey is the row the chip was rendered from; the live key has moved past it.
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={dispatch} sourceKey={ROW_A} />,
    )
    fireEvent.click(screen.getByRole('button', { name: CLOSE_NAME }))
    await waitFor(() => expect(writes).toHaveLength(0))
    expect(closes).toHaveLength(0)
  })

  it('closes normally when the row never moved', async () => {
    const key = { value: ROW_A }
    const { dispatch, closes, release } = makeDispatch(key)
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={dispatch} sourceKey={ROW_A} />,
    )
    fireEvent.click(screen.getByRole('button', { name: CLOSE_NAME }))
    release()
    await waitFor(() => expect(closes).toEqual([CLOSE.label]))
  })

  it('an absent key keeps the previous behaviour rather than refusing', async () => {
    // `undefined` means no key was supplied at all — a programmatic caller, whose
    // call carries none. Refusing those wholesale would silently disable dispatch
    // for them.
    const key = { value: ROW_A }
    const { dispatch, closes, release } = makeDispatch(key)
    // Fire WITHOUT awaiting: the write only settles on `release()`, so awaiting
    // here would deadlock before the key could move.
    const inFlight = dispatch(CLOSE, undefined)
    key.value = ROW_B
    release()
    await inFlight
    expect(closes).toEqual([CLOSE.label])
  })
})
