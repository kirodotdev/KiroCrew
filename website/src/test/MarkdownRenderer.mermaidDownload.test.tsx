import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'

vi.mock('mermaid', () => ({ default: { initialize: vi.fn(), render: vi.fn() } }))
import userEvent from '@testing-library/user-event'
vi.mock('html-to-image', () => ({ toBlob: vi.fn() }))
import { toBlob } from 'html-to-image'
import mermaid from 'mermaid'
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

  it('downloads only the exact rendered SVG with its MIME and filename', async () => {
    const { menu } = await openActions()
    await userEvent.click(within(menu).getByRole('menuitem', { name: 'Download SVG' }))
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    const blob = vi.mocked(URL.createObjectURL).mock.calls[0][0] as Blob
    expect(blob.type).toBe('image/svg+xml;charset=utf-8')
    expect(await blob.text()).toBe(SVG)
    const anchor = vi.mocked(HTMLAnchorElement.prototype.click).mock.instances[0]
    expect(anchor.download).toBe('mermaid-diagram.svg')
  })

  it('rasterizes the live diagram host at 2x only on demand and downloads PNG', async () => {
    const { menu } = await openActions()
    expect(toBlob).not.toHaveBeenCalled()
    const host = document.querySelector('figure > div')
    await userEvent.click(within(menu).getByRole('menuitem', { name: 'Download PNG' }))
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledTimes(1))
    expect(toBlob).toHaveBeenCalledWith(host, expect.objectContaining({ pixelRatio: 2 }))
    expect(vi.mocked(URL.createObjectURL).mock.calls[0][0]).toBe(PNG)
    expect(vi.mocked(HTMLAnchorElement.prototype.click).mock.instances[0].download).toBe('mermaid-diagram.png')
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
