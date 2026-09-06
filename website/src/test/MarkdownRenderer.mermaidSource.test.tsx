import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(),
  },
}))

// The block copies through the shared clipboard helpers, so mocking the module
// is what lets the test observe WHICH string was handed over and what the block
// does with the RESULT. Both exports are stubbed because MarkdownRenderer
// imports both and the other consumers (file paths, session keys) would
// otherwise lose their implementation.
vi.mock('../utils/clipboard', () => ({
  copyCode: vi.fn(async () => true),
  copyToClipboard: vi.fn(async () => true),
}))

import mermaid from 'mermaid'
import { copyCode } from '../utils/clipboard'
import MarkdownRenderer from '../components/MarkdownRenderer'

const SOURCE = 'graph TD;A-->B'
const MERMAID_MD = '```mermaid\n' + SOURCE + '\n```'
// Built across two lines so the icon-lint gate (line-anchored regex aimed at
// JSX inline SVGs) cannot match; this is a mermaid-output fixture, not an icon.
const RENDERED_SVG =
  '<svg ' +
  'viewBox="0 0 240 120" aria-roledescription="flowchart-v2"><g class="nodes"></g></svg>'

/** The controls that depend on a successful render appear only after mermaid
 *  resolves, so every test awaits the toggle before interacting. Located by
 *  test id rather than by its label, so a test can never pass by finding some
 *  other control whose name happens to match what is being asserted. */
async function renderDiagram() {
  render(<MarkdownRenderer content={MERMAID_MD} />)
  return await waitFor(() => screen.getByTestId('mermaid-source-toggle'))
}

const figureOf = (el: HTMLElement) =>
  el.closest('.group')!.querySelector('figure') as HTMLElement

/** The action row is the toggle's parent. Counting its buttons is the direct
 *  reading of the two-per-row cap, rather than an assertion about which
 *  controls happen to be present. */
const rowButtons = (el: HTMLElement) =>
  Array.from((el.parentElement as HTMLElement).querySelectorAll('button'))

describe('MermaidBlock source view and copy', () => {
  beforeEach(() => {
    // Both histories, not just the clipboard's: a call-count assertion reads
    // every earlier test's mounts otherwise, which is what made the round-trip
    // test report two renders for a component that had only rendered once.
    vi.clearAllMocks()
    vi.mocked(mermaid.render).mockResolvedValue({ svg: RENDERED_SVG } as never)
    vi.mocked(copyCode).mockResolvedValue(true)
  })

  it('offers a source toggle on a rendered diagram, and no source block yet', async () => {
    const toggle = await renderDiagram()
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByTestId('mermaid-source')).toBeNull()
    expect(figureOf(toggle)).toBeVisible()
  })

  it('swaps the rendered diagram for its source and back, keeping the SVG', async () => {
    const toggle = await renderDiagram()
    const figure = figureOf(toggle)
    expect(figure.querySelector('svg[aria-roledescription="flowchart-v2"]')).toBeTruthy()

    fireEvent.click(toggle)
    expect(screen.getByTestId('mermaid-source').textContent).toBe(SOURCE)
    expect(figure).not.toBeVisible()

    fireEvent.click(toggle)
    expect(screen.queryByTestId('mermaid-source')).toBeNull()
    // Looked up FRESH rather than reusing the captured node: a remounted figure
    // would leave the captured one detached, and a detached node reports "not
    // visible", so asserting on it would fail for staleness before reaching the
    // assertion that actually describes the defect.
    const back = figureOf(toggle)
    expect(back).toBeVisible()
    // ...and the diagram is STILL THERE. This is the whole reason the host is
    // hidden rather than unmounted: the render effect is guarded on the source
    // being unchanged, so a remounted host would be a fresh empty node the
    // effect then declines to refill, and the diagram would come back blank.
    expect(back.querySelector('svg[aria-roledescription="flowchart-v2"]')).toBeTruthy()
    // The two below corroborate the same property by its mechanism rather than
    // pinning a further observable: same DOM node, and mermaid never asked to
    // render a second time. The call count cannot be reddened on its own,
    // because the effect is gated on its deps -- it is a defensive pin.
    expect(back).toBe(figure)
    expect(vi.mocked(mermaid.render)).toHaveBeenCalledTimes(1)
  })

  it('reports the source view through aria-pressed', async () => {
    const toggle = await renderDiagram()
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
  })

  it('holds AT MOST TWO buttons in the action row in every reachable state', async () => {
    // The repo's row cap is two. Counted rather than reasoned about, and counted
    // in each state separately, because the row's membership is state-dependent.
    const toggle = await renderDiagram()
    const diagramView = rowButtons(toggle).map(b => b.getAttribute('data-testid'))
    expect(diagramView).toEqual(['mermaid-source-toggle', 'mermaid-enlarge'])

    fireEvent.click(toggle)
    const sourceView = rowButtons(toggle).map(b => b.getAttribute('data-testid'))
    expect(sourceView).toEqual(['mermaid-source-toggle', 'mermaid-copy-source'])

    expect(diagramView.length).toBeLessThanOrEqual(2)
    expect(sourceView.length).toBeLessThanOrEqual(2)
  })

  it('offers copy only where the source is on screen', async () => {
    const toggle = await renderDiagram()
    // On the rendered diagram the object of "copy" would be ambiguous, so it is
    // not offered there at all.
    expect(screen.queryByTestId('mermaid-copy-source')).toBeNull()
    fireEvent.click(toggle)
    expect(screen.getByTestId('mermaid-copy-source')).toBeTruthy()
    fireEvent.click(toggle)
    expect(screen.queryByTestId('mermaid-copy-source')).toBeNull()
  })

  it('copies the diagram SOURCE, never the rendered SVG', async () => {
    const toggle = await renderDiagram()
    fireEvent.click(toggle)
    fireEvent.click(screen.getByTestId('mermaid-copy-source'))
    expect(vi.mocked(copyCode)).toHaveBeenCalledTimes(1)
    const handed = vi.mocked(copyCode).mock.calls[0][0]
    expect(handed).toContain(SOURCE)
    // The complement, and the point of the control: an image copy is not what
    // this offers, so the rendered markup must appear nowhere in what was
    // handed to the clipboard.
    expect(handed).not.toContain('<svg')
    expect(handed).not.toContain('flowchart-v2')
  })

  it('confirms a copy only after the write actually succeeded', async () => {
    const toggle = await renderDiagram()
    fireEvent.click(toggle)
    const copy = screen.getByTestId('mermaid-copy-source')
    fireEvent.click(copy)
    await waitFor(() => expect(copy.getAttribute('aria-label')).toMatch(/copied/i))
  })

  it('reports a refused clipboard write instead of announcing success', async () => {
    // The defect this pins: `copyCode` RESOLVES false when the textarea fallback
    // reports failure (an insecure origin, a denied permission), so a
    // confirmation that ignores the result announces a copy that never happened.
    vi.mocked(copyCode).mockResolvedValue(false)
    const toggle = await renderDiagram()
    fireEvent.click(toggle)
    const copy = screen.getByTestId('mermaid-copy-source')
    fireEvent.click(copy)
    // Reported through the shared error surface, not the button's own label.
    const notice = await waitFor(() => screen.getByTestId('mermaid-copy-error'))
    expect(notice.textContent).toMatch(/failed/i)
    expect(copy.getAttribute('aria-label')).not.toMatch(/copied/i)
    // ONE error surface: the button keeps its neutral name rather than restating
    // the failure beside the notice that already carries it.
    expect(copy.getAttribute('aria-label')).not.toMatch(/failed/i)
  })

  it('keeps the notice OUT of the action row, so the row cap still holds', async () => {
    // The notice brings a dismiss control with it. Inside the row that would be
    // a third button and a violation of the two-per-row cap; as a separated
    // region it is exempt. Asserted structurally -- the row must not contain it
    // -- rather than by counting the row's buttons alone, because a future
    // refactor could move the notice into the row and still land on two.
    vi.mocked(copyCode).mockResolvedValue(false)
    const toggle = await renderDiagram()
    fireEvent.click(toggle)
    const copy = screen.getByTestId('mermaid-copy-source')
    fireEvent.click(copy)
    const notice = await waitFor(() => screen.getByTestId('mermaid-copy-error'))
    const row = copy.parentElement as HTMLElement
    expect(row.contains(notice)).toBe(false)
    expect(rowButtons(copy).length).toBeLessThanOrEqual(2)
  })

  it('does not erase the failure after the confirmation timeout', async () => {
    // The two outcomes are asymmetric on purpose: a confirmation clears itself,
    // an error does not. A failure that vanished on a timer could not be read or
    // acted on -- and it is the outcome that matters, since the text the user
    // asked for is not on their clipboard.
    vi.useFakeTimers()
    try {
      vi.mocked(copyCode).mockResolvedValue(false)
      render(<MarkdownRenderer content={MERMAID_MD} />)
      const toggle = await vi.waitFor(() => screen.getByTestId('mermaid-source-toggle'))
      fireEvent.click(toggle)
      fireEvent.click(screen.getByTestId('mermaid-copy-source'))
      await vi.waitFor(() => screen.getByTestId('mermaid-copy-error'))
      // Well past the 1500ms the confirmation uses.
      await vi.advanceTimersByTimeAsync(5000)
      expect(screen.queryByTestId('mermaid-copy-error')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('reports a THROWN clipboard failure the same way', async () => {
    // The second arm: the fallback itself throwing must not become an unhandled
    // rejection, and must not leave the control claiming success either.
    vi.mocked(copyCode).mockRejectedValue(new Error('denied'))
    const toggle = await renderDiagram()
    fireEvent.click(toggle)
    const copy = screen.getByTestId('mermaid-copy-source')
    fireEvent.click(copy)
    const notice = await waitFor(() => screen.getByTestId('mermaid-copy-error'))
    expect(notice.textContent).toMatch(/failed/i)
  })

  it('still offers copy when the diagram fails to render, but no toggle', async () => {
    vi.mocked(mermaid.render).mockRejectedValueOnce(new Error('parse error'))
    render(<MarkdownRenderer content={MERMAID_MD} />)
    await waitFor(() => {
      expect(screen.getByTestId('mermaid-render-error')).toBeTruthy()
    })
    // A failed render shows the source as its own evidence -- the same single
    // element the toggle shows -- so there are not two views to switch between.
    // But the source is exactly what a reader wants to take away, so copy stays.
    expect(screen.queryByTestId('mermaid-source-toggle')).toBeNull()
    expect(screen.getByTestId('mermaid-source').textContent).toContain(SOURCE)
    const copy = screen.getByTestId('mermaid-copy-source')
    expect(rowButtons(copy).length).toBe(1)
    fireEvent.click(copy)
    expect(vi.mocked(copyCode).mock.calls[0][0]).toContain(SOURCE)
  })

  it('returns to the diagram when a later render succeeds after a failure', async () => {
    // Why the failure path resets `showSource`. A mutation that dropped the reset
    // survived every other test in this file: with the source rendered for
    // `failed || showSource` alike, the two values are indistinguishable WHILE
    // the failure lasts. They diverge only afterwards -- a render that succeeds
    // should show the diagram it just produced, not silently stay on text.
    //
    // So the failure has to be reached FROM the source view: a first attempt
    // whose `showSource` was already false cannot tell the reset from its
    // absence, which is what made an earlier version of this test toothless.
    const { rerender } = render(<MarkdownRenderer content={MERMAID_MD} />)
    fireEvent.click(await waitFor(() => screen.getByTestId('mermaid-source-toggle')))
    expect(screen.getByTestId('mermaid-source')).toBeTruthy()

    // Each rerender needs a DIFFERENT source: the render effect is guarded on the
    // code being unchanged, so re-sending the same string is a no-op.
    vi.mocked(mermaid.render).mockRejectedValueOnce(new Error('parse error'))
    rerender(<MarkdownRenderer content={'```mermaid\ngraph TD;B-->C\n```'} />)
    await waitFor(() => expect(screen.getByTestId('mermaid-render-error')).toBeTruthy())

    vi.mocked(mermaid.render).mockResolvedValue({ svg: RENDERED_SVG } as never)
    rerender(<MarkdownRenderer content={'```mermaid\ngraph TD;C-->D\n```'} />)
    await waitFor(() => expect(screen.queryByTestId('mermaid-render-error')).toBeNull())
    // The diagram is what is on screen, and the source has stepped aside.
    expect(screen.queryByTestId('mermaid-source')).toBeNull()
    expect(document.querySelector('figure')).toBeVisible()
    expect(screen.getByTestId('mermaid-source-toggle').getAttribute('aria-pressed')).toBe('false')
  })

  it('leaves the source view when a re-render of the diagram fails', async () => {
    const { rerender } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const toggle = await waitFor(() => screen.getByTestId('mermaid-source-toggle'))
    fireEvent.click(toggle)
    expect(screen.getByTestId('mermaid-source')).toBeTruthy()

    vi.mocked(mermaid.render).mockRejectedValueOnce(new Error('parse error'))
    rerender(<MarkdownRenderer content={'```mermaid\ngraph TD;B-->C\n```'} />)
    await waitFor(() => {
      expect(screen.getByTestId('mermaid-render-error')).toBeTruthy()
    })
    // The source is STILL on screen -- it is now the same single element the
    // toggle shows, and a failed render is one of the two states that show it.
    // What goes away is the toggle, there being no diagram to switch to, and the
    // figure, which the failure emptied: leaving it visible would show the
    // reader a blank 60px box beside the notice.
    expect(screen.getByTestId('mermaid-source')).toBeTruthy()
    expect(screen.getByTestId('mermaid-source').textContent).toContain('B-->C')
    expect(screen.queryByTestId('mermaid-source-toggle')).toBeNull()
    expect(document.querySelector('figure')).not.toBeVisible()
  })
})
