import React from 'react'
import { describe, it, expect, vi, onTestFinished } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import ModelEffortDropdown from '../components/ModelEffortDropdown'
import chatReducer from '../store/chatSlice'
import dashboardReducer, { addSlotOptimistic } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/**
 * With `Auto (Jev)` picked, the composer's own model picker lets the owner narrow the models and
 * the effort span Jev may route to (`POST /api/chat/slots/{slot}/jev-range`). The range is read
 * from the slot in the store, like `jev_route`; no second control exists
 * (docs/decisions/2026-06-14-chat-composer-model-and-effort-are-one-control.md).
 */

const SLOT = 'dashboard:1'
const OTHER_SLOT = 'dashboard:2'
const MODELS = [
  { name: 'auto:jev', description: 'Jev' },
  { name: 'auto', description: '' },
  { name: 'model-a' },
  { name: 'model-b' },
  { name: 'model-c' },
]
const NO_RANGE = { models: [], effort_min: '', effort_max: '' }

const baseProps = {
  anchorRect: { right: 400, top: 300 } as DOMRect,
  dropdownRef: React.createRef<HTMLDivElement>(),
  inputRef: React.createRef<HTMLInputElement>(),
  models: MODELS,
  activeModel: 'auto:jev',
  onSelectModel: vi.fn(),
  filter: '',
  setFilter: vi.fn(),
  onClose: vi.fn(),
  hasEffort: true,
  slot: SLOT,
  currentEffort: '',
  onListKeyDown: vi.fn(),
}

interface RangeBody { models: string[]; effort_min: string; effort_max: string }

/** Renders the picker over a store whose slot carries `jev_range`, and records each range POST;
 *  the default reply echoes the posted range, as the gateway does. */
function setup(
  jevRange: RangeBody,
  props: Partial<React.ComponentProps<typeof ModelEffortDropdown>> = {},
  reply: (body: RangeBody, slot: string) => Response | Promise<Response> = body => HttpResponse.json({ ok: true, jev_range: body }),
  levels = ['low', 'medium', 'high'],
) {
  const posts: RangeBody[] = []
  const postedTo: string[] = []
  server.use(
    http.get('/api/effort-levels', () => HttpResponse.json(levels)),
    http.post('/api/chat/slots/:slot/jev-range', async ({ request, params }) => {
      postedTo.push(params.slot as string)
      const body = (await request.json()) as RangeBody
      posts.push(body)
      return reply(body, params.slot as string)
    }),
  )
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  store.dispatch(addSlotOptimistic({ key: SLOT, model: 'auto', jev_route: true, jev_range: jevRange } as never))
  store.dispatch(addSlotOptimistic({ key: OTHER_SLOT, model: 'auto', jev_route: true, jev_range: NO_RANGE } as never))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (extra: typeof props) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ModelEffortDropdown {...baseProps} {...props} {...extra} />
      </QueryClientProvider>
    </Provider>
  )
  let view = render(tree({}))
  return {
    posts, postedTo, store, qc,
    unmount: () => view.unmount(),
    remount: () => { view.unmount(); view = render(tree({})) },
    reshow: (extra: typeof props) => view.rerender(tree(extra)),
  }
}

/** Replies that wait for `release(i)`, the i-th POST counting from 0: an echo, or a 400 when `refuse`. */
function heldReplies(refuse = false) {
  const gates: Array<() => void> = []
  onTestFinished(() => gates.forEach(open => open()))
  const reply = (body: RangeBody) => new Promise<Response>(resolve => {
    gates.push(() => resolve(refuse
      ? HttpResponse.json({ error: 'jev_range_invalid' }, { status: 400 })
      : HttpResponse.json({ ok: true, jev_range: body })))
  })
  return { reply, release: (i: number) => gates[i]() }
}

const rangeOn = (store: ReturnType<typeof setup>['store'], slot: string) =>
  store.getState().dashboard.slots.find(x => x.key === slot)?.jev_range
const LMH = { effortLevelsOverride: ['low', 'medium', 'high'] }

const modelCheck = (id: string) => screen.getByRole('checkbox', { name: `Jev may use ${id}` })
const lowest = () => screen.findByRole('slider', { name: 'Lowest effort Jev may use' })
const highest = () => screen.findByRole('slider', { name: 'Highest effort Jev may use' })
const lowestMine = () => screen.findByRole('switch', { name: 'Use my level as the lowest' })
const highestMine = () => screen.findByRole('switch', { name: 'Use my level as the highest' })

describe('ModelEffortDropdown — Jev range', () => {
  it('offers a "Jev may use" check on each model row, all ticked while no range is set', () => {
    setup(NO_RANGE)
    // Real models only: `auto` and `auto:jev` are not models Jev can route to.
    expect(screen.getAllByRole('checkbox')).toHaveLength(3)
    for (const id of ['model-a', 'model-b', 'model-c']) expect(modelCheck(id)).toBeChecked()
  })

  it('posts the narrowed range, keeping the effort span, when a row is unchecked', async () => {
    const { posts, postedTo } = setup({ models: [], effort_min: 'low', effort_max: '' })
    await userEvent.click(modelCheck('model-b'))
    await waitFor(() => expect(posts).toEqual([{ models: ['model-a', 'model-c'], effort_min: 'low', effort_max: '' }]))
    expect(postedTo).toEqual([SLOT])
  })

  it('keeps the lowest and highest effort sliders from crossing, an equal pair allowed', async () => {
    const { posts } = setup({ models: [], effort_min: 'medium', effort_max: 'medium' })
    const lo = await lowest()
    const hi = await highest()
    // Three levels, no "Your level" notch.
    await waitFor(() => expect(hi).toHaveAttribute('aria-valuemax', '2'))
    fireEvent.keyDown(lo, { key: 'End' })
    fireEvent.keyDown(hi, { key: 'Home' })
    expect(lo).toHaveAttribute('aria-valuetext', 'Medium')
    expect(hi).toHaveAttribute('aria-valuetext', 'Medium')
    // The span still moves on the free side.
    fireEvent.keyDown(hi, { key: 'ArrowRight' })
    await waitFor(() => expect(posts).toEqual([{ models: [], effort_min: 'medium', effort_max: 'high' }]))
  })

  it('starts with both bounds on "Use my level", their sliders disabled', async () => {
    setup(NO_RANGE, LMH)
    for (const mine of [await lowestMine(), await highestMine()]) expect(mine).toHaveAttribute('aria-checked', 'true')
    for (const slider of [await lowest(), await highest()]) {
      expect(slider).toHaveAttribute('aria-disabled', 'true')
      expect(slider).toHaveAttribute('aria-valuetext', 'Your level')
    }
  })

  it('stores the far end of the axis when a "Use my level" switch is turned off', async () => {
    const { posts } = setup(NO_RANGE, LMH)
    await userEvent.click(await lowestMine())
    await waitFor(() => expect(posts).toEqual([{ models: [], effort_min: 'low', effort_max: '' }]))
    await userEvent.click(await highestMine())
    await waitFor(() => expect(posts[1]).toEqual({ models: [], effort_min: 'low', effort_max: 'high' }))
  })

  it.each([
    ['lowest', lowestMine, { models: ['model-a', 'model-b'], effort_min: '', effort_max: 'high' }],
    ['highest', highestMine, { models: ['model-a', 'model-b'], effort_min: 'low', effort_max: '' }],
  ])('stores "" for just the %s bound when its "Use my level" switch is turned back on', async (_, mine, expected) => {
    const { posts } = setup({ models: ['model-a', 'model-b'], effort_min: 'low', effort_max: 'high' }, LMH)
    await userEvent.click(await mine())
    await waitFor(() => expect(posts).toEqual([expected]))
  })

  it("stores the axis's first level for the lowest bound when the offered levels all sit above the stored highest", async () => {
    const { posts } = setup({ models: [], effort_min: '', effort_max: 'low' }, { effortLevelsOverride: ['medium', 'high'] })
    await userEvent.click(await lowestMine())
    await waitFor(() => expect(posts).toEqual([{ models: [], effort_min: 'low', effort_max: 'low' }]))
  })

  it('draws a set bound on the real levels only, with no "Your level" value', async () => {
    setup({ models: [], effort_min: 'low', effort_max: 'high' }, LMH)
    for (const slider of [await lowest(), await highest()]) expect(slider).toHaveAttribute('aria-valuemax', '2')
    expect(await lowest()).toHaveAttribute('aria-valuetext', 'Low')
    expect(await highest()).toHaveAttribute('aria-valuetext', 'High')
  })

  it('lets the lowest slider reach the top level while the highest bound is "Use my level"', async () => {
    const { posts } = setup({ models: [], effort_min: 'low', effort_max: '' }, LMH)
    fireEvent.keyDown(await lowest(), { key: 'End' })
    await waitFor(() => expect(posts).toEqual([{ models: [], effort_min: 'high', effort_max: '' }]))
  })

  it('posts nothing for a key on a slider whose "Use my level" switch is on', async () => {
    const { posts } = setup(NO_RANGE, LMH)
    fireEvent.keyDown(await lowest(), { key: 'ArrowRight' })
    fireEvent.keyDown(await highest(), { key: 'ArrowLeft' })
    // The model post is the barrier: a slider post would sit before it.
    await userEvent.click(modelCheck('model-b'))
    await waitFor(() => expect(posts).toEqual([{ models: ['model-a', 'model-c'], effort_min: '', effort_max: '' }]))
  })

  it('offers only the levels the endpoint accepts', async () => {
    const { posts } = setup(NO_RANGE, {}, undefined, ['minimal', 'low', 'medium'])
    const hi = await highest()
    // Two Crew levels; `minimal` is not one of them, and the fallback list has five.
    await waitFor(() => expect(hi).toHaveAttribute('aria-valuemax', '1'))
    await userEvent.click(await lowestMine())
    await waitFor(() => expect(posts).toEqual([{ models: [], effort_min: 'low', effort_max: '' }]))
    await userEvent.click(await highestMine())
    await waitFor(() => expect(posts[1]).toEqual({ models: [], effort_min: 'low', effort_max: 'medium' }))
  })

  it('shows a stored bound the session does not offer, and lets it be moved off', async () => {
    const { posts } = setup({ models: [], effort_min: 'xhigh', effort_max: '' }, LMH)
    const lo = await lowest()
    expect(lo).toHaveAttribute('aria-valuetext', 'Extra High')
    expect(lo).toHaveAttribute('aria-valuemax', '3')
    fireEvent.keyDown(lo, { key: 'Home' })
    await waitFor(() => expect(posts).toEqual([{ models: [], effort_min: 'low', effort_max: '' }]))
  })

  it('draws a stored bound between offered levels, and clamps against it', async () => {
    const { posts } = setup({ models: [], effort_min: 'medium', effort_max: 'high' }, { effortLevelsOverride: ['low', 'high'] })
    expect(await lowest()).toHaveAttribute('aria-valuetext', 'Medium')
    fireEvent.keyDown(await highest(), { key: 'Home' })
    await waitFor(() => expect(posts).toEqual([{ models: [], effort_min: 'medium', effort_max: 'medium' }]))
  })

  it('draws levels offered out of order from low to high', async () => {
    const { posts } = setup({ models: [], effort_min: 'low', effort_max: 'high' }, { effortLevelsOverride: ['high', 'low', 'medium'] })
    fireEvent.keyDown(await lowest(), { key: 'ArrowRight' })
    await waitFor(() => expect(posts).toEqual([{ models: [], effort_min: 'medium', effort_max: 'high' }]))
  })

  it('hides the effort span when the backend offers none of the accepted levels', async () => {
    setup(NO_RANGE, {}, undefined, ['minimal', 'none'])
    await waitFor(() => expect(screen.queryAllByRole('slider', { name: /effort Jev may use/ })).toHaveLength(0))
    expect(screen.getAllByRole('checkbox')).toHaveLength(3)
  })

  it('posts only listed ids when the stored range holds one the list does not show', async () => {
    const { posts } = setup({ models: ['model-a', 'retired-model'], effort_min: '', effort_max: '' })
    await userEvent.click(modelCheck('model-b'))
    await waitFor(() => expect(posts).toEqual([{ models: ['model-a', 'model-b'], effort_min: '', effort_max: '' }]))
  })

  it('drops a stored id the list does not show when a slider moves', async () => {
    const { posts } = setup({ models: ['model-a', 'retired-model'], effort_min: 'low', effort_max: '' }, LMH)
    fireEvent.keyDown(await lowest(), { key: 'ArrowRight' })
    await waitFor(() => expect(posts).toEqual([{ models: ['model-a'], effort_min: 'medium', effort_max: '' }]))
  })

  it('never turns a range of only unlisted ids into "every model", and a tick repairs it', async () => {
    const { posts } = setup({ models: ['retired-model'], effort_min: 'low', effort_max: '' }, LMH)
    fireEvent.keyDown(await lowest(), { key: 'ArrowRight' })
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toEqual({ models: ['retired-model'], effort_min: 'medium', effort_max: '' })
    for (const id of ['model-a', 'model-b', 'model-c']) {
      expect(modelCheck(id)).not.toBeChecked()
      expect(modelCheck(id)).toBeEnabled()
    }
    await userEvent.click(modelCheck('model-b'))
    await waitFor(() => expect(posts.at(-1)?.models).toEqual(['model-b']))
    expect(posts.every(p => p.models.length > 0)).toBe(true)
  })

  it.each([['A first', [0, 1]], ['B first', [1, 0]]])('keeps each slot its own reply when a save on another slot supersedes it (%s)', async (_, order) => {
    const { reply, release } = heldReplies()
    const { posts, store, reshow } = setup(NO_RANGE, {}, reply)
    await userEvent.click(modelCheck('model-b'))
    await waitFor(() => expect(posts).toHaveLength(1))
    reshow({ slot: OTHER_SLOT })
    await userEvent.click(modelCheck('model-c'))
    await waitFor(() => expect(posts).toHaveLength(2))
    order.forEach(release)
    await waitFor(() => expect(rangeOn(store, SLOT)).toEqual(posts[0]))
    await waitFor(() => expect(rangeOn(store, OTHER_SLOT)).toEqual(posts[1]))
  })

  it('stores a reply that lands after the picker closed', async () => {
    const { reply, release } = heldReplies()
    const { posts, store, unmount } = setup(NO_RANGE, {}, reply)
    await userEvent.click(modelCheck('model-b'))
    await waitFor(() => expect(posts).toHaveLength(1))
    unmount()
    release(0)
    await waitFor(() => expect(rangeOn(store, SLOT)).toEqual(posts[0]))
  })

  it('keeps the newer range when an older reply on the same slot lands last', async () => {
    const { reply, release } = heldReplies()
    const { posts, store, qc } = setup(NO_RANGE, {}, reply)
    await userEvent.click(modelCheck('model-b'))
    await waitFor(() => expect(posts).toHaveLength(1))
    await userEvent.click(modelCheck('model-c'))
    await waitFor(() => expect(posts).toHaveLength(2))
    release(1)
    await waitFor(() => expect(rangeOn(store, SLOT)).toEqual(posts[1]))
    release(0)
    await waitFor(() => expect(qc.getMutationCache().getAll().every(m => m.state.status === 'success')).toBe(true))
    expect(rangeOn(store, SLOT)).toEqual(posts[1])
  })

  it('keeps the newer range when the picker remounts between two saves on a slot', async () => {
    const { reply, release } = heldReplies()
    const { posts, store, qc, remount } = setup(NO_RANGE, {}, reply)
    await userEvent.click(modelCheck('model-b'))
    await waitFor(() => expect(posts).toHaveLength(1))
    remount()
    await userEvent.click(modelCheck('model-c'))
    await waitFor(() => expect(posts).toHaveLength(2))
    release(1)
    await waitFor(() => expect(rangeOn(store, SLOT)).toEqual(posts[1]))
    release(0)
    await waitFor(() => expect(qc.getMutationCache().getAll().every(m => m.state.status === 'success')).toBe(true))
    expect(rangeOn(store, SLOT)).toEqual(posts[1])
  })

  it("shows another slot's refusal on that slot only", async () => {
    const { reply, release } = heldReplies(true)
    const { posts, reshow } = setup(NO_RANGE, {}, reply)
    await userEvent.click(modelCheck('model-c'))
    await waitFor(() => expect(posts).toHaveLength(1))
    reshow({ slot: OTHER_SLOT })
    release(0)
    // Back on the slot that was refused, the notice is there: that also proves the reply landed.
    reshow({ slot: SLOT })
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    reshow({ slot: OTHER_SLOT })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('never shows or posts a range that is pending for another slot', async () => {
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    onTestFinished(() => release())
    const { posts, postedTo, reshow } = setup(NO_RANGE, {}, async body => {
      await gate
      return HttpResponse.json({ ok: true, jev_range: body })
    })
    await userEvent.click(modelCheck('model-b'))
    await waitFor(() => expect(posts).toEqual([{ models: ['model-a', 'model-c'], effort_min: '', effort_max: '' }]))
    reshow({ slot: OTHER_SLOT })
    expect(modelCheck('model-b')).toBeChecked()
    await userEvent.click(modelCheck('model-c'))
    await waitFor(() => expect(posts).toHaveLength(2))
    expect(posts[1]).toEqual({ models: ['model-a', 'model-b'], effort_min: '', effort_max: '' })
    expect(postedTo).toEqual([SLOT, OTHER_SLOT])
  })

  it('labels the ticks once, above them, as a hidden caption and no heading', () => {
    setup(NO_RANGE)
    const label = screen.getByText('Jev may use')
    expect(label.compareDocumentPosition(modelCheck('model-a')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(label).toHaveAttribute('aria-hidden', 'true')
    expect(screen.queryByRole('heading', { name: 'Jev may use', hidden: true })).toBeNull()
  })

  it('shows no effort span on a backend without effort support', () => {
    setup(NO_RANGE, { hasEffort: false })
    expect(screen.getAllByRole('checkbox')).toHaveLength(3)
    expect(screen.queryByRole('slider', { name: /effort Jev may use/ })).not.toBeInTheDocument()
  })

  it('names a refused save and keeps the previous range', async () => {
    const range = { models: ['model-a', 'model-b'], effort_min: '', effort_max: '' }
    const { posts } = setup(range, {}, () => HttpResponse.json({ error: 'jev_range_invalid' }, { status: 400 }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await userEvent.click(modelCheck('model-c'))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('Ask the agent')).toBeInTheDocument()
    expect(modelCheck('model-c')).not.toBeChecked()
    expect(modelCheck('model-a')).toBeChecked()
    expect(modelCheck('model-b')).toBeChecked()
  })

  it('shows no range controls with the preview off, even for a slot holding a range', () => {
    const { reshow } = setup({ models: ['model-a'], effort_min: 'low', effort_max: 'high' })
    expect(screen.getAllByRole('checkbox')).toHaveLength(3)
    // Preview off: `withJevRoute` drops the row, so the picker lists plain models and `Auto`.
    reshow({ models: MODELS.slice(1), activeModel: 'auto' })
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByText('Jev may use')).not.toBeInTheDocument()
    expect(screen.queryByRole('slider', { name: /effort Jev may use/ })).not.toBeInTheDocument()
  })
})
