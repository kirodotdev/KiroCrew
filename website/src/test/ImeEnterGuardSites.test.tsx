/**
 * IME Enter guard — site-level behaviour for the guard-less Enter-submit inputs
 * routed through `useImeGuard`.
 *
 * On WebKit the keydown that commits an IME candidate is dispatched AFTER
 * `compositionend`, where `KeyboardEvent.isComposing` already reads false. The
 * hook's 50ms latch is the only signal that survives into that window, so each
 * test arms the latch with compositionStart→compositionEnd and then fires the
 * committing Enter exactly as WebKit delivers it (native flag clear).
 *
 * One site per remedy, because the remedies differ and a regression in one is
 * invisible from the other:
 *  - textarea → `claimEnter` (UserMessage edit box): a declined Enter must ALSO
 *    be consumed, or the browser inserts a newline into the draft.
 *  - single-line input → `isComposing` early-return (TagManagerList create box):
 *    a declined Enter must NOT be consumed — nothing would be inserted, and
 *    claiming would suppress an implicit form submit where one is wanted.
 *  - picker input carrying arrows (ProjectPicker): only the Enter path is
 *    gated; arrow navigation keeps working with the latch armed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore, renderWithProviders } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import UserMessage from '../pages/chat/UserMessage'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      chatTags: vi.fn().mockResolvedValue([]),
      createChatTag: vi.fn().mockResolvedValue({ ok: true }),
      chatFolders: vi.fn().mockResolvedValue([]),
      recentProjects: vi.fn().mockResolvedValue({ dirs: [] }),
      browseDirs: vi.fn().mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] }),
    },
  }
})

import { api } from '../api/client'
import TagManagerList from '../components/TagManagerList'
import ProjectPicker from '../components/ProjectPicker'

/** Arm the hook's post-composition latch: the WebKit commit-Enter window. */
function armLatch(el: Element) {
  fireEvent.compositionStart(el)
  fireEvent.compositionEnd(el)
}

const renderContent = (content: string) => <span data-testid="content">{content}</span>

describe('UserMessage edit textarea — rule 1 textarea: claimEnter', () => {
  function openEditor(onEditResend = vi.fn()) {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={onEditResend} messageIndex={0} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    const ta = screen.getByLabelText('Edit message') as HTMLTextAreaElement
    return { ta, onEditResend }
  }

  it('declines the committing Enter in the post-composition window AND consumes it (no newline)', () => {
    const { ta, onEditResend } = openEditor()
    fireEvent.change(ta, { target: { value: '你好' } })
    armLatch(ta)
    // WebKit shape: compositionend already fired, native isComposing is false.
    const defaultNotPrevented = fireEvent.keyDown(ta, { key: 'Enter' })
    expect(onEditResend).not.toHaveBeenCalled()
    // claimEnter consumed the declined key — an unclaimed Enter would insert a
    // literal newline into the draft the user is about to send.
    expect(defaultNotPrevented).toBe(false)
  })

  it('leaves a mid-composition Enter to the IME (native flag set → no preventDefault)', () => {
    const { ta, onEditResend } = openEditor()
    fireEvent.change(ta, { target: { value: '你好' } })
    fireEvent.compositionStart(ta)
    const defaultNotPrevented = fireEvent.keyDown(ta, { key: 'Enter', isComposing: true })
    expect(onEditResend).not.toHaveBeenCalled()
    // Cancelling the default here would risk the candidate commit the same
    // keypress carries; the guard must not touch a key the IME is consuming.
    expect(defaultNotPrevented).toBe(true)
  })

  it('submits on a plain Enter (positive control)', () => {
    const { ta, onEditResend } = openEditor()
    fireEvent.change(ta, { target: { value: 'edited text' } })
    fireEvent.keyDown(ta, { key: 'Enter' })
    expect(onEditResend).toHaveBeenCalledTimes(1)
  })
})

describe('TagManagerList create input — rule 1 single-line input: isComposing only', () => {
  function renderList() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={createTestStore()}>
        <MemoryRouter>
          <QueryClientProvider client={qc}>
            <ThemeProvider>
              <TagManagerList mode="manage" />
            </ThemeProvider>
          </QueryClientProvider>
        </MemoryRouter>
      </Provider>,
    )
    return screen.findByTestId('tag-create') as Promise<HTMLInputElement>
  }

  it('does NOT create a tag on the committing Enter in the post-composition window', async () => {
    const input = await renderList()
    fireEvent.change(input, { target: { value: 'タグ' } })
    armLatch(input)
    const defaultNotPrevented = fireEvent.keyDown(input, { key: 'Enter' })
    // Flush the mutation's microtask queue — react-query's mutate() defers the
    // mutationFn, so a synchronous not-called assertion would pass vacuously.
    await act(async () => { await Promise.resolve() })
    expect(vi.mocked(api.createChatTag)).not.toHaveBeenCalled()
    // Single-line input: the guard declines, it does not claim. Consuming the
    // key here would be over-reach (it would suppress an implicit form submit
    // on form-hosted inputs of this shape).
    expect(defaultNotPrevented).toBe(true)
  })

  it('creates the tag on a plain Enter without consuming the key (implicit submit preserved)', async () => {
    const input = await renderList()
    fireEvent.change(input, { target: { value: 'ops' } })
    const defaultNotPrevented = fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(vi.mocked(api.createChatTag)).toHaveBeenCalledWith('ops', undefined, undefined))
    expect(defaultNotPrevented).toBe(true)
  })
})

describe('ProjectPicker path input — rule 2: gate the Enter path only', () => {
  const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
    top, left, width, height,
    bottom: top + height,
    right: left + width,
    x: left, y: top,
    toJSON: () => ({}),
  } as DOMRect)

  beforeEach(() => {
    vi.useFakeTimers()
    vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
    vi.mocked(api.browseDirs).mockResolvedValue({
      path: '/home/u',
      parent: '/home',
      dirs: [
        { name: 'alpha', path: '/home/u/alpha' },
        { name: 'beta', path: '/home/u/beta' },
      ],
    })
  })
  afterEach(() => {
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
  })

  async function openBrowse() {
    renderWithProviders(
      <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />,
    )
    // Drain the initial browse() + recentProjects() promises (empty recents
    // auto-switch the picker to the Browse tab).
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    return screen.getByPlaceholderText('/path/to/project') as HTMLInputElement
  }

  it('keeps arrow-key list navigation working while the latch is armed', async () => {
    const input = await openBrowse()
    armLatch(input)
    expect(input.getAttribute('aria-activedescendant')).toBe('pp-dir-0')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    // Claiming the whole handler would have broken this: only Enter is gated.
    expect(input.getAttribute('aria-activedescendant')).toBe('pp-dir-1')
  })

  it('does NOT drill into the highlighted folder on the committing Enter', async () => {
    const input = await openBrowse()
    armLatch(input)
    vi.mocked(api.browseDirs).mockClear()
    fireEvent.keyDown(input, { key: 'Enter' })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(vi.mocked(api.browseDirs)).not.toHaveBeenCalled()
  })

  it('drills into the highlighted folder on a plain Enter (positive control)', async () => {
    const input = await openBrowse()
    vi.mocked(api.browseDirs).mockClear()
    fireEvent.keyDown(input, { key: 'Enter' })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(vi.mocked(api.browseDirs)).toHaveBeenCalledWith('/home/u/alpha')
  })

  it('lets Enter through again once the 50ms post-composition window expires', async () => {
    const input = await openBrowse()
    armLatch(input)
    // Advance past the latch window — the WebKit commit-Enter hazard is over,
    // so the next Enter is a deliberate one and must act normally.
    await act(async () => { await vi.advanceTimersByTimeAsync(60) })
    vi.mocked(api.browseDirs).mockClear()
    fireEvent.keyDown(input, { key: 'Enter' })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(vi.mocked(api.browseDirs)).toHaveBeenCalledWith('/home/u/alpha')
  })
})
