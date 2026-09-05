import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import ShareMessageModal from './ShareMessageModal'
import { SHARE_REPO_URL } from './shareSupport'

// The modal imports html-to-image on demand; jsdom has no canvas, so the mock
// stands in for the rasterizer everywhere.
const toBlobMock = vi.fn(async () => new Blob(['png-bytes'], { type: 'image/png' }))
vi.mock('html-to-image', () => ({ toBlob: (...args: unknown[]) => toBlobMock(...args) }))

describe('ShareMessageModal', () => {
  beforeEach(() => {
    toBlobMock.mockClear()
    // jsdom lacks object URLs; downloadBlob needs both halves.
    URL.createObjectURL = vi.fn(() => 'blob:mock')
    URL.revokeObjectURL = vi.fn()
  })
  afterEach(() => { vi.restoreAllMocks() })

  const renderModal = (over: Partial<Parameters<typeof ShareMessageModal>[0]> = {}) =>
    render(
      <ShareMessageModal
        onClose={over.onClose ?? (() => {})}
        messageText={over.messageText ?? 'Triaged 47 issues overnight and opened two PRs.'}
        prevUserText={over.prevUserText}
        shareEnabled={over.shareEnabled ?? true}
      />,
    )

  it('renders the card with the message excerpt and a prefilled caption', () => {
    renderModal()
    expect(screen.getByTestId('share-card')).toHaveTextContent('Triaged 47 issues overnight')
    const caption = screen.getByRole('textbox', { name: 'Post text' }) as HTMLTextAreaElement
    expect(caption.value).toContain(SHARE_REPO_URL)
    // The template interpolates {{productName}} rather than hardcoding it.
    expect(caption.value).toMatch(/^Kiro Crew /)
  })

  it('pairs the question by default and drops it when unchecked', () => {
    renderModal({ prevUserText: 'How did tonight go?' })
    expect(screen.getByTestId('share-card')).toHaveTextContent('How did tonight go?')
    fireEvent.click(screen.getByRole('checkbox', { name: 'Include my question' }))
    expect(screen.getByTestId('share-card')).not.toHaveTextContent('How did tonight go?')
  })

  it('shows no question toggle without a preceding user message', () => {
    renderModal()
    expect(screen.queryByRole('checkbox', { name: 'Include my question' })).toBeNull()
  })

  it('warns with CARD location when the shared message looks sensitive', () => {
    renderModal({ messageText: 'set AWS_KEY=AKIAIOSFODNN7EXAMPLE' })
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('the card contains: an AWS access key')
    expect(alert).not.toHaveTextContent('post text')
  })

  it('warns with POST-TEXT location when a sensitive value is typed into the caption', () => {
    renderModal()
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.change(screen.getByRole('textbox', { name: 'Post text' }), {
      target: { value: 'token ghp_abcdefghijklmnopqrstu0123456789' },
    })
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('the post text contains: an API token')
    expect(alert).not.toHaveTextContent('the card contains')
  })

  it('pre-opens the composer tab synchronously, copies the card, then navigates it', async () => {
    vi.stubGlobal('ClipboardItem', class { constructor(_items: unknown) {} })
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { write }, configurable: true })
    // The tab is opened blank inside the click's own call stack (what popup
    // blockers judge), with its opener severed, and pointed at the composer
    // only after the export has settled.
    const tab = { opener: {} as unknown, location: { href: '' } }
    const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    renderModal()
    fireEvent.change(screen.getByRole('textbox', { name: 'Post text' }), { target: { value: 'wow' } })
    fireEvent.click(screen.getByTestId('share-x'))
    expect(open).toHaveBeenCalledWith('', '_blank')
    expect(tab.opener).toBeNull()
    await waitFor(() => expect(tab.location.href).toBe('https://x.com/intent/post?text=wow'))
    expect(write).toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('share-linkedin'))
    await waitFor(() => expect(tab.location.href).toBe('https://www.linkedin.com/feed/?shareActive=true&text=wow'))
  })

  it('falls back to a direct open when the blocker refused the pre-opened tab', async () => {
    vi.stubGlobal('ClipboardItem', class { constructor(_items: unknown) {} })
    Object.defineProperty(navigator, 'clipboard', { value: { write: vi.fn().mockResolvedValue(undefined) }, configurable: true })
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    renderModal()
    fireEvent.change(screen.getByRole('textbox', { name: 'Post text' }), { target: { value: 'wow' } })
    fireEvent.click(screen.getByTestId('share-x'))
    await waitFor(() => expect(open).toHaveBeenCalledWith('https://x.com/intent/post?text=wow', '_blank', 'noopener,noreferrer'))
  })

  it('shows a focus ring on the editable card text and mirrors edits into the scan', () => {
    renderModal()
    const excerptBox = screen.getAllByRole('textbox', { name: /card text/i })[0]
    fireEvent.focus(excerptBox)
    expect(excerptBox.style.boxShadow).toContain('rgba(255,255,255')
    fireEvent.blur(excerptBox)
    expect(excerptBox.style.boxShadow).toBe('')
    // jsdom does not compute innerText from DOM edits; pin the value the
    // handler reads so the mirrored-scan path is what's exercised.
    Object.defineProperty(excerptBox, 'innerText', { value: 'now with AKIAIOSFODNN7EXAMPLE inside', configurable: true })
    fireEvent.input(excerptBox)
    expect(screen.getByRole('alert')).toHaveTextContent('an AWS access key')
  })

  it('scans edits made to the question bubble and clears them when unchecked', () => {
    renderModal({ prevUserText: 'How did tonight go?' })
    const questionBox = screen.getAllByRole('textbox', { name: /card text/i })[0]
    Object.defineProperty(questionBox, 'innerText', { value: 'psst ghp_abcdefghijklmnopqrstu0123456789', configurable: true })
    fireEvent.input(questionBox)
    expect(screen.getByRole('alert')).toHaveTextContent('an API token')
    // Unchecking drops the question (and its edit mirror) from the scan.
    fireEvent.click(screen.getByRole('checkbox', { name: 'Include my question' }))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('scales the preview to fit a narrow container while keeping the export width', async () => {
    // The suite setup pins a no-op ResizeObserver as an own property of
    // `window`, which is what the component's bare identifier resolves to —
    // so the capturing stub must be installed there, not via stubGlobal.
    const w = window as unknown as { ResizeObserver: unknown }
    const original = w.ResizeObserver
    let roCallback: (() => void) | null = null
    w.ResizeObserver = class {
      constructor(cb: () => void) { roCallback = cb }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    try {
      renderModal()
      const card = screen.getByTestId('share-card')
      const scaleWrap = card.parentElement as HTMLElement
      const fitEl = scaleWrap.parentElement as HTMLElement
      Object.defineProperty(fitEl, 'clientWidth', { value: 260, configurable: true })
      Object.defineProperty(card, 'offsetHeight', { value: 400, configurable: true })
      await waitFor(() => expect(roCallback).not.toBeNull())
      act(() => { roCallback!() })
      expect(scaleWrap.style.transform).toBe('scale(0.5)')
      // The spacer takes the scaled height so no dead gap is left below.
      expect(fitEl.style.height).toBe('200px')
      // The card node itself keeps the full export width.
      expect(card.style.width).toBe('520px')
    } finally {
      w.ResizeObserver = original
    }
  })

  it('flags the caption count once it passes the X limit', () => {
    renderModal()
    fireEvent.change(screen.getByRole('textbox', { name: 'Post text' }), { target: { value: 'x'.repeat(300) } })
    const counter = screen.getByText(/300 \/ 280/)
    expect(counter.className).toContain('text-danger')
  })

  it('exports at 2x and downloads on the download action', async () => {
    renderModal()
    fireEvent.click(screen.getByTestId('share-download'))
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled())
    expect(toBlobMock).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ pixelRatio: 2 }))
  })

  it('reports success when the clipboard write goes through', async () => {
    vi.stubGlobal('ClipboardItem', class { constructor(_items: unknown) {} })
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { write }, configurable: true })
    renderModal()
    fireEvent.click(screen.getByTestId('share-copy'))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/paste the image/i))
    expect(write).toHaveBeenCalled()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('falls back to a download when the clipboard write is refused', async () => {
    // Firefox / denied permission: every ClipboardItem write rejects, so the
    // image must still reach the user as a file, never a dead button.
    vi.stubGlobal('ClipboardItem', class { constructor(_items: unknown) {} })
    const write = vi.fn().mockRejectedValue(new Error('NotAllowedError'))
    Object.defineProperty(navigator, 'clipboard', { value: { write }, configurable: true })
    renderModal()
    fireEvent.click(screen.getByTestId('share-copy'))
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled())
    expect(write).toHaveBeenCalledTimes(2) // multi-type item, then image-only retry
    expect(screen.getByRole('status')).toHaveTextContent(/downloaded/i)
  })

  it('keeps the compose and its edits when policy withdraws sharing mid-dialog, and says why', async () => {
    // A centrally pushed policy can flip `capabilities.social_share` while this
    // dialog is open. Unmounting would destroy the user's edited caption with
    // no explanation; instead the actions are withdrawn and a notice names the
    // cause, while the text the user typed stays where they left it.
    const onClose = vi.fn()
    const view = render(
      <ShareMessageModal onClose={onClose} messageText="Triaged 47 issues overnight." shareEnabled />,
    )
    const caption = screen.getByRole('textbox', { name: 'Post text' }) as HTMLTextAreaElement
    fireEvent.change(caption, { target: { value: 'my carefully edited caption' } })
    // The card's text is editable too and vanishes on close just like the
    // caption; the salvage copy must carry that edit as well.
    const excerptBox = screen.getAllByRole('textbox', { name: /card text/i })[0]
    Object.defineProperty(excerptBox, 'innerText', { value: 'my edited card excerpt', configurable: true })
    fireEvent.input(excerptBox)
    expect(screen.queryByTestId('share-withdrawn')).toBeNull()
    expect(screen.getByTestId('share-x')).not.toBeDisabled()

    view.rerender(
      <ShareMessageModal onClose={onClose} messageText="Triaged 47 issues overnight." shareEnabled={false} />,
    )
    expect(screen.getByTestId('share-withdrawn')).toHaveTextContent(/policy/i)
    for (const id of ['share-download', 'share-copy', 'share-x', 'share-linkedin']) {
      expect(screen.getByTestId(id)).toBeDisabled()
    }
    // The edit survived the flip; nothing was silently discarded.
    expect((screen.getByRole('textbox', { name: 'Post text' }) as HTMLTextAreaElement).value)
      .toBe('my carefully edited caption')
    // One local salvage path stays: plain-text copy of everything the user
    // could have edited (no image, no third-party site), so Close never means
    // silent loss.
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    fireEvent.click(screen.getByTestId('share-copy-text'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('my carefully edited caption\n\nmy edited card excerpt'))
    // The user still decides when to leave.
    expect(onClose).not.toHaveBeenCalled()
  })

  it('says so and selects the caption when the withdrawn-state text copy is refused', async () => {
    // The notice points the user at this one button; on a host where the
    // clipboard refuses (plain HTTP) it must not look like success or sit
    // inert — it reports the failure and leaves the text selected so a
    // keyboard copy is the next keystroke.
    renderModal({ shareEnabled: false })
    const caption = screen.getByRole('textbox', { name: 'Post text' }) as HTMLTextAreaElement
    fireEvent.change(caption, { target: { value: 'keep this' } })
    // No async Clipboard API, and the legacy fallback reports failure.
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    const execBefore = Object.getOwnPropertyDescriptor(document, 'execCommand')
    const execCommand = vi.fn(() => false)
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })
    const select = vi.spyOn(caption, 'select')
    try {
      fireEvent.click(screen.getByTestId('share-copy-text'))
      await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'))
      expect(screen.getByTestId('share-copy-text')).toHaveTextContent(/failed/i)
      expect(screen.getByTestId('share-copy-text-unavailable')).toHaveTextContent(/clipboard is blocked.*Ctrl\+C/i)
      expect(select).toHaveBeenCalled()
      expect(caption.value).toBe('keep this')
    } finally {
      if (execBefore) Object.defineProperty(document, 'execCommand', execBefore)
      else delete (document as unknown as Record<string, unknown>).execCommand
    }
  })

  it('does not hand the caption to the third-party site when policy withdraws sharing mid-export', async () => {
    // The intent click pre-opens a blank tab, then awaits the export. If the
    // permission flips during that await, the navigation that carries the
    // caption off the machine must not happen, and the blank tab is closed.
    vi.stubGlobal('ClipboardItem', class { constructor(_items: unknown) {} })
    Object.defineProperty(navigator, 'clipboard', { value: { write: vi.fn().mockResolvedValue(undefined) }, configurable: true })
    let releaseExport!: (b: Blob) => void
    toBlobMock.mockImplementationOnce(() => new Promise<Blob>(res => { releaseExport = res }))
    const tab = { opener: {} as unknown, location: { href: '' }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    const view = render(
      <ShareMessageModal onClose={() => {}} messageText="Triaged 47 issues overnight." shareEnabled />,
    )
    fireEvent.click(screen.getByTestId('share-x'))
    // html-to-image is imported on demand, so the rasterizer is reached a tick
    // after the click; wait for the export to actually be in flight.
    await waitFor(() => expect(toBlobMock).toHaveBeenCalled())
    // Policy lands while the export is still in flight…
    view.rerender(
      <ShareMessageModal onClose={() => {}} messageText="Triaged 47 issues overnight." shareEnabled={false} />,
    )
    await act(async () => { releaseExport(new Blob(['png'], { type: 'image/png' })); await Promise.resolve() })
    await waitFor(() => expect(tab.close).toHaveBeenCalledTimes(1))
    // …so the pre-opened tab never receives the intent URL.
    expect(tab.location.href).toBe('')
  })
})
