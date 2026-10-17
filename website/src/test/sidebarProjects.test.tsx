import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, renderHook, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import FolderConfigModal from '../components/FolderConfigModal'
import { useProjectAgentLocked } from '../hooks/useProjectAgentLocked'
import { ChatFolder } from '../types'
import { PROJECT_AGENT, projectNestingRefused, projectRootId, resolveFolderAgent } from '../utils/folderAgent'
import { conductorTreeRows, rowLineage, topThreadFirst } from '../pages/chat-sidebar/conductorTree'
import { sessionRowIdentity } from '../pages/chat-sidebar/rowIdentity'
import type { Slot } from '../pages/chat-sidebar/types'

vi.mock('../api/client', () => ({
  api: {
    recentProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    favoriteProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/', parent: '', dirs: [] }),
  },
}))

const folder = (id: string, extra: Partial<ChatFolder> = {}): ChatFolder =>
  ({ id, name: id, order: 0, ...extra }) as ChatFolder

const TREE: ChatFolder[] = [
  folder('p', { name: 'Ship', kind: 'project', default_agent: 'kirocrew-dev' }),
  folder('p-sub', { parent_id: 'p' }),
  folder('plain', { default_agent: 'kirocrew-dev' }),
  // A damaged cycle must end the walk rather than spin.
  folder('x', { parent_id: 'y' }),
  folder('y', { parent_id: 'x' }),
]

describe('project folder resolution', () => {
  it('finds the project a folder sits in, through ancestors', () => {
    expect(projectRootId(TREE, 'p')).toBe('p')
    expect(projectRootId(TREE, 'p-sub')).toBe('p')
    expect(projectRootId(TREE, 'plain')).toBeUndefined()
    expect(projectRootId(TREE, 'x')).toBeUndefined()
    expect(projectRootId(TREE, undefined)).toBeUndefined()
  })

  it('names the conductor for a new chat anywhere in a project, ignoring default_agent', () => {
    expect(resolveFolderAgent(TREE, 'p', 'kirocrew')).toBe(PROJECT_AGENT)
    expect(resolveFolderAgent(TREE, 'p-sub', 'kirocrew')).toBe(PROJECT_AGENT)
    expect(resolveFolderAgent(TREE, 'plain', 'kirocrew')).toBe('kirocrew-dev')
  })
})

const slot = (key: string, parentKey?: string, extra: Partial<Slot> = {}): Slot =>
  ({ key, title: key, parent: parentKey ? { slot: parentKey, key: parentKey } : null, ...extra }) as unknown as Slot

/** A Project's body exactly as folderBlocks builds it, with every conductor open
 *  unless `shut` names it. */
function projectTree(rows: Slot[], shut: string[] = []) {
  const byKey = new Map(rows.map(s => [sessionRowIdentity(s), s] as const))
  const tree = rowLineage(rows)
  const ids = new Set(byKey.keys())
  const open = new Set([...ids].filter(id => !shut.includes(id)))
  return conductorTreeRows({
    tree, byKey, kept: ids, matching: ids, expanded: open, citedCreatorExists: new Map(),
    wantsUser: () => false, isRunning: () => false, roots: topThreadFirst(tree, byKey),
  })
}

describe("a Project's session tree (conductorTree)", () => {
  it('puts the chat the person started first, even when a worker spoke last', () => {
    // Lane order is recency: the workers that spoke last sit first in the input.
    const rows = projectTree([
      slot('w2', 'lead'), slot('sub', 'lead'), slot('sw1', 'sub'), slot('w1', 'lead'),
      slot('lead', undefined, { created: '2026-10-17T10:00:00Z' }),
    ])
    expect(rows.map(r => [r.slot.key, r.depth])).toEqual([
      ['lead', 0],
      ['w2', 1], ['sub', 1], ['sw1', 2], ['w1', 1],
    ])
    // The top thread and the sub-conductor carry the chevron main's conductor view draws.
    expect(rows.find(r => r.slot.key === 'lead')?.childCount).toBe(3)
    expect(rows.find(r => r.slot.key === 'sub')?.childCount).toBe(1)
  })

  it('orders two started threads oldest first, ahead of an orphaned worker', () => {
    const roots = projectTree([
      slot('orphan', 'gone'),
      slot('second', undefined, { created: '2026-10-17T11:00:00Z' }),
      slot('first', undefined, { created: '2026-10-17T09:00:00Z' }),
    ]).map(r => r.slot.key)
    expect(roots).toEqual(['first', 'second', 'orphan'])
  })

  it('a shut conductor hides its workers and counts them on its own row', () => {
    const rows = projectTree([slot('lead'), slot('w', 'lead'), slot('ww', 'w')], ['lead'])
    expect(rows.map(r => r.slot.key)).toEqual(['lead'])
    expect(rows[0].expanded).toBe(false)
    expect(rows[0].aggregate).toEqual({ needsYou: 0, running: 0 })
  })

  it('never nests a peer row under a local row that shares its key', () => {
    const rows = projectTree([slot('c'), slot('w', 'c', { peer_id: 'peer-1', row_identity: 'peer-1:w' })])
    expect(rows.map(r => r.depth)).toEqual([0, 0])
  })
})

describe('projectNestingRefused', () => {
  const folders = [folder('proj', { kind: 'project' }), folder('plain')]
  it('keeps a Project at the top level and out of every folder', () => {
    expect(projectNestingRefused(folders, folders[0], '')).toBe(false)
    expect(projectNestingRefused(folders, folders[0], null)).toBe(false)
    expect(projectNestingRefused(folders, folders[0], 'plain')).toBe(true)
  })
  it('lets no folder into a Project, and leaves ordinary nesting alone', () => {
    expect(projectNestingRefused(folders, folders[1], 'proj')).toBe(true)
    expect(projectNestingRefused(folders, folder('other'), 'plain')).toBe(false)
  })
})

describe('useProjectAgentLocked', () => {
  const wrapper = (folders: ChatFolder[]) => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    qc.setQueryData(['chat-folders'], folders)
    return ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }

  it('locks the picker for a chat anywhere in a project, and nowhere else', () => {
    const w = wrapper(TREE)
    const locked = (id: string) => renderHook(() => useProjectAgentLocked(id), { wrapper: w }).result.current
    expect(locked('p')).toEqual({ locked: true, error: null })
    expect(locked('p-sub').locked).toBe(true)
    expect(locked('plain')).toEqual({ locked: false, error: null })
    expect(locked('').locked).toBe(false)
  })

  it('reports a failed folder read instead of reading it as "not a project"', async () => {
    const { api } = await import('../api/client')
    const fail = new Error('folders down')
    ;(api as unknown as { chatFolders: () => Promise<never> }).chatFolders = vi.fn().mockRejectedValue(fail)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const w = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    const { result } = renderHook(() => useProjectAgentLocked('p'), { wrapper: w })
    await waitFor(() => expect(result.current.error).toBe(fail))
    expect(result.current.locked).toBe(false)
    // A chat in no folder needs no folder list, so there is nothing to report.
    expect(renderHook(() => useProjectAgentLocked(''), { wrapper: w }).result.current.error).toBeNull()
  })
})

function open(props: Partial<React.ComponentProps<typeof FolderConfigModal>> = {}) {
  const onSubmit = vi.fn().mockResolvedValue(undefined)
  render(
    <FolderConfigModal
      open={true}
      mode="create"
      parentId=""
      folders={TREE}
      installedAgents={[{ name: 'kirocrew' }, { name: 'kirocrew-dev' }]}
      onClose={vi.fn()}
      onSubmit={onSubmit}
      onRetryTags={vi.fn()}
      availableTags={[]}
      {...props}
    />
  )
  return { onSubmit }
}

describe('FolderConfigModal for projects', () => {
  beforeEach(() => vi.clearAllMocks())

  it('New > Project asks only for a name and a directory, and names the agent it runs on', async () => {
    const { onSubmit } = open({ kind: 'project' })
    expect(screen.getByText('New project')).toBeInTheDocument()
    expect(screen.getByTestId('folder-config-project-dir')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Default agent' })).toBeNull()
    expect(screen.queryByTestId('folder-config-icon')).toBeNull()
    expect(screen.queryByTestId('folder-config-color-reset')).toBeNull()
    expect(screen.queryByTestId('folder-config-steering-add')).toBeNull()
    expect(screen.queryByTestId('folder-config-tags-empty')).toBeNull()
    expect(screen.getByTestId('folder-config-project-agent')).toHaveTextContent(PROJECT_AGENT)
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Ship it' } })
    fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/work/ship' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ name: 'Ship it', projectDir: '/work/ship' })
    expect(screen.getByTestId('folder-config-submit')).toHaveTextContent('Create project')
  })

  it('a folder row under a project (only a damaged store holds one) shows no agent picker', () => {
    open({ mode: 'edit', folder: TREE[1] })
    expect(screen.queryByRole('button', { name: 'Default agent' })).toBeNull()
    expect(screen.getByTestId('folder-config-project-agent')).toHaveTextContent(PROJECT_AGENT)
  })

  it('an ordinary folder keeps its agent picker and offers no project toggle', () => {
    open({ mode: 'edit', folder: TREE[2] })
    expect(screen.getByRole('button', { name: 'Default agent' })).toBeInTheDocument()
    expect(screen.queryByTestId('folder-config-project-agent')).toBeNull()
  })
})
