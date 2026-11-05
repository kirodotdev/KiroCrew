/**
 * Library tree folder rows open and close from the keyboard.
 *
 * The header row is `tabIndex={-1}` (a focus target for the rename editor's
 * close, not a tab stop) and its only tab stop was the ⋯ menu, which has no
 * expand item. So a keyboard user could reach a folder but never see inside it.
 * The fix is a real disclosure button around the chevron and name, carrying
 * `aria-expanded`, beside the menu button rather than around it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createConnectedTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue({ ok: true }),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import type React from 'react'
import { DndContext } from '@dnd-kit/core'
import { useDndSensors } from '../hooks/useDndSensors'
import { FolderRow } from '../components/library/LibraryTable'
import type { ArtifactFolder, FolderActions } from '../types'

const FOLDER: ArtifactFolder = {
  id: 'f1', name: 'Alpha', parent_id: '', color: '', icon: '', collapsed: false,
}

function actions(renamingId: string | null = null): FolderActions {
  return {
    onOpen: vi.fn(), onRename: vi.fn(), onMove: vi.fn(), onDelete: vi.fn(), onSetColor: vi.fn(),
    renamingId, onRenameSubmit: vi.fn(), onRenameCancel: vi.fn(),
  }
}

/** The Artifacts page's own sensor set. A bare `DndContext` mounts dnd-kit's
 *  default KeyboardSensor, which the page does not, and that sensor would take
 *  Enter and Space on the row as a drag pick-up. */
function PageDnd({ children }: { children: React.ReactNode }) {
  return <DndContext sensors={useDndSensors({ distance: 6 })}>{children}</DndContext>
}

function renderRow({ expanded = false, renamingId = null as string | null } = {}) {
  const onToggle = vi.fn()
  const view = render(
    <Provider store={createConnectedTestStore()}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <PageDnd>
          <table>
            <tbody>
              <FolderRow folder={FOLDER} folders={[FOLDER]} depth={0} expanded={expanded} onToggle={onToggle} actions={actions(renamingId)} />
            </tbody>
          </table>
        </PageDnd>
      </QueryClientProvider>
    </Provider>,
  )
  return { onToggle, view }
}

/** Elements a Tab press can land on, in the row. */
const tabStops = (root: HTMLElement) =>
  Array.from(root.querySelectorAll<HTMLElement>('button, [href], input, [tabindex]'))
    .filter(el => el.tabIndex >= 0 && !el.hasAttribute('disabled'))

beforeEach(() => vi.clearAllMocks())

describe('library FolderRow keyboard disclosure', () => {
  it('has a tab-reachable toggle named by the folder, with its state', () => {
    const { view } = renderRow()
    const toggle = screen.getByRole('button', { name: 'Alpha' })
    expect(tabStops(view.container)).toContain(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
  })

  it('reports the expanded state on the toggle', () => {
    renderRow({ expanded: true })
    expect(screen.getByRole('button', { name: 'Alpha' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('Tab reaches the toggle before the ⋯ menu, and Enter and Space toggle it', async () => {
    const user = userEvent.setup()
    const { onToggle } = renderRow()
    await user.tab()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Alpha' }))
    await user.keyboard('{Enter}')
    expect(onToggle).toHaveBeenCalledTimes(1)
    await user.keyboard(' ')
    expect(onToggle).toHaveBeenCalledTimes(2)
    await user.tab()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /Actions for folder Alpha/ }))
  })

  it('one click on the toggle opens the folder exactly once', () => {
    const { onToggle } = renderRow()
    // One toggle, not two: the row's own click handler must not fire it again.
    fireEvent.click(screen.getByRole('button', { name: 'Alpha' }))
    expect(onToggle).toHaveBeenCalledTimes(1)
    expect(onToggle).toHaveBeenCalledWith('f1')
  })

  it('a click elsewhere on the row still toggles it', () => {
    const { onToggle, view } = renderRow()
    fireEvent.click(view.container.querySelector('tr')!)
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('nests no button inside another, and the ⋯ menu does not toggle', () => {
    const { onToggle, view } = renderRow()
    expect(view.container.querySelector('button button')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Actions for folder Alpha/ }))
    expect(onToggle).not.toHaveBeenCalled()
  })

  it('while renaming, the field is not inside a button', () => {
    const { view } = renderRow({ renamingId: 'f1' })
    const input = screen.getByRole('textbox')
    expect(input.closest('button')).toBeNull()
    expect(view.container.querySelector('button button')).toBeNull()
  })
})
