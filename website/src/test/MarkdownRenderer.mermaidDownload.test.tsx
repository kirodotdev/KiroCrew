import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'

vi.mock('mermaid', () => ({ default: { initialize: vi.fn(), render: vi.fn() } }))
import userEvent from '@testing-library/user-event'
vi.mock('html-to-image', () => ({ toBlob: vi.fn() }))
import { toBlob } from 'html-to-image'
import mermaid from 'mermaid'
import * as mermaidFonts from '../components/mermaidFontCss'
import MarkdownRenderer from '../components/MarkdownRenderer'

const PNG = new Blob(['png bytes'], { type: 'image/png' })
const SOURCE = 'graph TD;A-->B'
const MARKDOWN = '```mermaid\n' + SOURCE + '\n```'
const svgFixture = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
svgFixture.setAttribute('viewBox', '0 0 240 120')
svgFixture.appendChild(document.createElementNS('http://www.w3.org/2000/svg', 'text')).textContent = 'Rendered label'
const SVG = svgFixture.outerHTML

async function openActions() {
  render(<MarkdownRenderer content={MARKDOWN} />)
  const toggle = await screen.findByTestId('mermaid-source-toggle')
  const more = screen.getByTestId('mermaid-more-actions')
  await userEvent.click(more)
  return { toggle, more, menu: await screen.findByRole('menu') }
}

describe('Mermaid downloads', () => {
  beforeEach(() => {
    vi.mocked(toBlob).mockReset().mockResolvedValue(PNG)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:diagram')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    vi.mocked(mermaid.render).mockReset().mockResolvedValue({ svg: SVG } as never)
  })

  afterEach(() => { vi.restoreAllMocks() })

  it('keeps Source first and More second, with three named menu actions', async () => {
    const { toggle, more, menu } = await openActions()
    expect(Array.from(toggle.parentElement!.querySelectorAll('button'))).toEqual([toggle, more])
    expect(more).toHaveAccessibleName('More actions')
    expect(within(menu).getAllByRole('menuitem').map(item => item.textContent)).toEqual([
      'Enlarge diagram', 'Download SVG', 'Download PNG',
    ])
  })

  it.each([
    ['', 'mermaid-diagram'],
    ['Booking / Payment: final?', 'Booking-Payment-final'],
    ['预订 流程', '预订-流程'],
    ['.. / ..', 'mermaid-diagram'],
    ['CON', 'mermaid-diagram'],
  ])('downloads the exact SVG and PNG with a safe title basename (%s)', async (title, basename) => {
    const rendered = svgFixture.cloneNode(true) as SVGSVGElement
    if (title) rendered.appendChild(document.createElementNS('http://www.w3.org/2000/svg', 'title')).textContent = title
    const renderedSvg = rendered.outerHTML
    vi.mocked(mermaid.render).mockResolvedValue({ svg: renderedSvg } as never)
    const { more, menu } = await openActions()
    await userEvent.click(within(menu).getByRole('menuitem', { name: 'Download SVG' }))
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    const blob = vi.mocked(URL.createObjectURL).mock.calls[0][0] as Blob
    expect(blob.type).toBe('image/svg+xml;charset=utf-8')
    expect(await blob.text()).toBe(renderedSvg)
    const anchor = vi.mocked(HTMLAnchorElement.prototype.click).mock.instances[0]
    expect(anchor.download).toBe(`${basename}.svg`)
    await userEvent.click(more)
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Download PNG' }))
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(2))
    expect(vi.mocked(HTMLAnchorElement.prototype.click).mock.instances[1].download).toBe(`${basename}.png`)
  })

  it('rasterizes the diagram snapshot at 2x only on demand and downloads PNG', async () => {
    let finish!: (blob: Blob) => void
    vi.mocked(toBlob).mockReturnValue(new Promise(resolve => { finish = resolve }))
    const { toggle, more, menu } = await openActions()
    expect(toBlob).not.toHaveBeenCalled()
    await userEvent.click(within(menu).getByRole('menuitem', { name: 'Download PNG' }))
    await waitFor(() => expect(toBlob).toHaveBeenCalled())
    expect(more).toHaveAttribute('aria-busy', 'true')
    expect(more).toHaveAttribute('aria-disabled', 'true')
    await waitFor(() => expect(more).toHaveFocus())
    await userEvent.click(more)
    await userEvent.keyboard('{Enter} {ArrowDown}')
    expect(screen.queryByRole('menu')).toBeNull()
    expect(toBlob).toHaveBeenCalledTimes(1)
    expect(toggle.parentElement!.querySelectorAll('button')).toHaveLength(2)
    await act(async () => { finish(PNG) })
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledTimes(1))
    expect(more).toHaveAttribute('aria-busy', 'false')
    expect(more).toBeEnabled()
    expect(toBlob).toHaveBeenCalledWith(expect.any(HTMLElement), expect.objectContaining({ pixelRatio: 2 }))
    expect(vi.mocked(URL.createObjectURL).mock.calls[0][0]).toBe(PNG)
    expect(vi.mocked(HTMLAnchorElement.prototype.click).mock.instances[0].download).toBe('mermaid-diagram.png')
  })

  it('keeps the exported snapshot unchanged when the diagram rerenders during PNG generation', async () => {
    let finishFonts!: (css: string) => void
    vi.spyOn(mermaidFonts, 'mermaidFontCss').mockReturnValue(new Promise(resolve => { finishFonts = resolve }))
    const { rerender } = render(<MarkdownRenderer content={MARKDOWN} />)
    await userEvent.click(await screen.findByTestId('mermaid-more-actions'))
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Download PNG' }))
    await waitFor(() => expect(mermaidFonts.mermaidFontCss).toHaveBeenCalled())
    const snapshot = vi.mocked(mermaidFonts.mermaidFontCss).mock.calls[0][0]
    expect(snapshot.isConnected).toBe(true)

    vi.mocked(mermaid.render).mockResolvedValue({ svg: SVG.replace('Rendered label', 'Updated label') } as never)
    rerender(<MarkdownRenderer content={'```mermaid\ngraph TD;C-->D\n```'} />)
    await waitFor(() => expect(document.querySelector('figure > div')).toHaveTextContent('Updated label'))
    expect(snapshot).toHaveTextContent('Rendered label')
    expect(snapshot).not.toHaveTextContent('Updated label')

    await act(async () => { finishFonts('') })
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledTimes(1))
    expect(toBlob).toHaveBeenCalledWith(snapshot, expect.objectContaining({ pixelRatio: 2 }))
    expect(snapshot.isConnected).toBe(false)

    vi.mocked(mermaid.render).mockReturnValue(new Promise(() => {}))
    rerender(<MarkdownRenderer content={'```mermaid\ngraph TD;E-->F\n```'} />)
    await userEvent.click(screen.getByTestId('mermaid-more-actions'))
    expect(await screen.findByRole('menuitem', { name: 'Download SVG' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('menuitem', { name: 'Download PNG' })).toHaveAttribute('aria-disabled', 'true')
  })

  it('reports a rejected rasterization and clears the notice only after a successful retry', async () => {
    vi.mocked(toBlob).mockRejectedValue(new Error('canvas refused'))
    const { toggle, more, menu } = await openActions()
    await userEvent.click(within(menu).getByRole('menuitem', { name: 'Download PNG' }))
    const notice = await screen.findByTestId('mermaid-download-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(toggle.parentElement!.contains(notice)).toBe(false)
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    vi.mocked(toBlob).mockResolvedValue(PNG)
    await userEvent.click(more)
    expect(notice).toBeVisible()
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Download PNG' }))
    await waitFor(() => expect(screen.queryByTestId('mermaid-download-error')).toBeNull())
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
  })

  it('offers no export actions after Mermaid fails, retaining source recovery', async () => {
    vi.mocked(mermaid.render).mockRejectedValue(new Error('parse error'))
    render(<MarkdownRenderer content={MARKDOWN} />)
    await screen.findByTestId('mermaid-render-error')
    expect(screen.queryByTestId('mermaid-more-actions')).toBeNull()
    expect(screen.queryByTestId('mermaid-download-svg')).toBeNull()
    expect(screen.queryByTestId('mermaid-download-png')).toBeNull()
    expect(screen.getByTestId('mermaid-copy-source')).toBeVisible()
    expect(toBlob).not.toHaveBeenCalled()
  })
})
