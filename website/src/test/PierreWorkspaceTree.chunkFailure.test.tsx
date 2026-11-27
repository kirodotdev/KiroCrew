/**
 * A failed chunk load of the workspace tree stays in the tree's place.
 *
 * The tree in the chat's Files tab and in a folder tab is a lazy chunk. When
 * that chunk fails to load, the failure must show where the tree would be and
 * never reach the chat route's boundary, which would replace the whole chat
 * page. It offers no agent hand-off: the chat composer and the open files
 * beside the tree may hold unsaved drafts. Try Again must run the import again,
 * because React.lazy caches a rejected import.
 */
import '../i18n/all'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import ErrorBoundary from '../components/ErrorBoundary'
import { PierreWorkspaceTree } from '../pierre/tree'
import { i18nT } from '../i18n/t'

const chunk = vi.hoisted(() => ({ fail: true, imports: 0 }))

// The tree's module as its chunk: an import that rejects the way a browser
// rejects a module script it could not fetch, until the test restores it.
vi.mock('../pierre/PierreWorkspaceTreeImpl', () => {
  chunk.imports += 1
  if (chunk.fail) throw new TypeError('Failed to fetch dynamically imported module: /assets/PierreWorkspaceTreeImpl.js')
  return {
    PierreWorkspaceTreeImpl: ({ projectDir }: { projectDir: string }) => <div data-testid="workspace-tree-loaded">{projectDir}</div>,
  }
})

/** A named ceiling for the lazy import, its rejection and the boundary's commit on a loaded worker. */
const CHUNK_SETTLE_TIMEOUT_MS = 5000

afterEach(() => { vi.restoreAllMocks() })

describe('PierreWorkspaceTree when its chunk fails to load', () => {
  it('shows a notice where the tree would be, with no hand-off, and Try Again imports the chunk again', async () => {
    // React logs the caught render error; the boundary handling it is the point.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary scope="test-chat-route" fallback={<div data-testid="chat-route-boundary-fallback" />}>
        <div data-testid="files-tab">
          <PierreWorkspaceTree projectDir="/repo/project" />
        </div>
      </ErrorBoundary>,
    )
    const tryAgain = await screen.findByRole(
      'button', { name: i18nT('components.errorBoundary.try_again') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    )
    expect(chunk.imports).toBe(1)
    expect(screen.getByTestId('files-tab')).toHaveTextContent(i18nT('pages.chat.activityViewer.workspace_load_failed'))
    expect(screen.queryByTestId('chat-route-boundary-fallback'), 'the failure stays in the tree').toBeNull()
    expect(screen.queryByRole('status', { name: i18nT('pages.chat.activityViewer.loading_workspace') })).toBeNull()
    expect(
      screen.queryByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }),
      'no hand-off beside unsaved drafts',
    ).toBeNull()

    chunk.fail = false
    fireEvent.click(tryAgain)
    expect(
      await screen.findByTestId('workspace-tree-loaded', {}, { timeout: CHUNK_SETTLE_TIMEOUT_MS }),
    ).toHaveTextContent('/repo/project')
    expect(chunk.imports, 'Try Again runs the import again').toBe(2)
  })
})
