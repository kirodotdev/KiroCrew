// The picker's caller footer (ComposerDockView's AWS profile row): keys typed in it belong to it.
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api } from '../api/client'

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height,
  bottom: top + height,
  right: left + width,
  x: left, y: top,
  toJSON: () => ({}),
} as DOMRect)

beforeEach(() => {
  vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/projA', '/home/u/projB'] })
  vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: [] })
})

afterEach(() => { vi.restoreAllMocks() })

describe('ProjectPicker footer', () => {
  it('Enter and Tab in a footer field do not pick a project or close the picker', async () => {
    const onSelect = vi.fn()
    const onOpenChange = vi.fn()
    const onSubmit = vi.fn((e: { preventDefault: () => void }) => e.preventDefault())
    renderWithProviders(
      <ProjectPicker
        open={true}
        onOpenChange={onOpenChange}
        anchorRect={rect(100, 50)}
        onSelect={onSelect}
        footer={<form onSubmit={onSubmit}><input aria-label="footer field" /></form>}
      />,
    )
    expect(await screen.findByRole('listbox', { name: 'Recent projects' })).toBeInTheDocument()
    const field = screen.getByRole('textbox', { name: 'footer field' })
    field.focus()
    fireEvent.focus(field)
    fireEvent.keyDown(field, { key: 'Enter' })
    fireEvent.keyDown(field, { key: 'Tab' })
    expect(onSelect).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('the list keys work again once focus leaves the footer', async () => {
    const onSelect = vi.fn()
    renderWithProviders(
      <ProjectPicker
        open={true}
        onOpenChange={vi.fn()}
        anchorRect={rect(100, 50)}
        onSelect={onSelect}
        footer={<input aria-label="footer field" />}
      />,
    )
    expect(await screen.findByRole('listbox', { name: 'Recent projects' })).toBeInTheDocument()
    const field = screen.getByRole('textbox', { name: 'footer field' })
    fireEvent.focus(field)
    fireEvent.blur(field, { relatedTarget: document.body })
    fireEvent.keyDown(document.body, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('/home/u/projA')
  })
})
