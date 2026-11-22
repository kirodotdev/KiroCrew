import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ArtifactsQuickCreate } from '../pages/ArtifactsPageQuickCreate'

function renderRow(overrides: Partial<Parameters<typeof ArtifactsQuickCreate>[0]> = {}) {
  const props = {
    onNewDoc: vi.fn(),
    newDocBusy: false,
    onNewCanvas: vi.fn(),
    newCanvasBusy: false,
    onImport: vi.fn(),
    importBusy: false,
    onNewFolder: vi.fn(),
    ...overrides,
  }
  render(<ArtifactsQuickCreate {...props} />)
  return props
}

describe('ArtifactsQuickCreate canvas tile', () => {
  it('sits beside the Doc tile and starts a canvas', async () => {
    const user = userEvent.setup()
    const props = renderRow()
    const row = screen.getByRole('group', { name: 'Create new' })
    const tile = within(row).getByTestId('artifacts-quick-new-canvas')
    expect(tile).toHaveTextContent('Canvas')
    await user.click(tile)
    expect(props.onNewCanvas).toHaveBeenCalledTimes(1)
    expect(props.onNewDoc).not.toHaveBeenCalled()
  })

  it('cannot start a second canvas while one is being created', async () => {
    const user = userEvent.setup()
    const props = renderRow({ newCanvasBusy: true })
    const tile = screen.getByTestId('artifacts-quick-new-canvas')
    await user.click(tile)
    expect(props.onNewCanvas).not.toHaveBeenCalled()
  })

  it('leaves New canvas out of the More menu', async () => {
    const user = userEvent.setup()
    renderRow()
    await user.click(screen.getByRole('button', { name: 'More ways to add an artifact' }))
    const menu = await screen.findByRole('menu')
    expect(within(menu).queryByText('New canvas')).toBeNull()
    expect(within(menu).getByRole('menuitem', { name: /new folder/i })).toBeTruthy()
    expect(within(menu).getByRole('menuitem', { name: /import/i })).toBeTruthy()
  })
})
