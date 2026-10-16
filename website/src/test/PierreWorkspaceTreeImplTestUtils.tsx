/**
 * The fixtures PierreWorkspaceTreeImpl.test.tsx and its topic files share: the
 * project root and path set, the payload makers, and `renderTree` with its
 * locally-built QueryClientProvider wrapper. Each test file still declares its
 * own `vi.mock` block, because a factory applies only to the file it sits in.
 */
import { expect } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { PierreWorkspaceTreeImpl } from '../pierre/PierreWorkspaceTreeImpl'
import { STATE_ROW_MARKER } from '../pierre/treeStateRows'
import { api } from '../api/client'

export const ROOT = '/repo/project'
export const PATHS = ['README.md', 'src/a/b.ts']
// Every state row's synthetic segment ends in this (see `treeStateRows`).
export const M = STATE_ROW_MARKER

export type TreePayload = Awaited<ReturnType<typeof api.projectTree>>
type StatusPayload = Awaited<ReturnType<typeof api.projectGitStatus>>
type StatusFile = StatusPayload['files'][number]

export const mkTree = (over: Partial<TreePayload> = {}): TreePayload => ({
  root: ROOT,
  paths: PATHS,
  repo: true,
  ...over,
})

export const mkStatus = (files: StatusFile[], over: Partial<StatusPayload> = {}): StatusPayload => ({
  repo: true,
  repoRoot: '/repo',
  files,
  ...over,
})

export const mkFile = (path: string, status: string, staged = false): StatusFile => ({ path, status, staged })

type Props = Parameters<typeof PierreWorkspaceTreeImpl>[0]

export function renderTree(props: Partial<Props> = {}) {
  // structuralSharing off: with it on, a poll that returns deep-equal data
  // reuses the previous `data` object, so the wrapper's own same-paths guard
  // would never be exercised — react-query would be doing the work.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, structuralSharing: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const view = render(<PierreWorkspaceTreeImpl projectDir={ROOT} {...props} />, { wrapper })
  return {
    qc,
    ...view,
    update: (next: Partial<Props> = {}) =>
      view.rerender(<PierreWorkspaceTreeImpl projectDir={ROOT} {...props} {...next} />),
  }
}

/** Resolve once the first payload has been folded into the model. */
export const waitForTree = () => waitFor(() => expect(screen.getByTestId('file-tree')).toBeInTheDocument())
