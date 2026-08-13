import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(),
  },
}))

import mermaid from 'mermaid'
import MarkdownRenderer from '../components/MarkdownRenderer'

const MERMAID_MD = '```mermaid\ngraph TD;A-->B\n```'
// Built across two lines so the icon-lint gate (line-anchored regex aimed at
// JSX inline SVGs) cannot match; this is a mermaid-output fixture, not an icon.
const RENDERED_SVG =
  '<svg ' +
  'viewBox="0 0 240 120" aria-roledescription="flowchart-v2"><g class="nodes"></g></svg>'

/** The enlarge trigger appears only after mermaid resolves, so every test
 *  awaits it before interacting. */
async function renderDiagram() {
  render(<MarkdownRenderer content={MERMAID_MD} />)
  return await waitFor(() =>
    screen.getByRole('button', { name: /enlarge diagram/i })
  )
}

describe('MermaidBlock click-to-enlarge', () => {
  beforeEach(() => {
    vi.mocked(mermaid.render).mockResolvedValue({ svg: RENDERED_SVG } as never)
  })

  it('renders an enlarge trigger for a successfully rendered diagram', async () => {
    const trigger = await renderDiagram()
    expect(trigger).toBeTruthy()
    // No overlay until the trigger is activated.
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('does NOT render the enlarge trigger when the diagram fails to render', async () => {
    vi.mocked(mermaid.render).mockRejectedValueOnce(new Error('parse error'))
    render(<MarkdownRenderer content={MERMAID_MD} />)
    // The failure fallback is the source text in a <pre>; wait for it so the
    // negative assertion runs after the async render settled.
    await waitFor(() => {
      expect(document.querySelector('pre')?.textContent).toContain('graph TD')
    })
    expect(screen.queryByRole('button', { name: /enlarge diagram/i })).toBeNull()
  })

  it('activating the trigger opens a modal dialog containing the diagram SVG', async () => {
    const trigger = await renderDiagram()
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    // The overlay hosts the live SVG (not a rasterized copy), scaled to fill
    // the viewport box; viewBox is preserved so aspect ratio is intact.
    // (Selector excludes the lucide icon SVGs in the overlay's own controls.)
    const svg = dialog.querySelector('svg[aria-roledescription="flowchart-v2"]') as SVGElement | null
    expect(svg).toBeTruthy()
    expect(svg!.getAttribute('viewBox')).toBe('0 0 240 120')
    expect(svg!.style.width).toBe('100%')
    expect(svg!.style.maxWidth).toBe('none')
  })

  it('Escape closes the overlay and returns focus to the trigger', async () => {
    const trigger = await renderDiagram()
    trigger.focus()
    fireEvent.click(trigger)
    expect(screen.getByRole('dialog')).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })

  it('claims Escape (preventDefault) so an enclosing Modal does not also dismiss', async () => {
    // Modal.tsx guards its window Escape listener on !e.defaultPrevented; the
    // lightbox must mark the event consumed or one keypress tears down both
    // layers (regression contract documented in Modal.escape.test.tsx).
    const trigger = await renderDiagram()
    fireEvent.click(trigger)
    expect(screen.getByRole('dialog')).toBeTruthy()
    const esc = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
    window.dispatchEvent(esc)
    expect(esc.defaultPrevented).toBe(true)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('closes the overlay when a re-render of the diagram fails', async () => {
    // If `enlarged` survived the failure, the overlay would silently re-open on
    // the next successful render with no user action.
    const { rerender } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const trigger = await waitFor(() =>
      screen.getByRole('button', { name: /enlarge diagram/i })
    )
    fireEvent.click(trigger)
    expect(screen.getByRole('dialog')).toBeTruthy()
    vi.mocked(mermaid.render).mockRejectedValueOnce(new Error('parse error'))
    rerender(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->C\n```'} />)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    // Recovery: a later successful render must NOT resurrect the overlay.
    rerender(<MarkdownRenderer content={MERMAID_MD} />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /enlarge diagram/i })).toBeTruthy()
    )
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('the close button closes the overlay', async () => {
    const trigger = await renderDiagram()
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('button', { name: /^close$/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('clicking the backdrop (outside the diagram) closes the overlay', async () => {
    const trigger = await renderDiagram()
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog')
    fireEvent.click(dialog)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('clicking the rendered diagram itself opens the overlay (pointer convenience path)', async () => {
    await renderDiagram()
    const figure = document.querySelector('figure')
    expect(figure).toBeTruthy()
    fireEvent.click(figure!)
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('clicking the diagram inside the overlay does NOT close it', async () => {
    const trigger = await renderDiagram()
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog')
    fireEvent.click(dialog.querySelector('svg[aria-roledescription="flowchart-v2"]')!)
    expect(screen.queryByRole('dialog')).toBeTruthy()
  })
})
