/**
 * LibraryPanel: the styles and templates library list and detail pane.
 *
 * The panel must show the entries the server holds, split into the operator's own and the built-in
 * ones; only the operator's own entries can be renamed or deleted; pinning a style sends the new pinned
 * state; a rename is sent only for a changed, non-empty name; an import sends the picked file under the
 * name derived from its filename; a refused mutation shows its reason; and a template shows its
 * fonts, layout count and description. The pptx-maker API is mocked at its boundary. The preview frame
 * and the thumbnail are stubbed, since they only draw HTML.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { pptxMakerApi } from './api'
import LibraryPanel from './LibraryPanel'

vi.mock('./BoardFrame', () => ({
  default: () => null,
  BoardThumb: () => null,
}))

vi.mock('./api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('./api')>()
  return {
    ...mod,
    pptxMakerApi: {
      ...mod.pptxMakerApi,
      styles: vi.fn(),
      style: vi.fn(),
      importStyle: vi.fn(),
      renameStyle: vi.fn(),
      pinStyle: vi.fn(),
      deleteStyle: vi.fn(),
      templates: vi.fn(),
      importTemplate: vi.fn(),
      renameTemplate: vi.fn(),
      deleteTemplate: vi.fn(),
    },
  }
})

const api = vi.mocked(pptxMakerApi)
const ok = <T,>(value: T) => Promise.resolve(value)

const STYLES = [
  { name: 'my-deck', source: 'user', pinned: true, coverHtml: '' },
  { name: 'Minimal', source: 'builtin', pinned: false, coverHtml: '' },
]

beforeEach(() => {
  vi.resetAllMocks()
  api.styles.mockReturnValue(ok({ styles: STYLES }) as never)
  api.style.mockReturnValue(ok({ fullHtml: '<p>preview</p>' }) as never)
  api.templates.mockReturnValue(ok({ templates: [] }) as never)
})

describe('LibraryPanel styles list', () => {
  it('splits the operator styles from the built-in ones and marks a pinned style', async () => {
    renderWithProviders(<LibraryPanel kind="styles" />)
    expect(await screen.findByText('my-deck')).toBeTruthy()
    expect(screen.getByText('Minimal')).toBeTruthy()
    expect(screen.getByText(i18nT('apps.pptxMaker.libraryPanel.mine'))).toBeTruthy()
    expect(screen.getByText(i18nT('apps.pptxMaker.libraryPanel.built_in'))).toBeTruthy()
    expect(screen.getByText(i18nT('apps.pptxMaker.libraryPanel.pinned'))).toBeTruthy()
    const mine = screen.getByText(i18nT('apps.pptxMaker.libraryPanel.mine')).parentElement as HTMLElement
    expect(within(mine).getByText('my-deck')).toBeTruthy()
    expect(within(mine).queryByText('Minimal')).toBeNull()
    const builtIn = screen.getByText(i18nT('apps.pptxMaker.libraryPanel.built_in')).parentElement as HTMLElement
    expect(within(builtIn).getByText('Minimal')).toBeTruthy()
    expect(within(builtIn).queryByText('my-deck')).toBeNull()
  })

  it('shows the empty state when the library holds no styles', async () => {
    api.styles.mockReturnValue(ok({ styles: [] }) as never)
    renderWithProviders(<LibraryPanel kind="styles" />)
    expect(await screen.findByText(i18nT('apps.pptxMaker.libraryPanel.no_styles_yet'))).toBeTruthy()
  })

  it('loads the selected style and offers its chat reference token', async () => {
    renderWithProviders(<LibraryPanel kind="styles" />)
    await userEvent.click(await screen.findByText('Minimal'))
    expect(api.style).toHaveBeenCalledWith('Minimal')
    expect(await screen.findByText('[Style: Minimal]')).toBeTruthy()
  })

  it('offers no rename or delete on a built-in style', async () => {
    renderWithProviders(<LibraryPanel kind="styles" />)
    await userEvent.click(await screen.findByText('Minimal'))
    expect(screen.queryByRole('button', { name: i18nT('apps.pptxMaker.libraryPanel.rename') })).toBeNull()
    expect(screen.queryByRole('button', { name: i18nT('apps.pptxMaker.libraryPanel.delete') })).toBeNull()
  })
})

describe('LibraryPanel style actions', () => {
  it('unpins a pinned operator style with the new state', async () => {
    api.pinStyle.mockReturnValue(ok({}) as never)
    renderWithProviders(<LibraryPanel kind="styles" />)
    await userEvent.click(await screen.findByText('my-deck'))
    await userEvent.click(screen.getByRole('button', { name: i18nT('apps.pptxMaker.libraryPanel.unpin_style') }))
    await waitFor(() => expect(api.pinStyle).toHaveBeenCalledWith('my-deck', false))
  })

  it('renames an operator style to the typed name and selects the new name', async () => {
    api.renameStyle.mockReturnValue(ok({}) as never)
    renderWithProviders(<LibraryPanel kind="styles" />)
    await userEvent.click(await screen.findByText('my-deck'))
    await userEvent.click(screen.getByRole('button', { name: i18nT('apps.pptxMaker.libraryPanel.rename') }))
    const field = screen.getByRole('textbox', { name: i18nT('apps.pptxMaker.libraryPanel.new_name') })
    await userEvent.clear(field)
    api.styles.mockReturnValue(ok({ styles: [{ ...STYLES[0], name: 'final-deck' }] }) as never)
    await userEvent.type(field, 'final-deck{Enter}')
    await waitFor(() => expect(api.renameStyle).toHaveBeenCalledWith('my-deck', 'final-deck'))
    expect(await screen.findByText('[Style: final-deck]')).toBeTruthy()
  })

  it('closes the rename row without sending anything on Escape or an unchanged name', async () => {
    renderWithProviders(<LibraryPanel kind="styles" />)
    await userEvent.click(await screen.findByText('my-deck'))
    await userEvent.click(screen.getByRole('button', { name: i18nT('apps.pptxMaker.libraryPanel.rename') }))
    const field = screen.getByRole('textbox', { name: i18nT('apps.pptxMaker.libraryPanel.new_name') })
    await userEvent.type(field, '{Escape}')
    expect(screen.queryByRole('textbox', { name: i18nT('apps.pptxMaker.libraryPanel.new_name') })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: i18nT('apps.pptxMaker.libraryPanel.rename') }))
    await userEvent.type(screen.getByRole('textbox', { name: i18nT('apps.pptxMaker.libraryPanel.new_name') }), '{Enter}')
    expect(api.renameStyle).not.toHaveBeenCalled()
  })

  it('shows the server refusal when the delete fails', async () => {
    api.deleteStyle.mockRejectedValue(new Error('style is in use') as never)
    renderWithProviders(<LibraryPanel kind="styles" />)
    await userEvent.click(await screen.findByText('my-deck'))
    await userEvent.click(screen.getByRole('button', { name: i18nT('apps.pptxMaker.libraryPanel.delete') }))
    expect(api.deleteStyle).toHaveBeenCalledWith('my-deck')
    expect(await screen.findByText('style is in use')).toBeTruthy()
  })

  it('imports a picked HTML file under the name derived from its filename', async () => {
    api.importStyle.mockReturnValue(ok({}) as never)
    const { container } = renderWithProviders(<LibraryPanel kind="styles" />)
    await screen.findByText('my-deck')
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    expect(input.getAttribute('accept')).toBe('.html')
    const file = new File(['<html></html>'], 'Quarterly Review.html', { type: 'text/html' })
    fireEvent.change(input, { target: { files: [file] } })
    await waitFor(() => expect(api.importStyle).toHaveBeenCalledWith('Quarterly-Review', file))
  })
})

describe('LibraryPanel templates', () => {
  const TEMPLATES = [
    {
      name: 'Corporate',
      source: 'user',
      description: 'Board-room layouts',
      layout_count: 7,
      fonts: { halfwidth: 'Arial', fullwidth: 'Yu Gothic' },
      theme_colors: { background: '#fff', text: '#000', accent1: '#f00', accent2: '#0f0' },
    },
    { name: 'Plain', source: 'builtin', layout_count: 2, theme_colors: {} },
  ]

  it('shows a template with its fonts, layout count and description, and a reference token', async () => {
    api.templates.mockReturnValue(ok({ templates: TEMPLATES }) as never)
    renderWithProviders(<LibraryPanel kind="templates" />)
    await userEvent.click(await screen.findByText('Corporate'))
    expect(await screen.findByText('[Template: Corporate]')).toBeTruthy()
    expect(screen.getByText('Board-room layouts')).toBeTruthy()
    expect(screen.getByText(/Arial/)).toBeTruthy()
    expect(screen.getByText(i18nT('apps.pptxMaker.libraryPanel.layouts', { count: 7 }))).toBeTruthy()
  })

  it('shows the empty state for a library with no templates', async () => {
    renderWithProviders(<LibraryPanel kind="templates" />)
    expect(await screen.findByText(i18nT('apps.pptxMaker.libraryPanel.no_templates_yet'))).toBeTruthy()
    expect(api.styles).not.toHaveBeenCalled()
  })

  it('reports a template rename the server refuses', async () => {
    api.templates.mockReturnValue(ok({ templates: TEMPLATES }) as never)
    api.renameTemplate.mockRejectedValue(new Error('name taken') as never)
    renderWithProviders(<LibraryPanel kind="templates" />)
    await userEvent.click(await screen.findByText('Corporate'))
    await userEvent.click(screen.getByRole('button', { name: i18nT('apps.pptxMaker.libraryPanel.rename') }))
    const field = screen.getByRole('textbox', { name: i18nT('apps.pptxMaker.libraryPanel.new_name') })
    await userEvent.clear(field)
    await userEvent.type(field, 'Plain{Enter}')
    expect(await screen.findByText('name taken')).toBeTruthy()
    expect(api.renameTemplate).toHaveBeenCalledWith('Corporate', 'Plain')
  })
})
