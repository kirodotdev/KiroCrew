import { vi, expect } from 'vitest'
import { render as rtlRender, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import FolderConfigModal from '../components/FolderConfigModal'
import { ChatFolder } from '../types'

// Shared setup extracted from FolderConfigModal.test.tsx so every sibling test
// file (split to stay under the 2,000-line file cap) gets identical render
// wrapping, mock shape, and fixture/helper functions. Each test FILE must still
// declare its own `vi.mock('../api/client', ...)` call — vi.mock is hoisted per
// file, so the factory here is imported into that per-file mock, not shared
// across files via a single hoisted call.

// The modal now fetches the project-scoped roster via react-query (useQuery),
// so every render needs a QueryClient in context. Wrap RTL's render so the
// existing call sites (and their `rerender`) get a provider transparently; a
// fresh client per render keeps tests isolated, and retries are off so a
// rejected queryFn surfaces immediately instead of being retried.
export function render(ui: React.ReactElement, options?: Parameters<typeof rtlRender>[1]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  // `wrapper` is re-applied by RTL's own rerender, so a plain rerender(next)
  // keeps the provider without double-wrapping (double-wrapping remounts the
  // subtree and re-runs mount effects — e.g. the tag seed).
  return rtlRender(ui, { wrapper: Wrapper, ...options })
}

// The default mock shape for '../api/client'. Each test file passes this (or
// an equivalent factory body) to its own file-level `vi.mock` call.
export function apiClientMock() {
  return {
    api: {
      // ProjectPicker fetches these on open; the modal itself never calls them.
      recentProjects: vi.fn().mockResolvedValue({ dirs: [] }),
      favoriteProjects: vi.fn().mockResolvedValue({ dirs: [] }),
      browseDirs: vi.fn().mockResolvedValue({ path: '/', parent: '', dirs: [] }),
      // Fetched by the modal ONLY when a project directory is set, to scope the
      // agent dropdown to that directory's project agents. Default: empty roster
      // (the no-dir tests never reach this call).
      kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    },
  }
}
// NOTE: this helper cannot be imported into a `vi.mock('../api/client', () => ...)`
// factory in a sibling test file — vi.mock factories are hoisted above all
// imports, so a reference to an imported function throws "Cannot access
// '<binding>' before initialization". Each test file inlines an equivalent
// factory body directly in its own `vi.mock` call instead; this export exists
// only for documentation/reference of that shared shape.

export const folder = (id: string, extra: Partial<ChatFolder> = {}): ChatFolder =>
  ({ id, name: id, order: 0, ...extra }) as ChatFolder

export const AGENTS = [{ name: 'kirocrew' }, { name: 'kirocrew-dev' }]

export function open(props: Partial<React.ComponentProps<typeof FolderConfigModal>> = {}) {
  const onSubmit = vi.fn().mockResolvedValue(undefined)
  const onClose = vi.fn()
  const utils = render(
    <FolderConfigModal
      open={true}
      mode="create"
      parentId=""
      folders={[]}
      installedAgents={AGENTS}
      onClose={onClose}
      onSubmit={onSubmit}
      onRetryTags={vi.fn()}
      {...props}
    />
  )
  return { onSubmit, onClose, ...utils }
}

/* The agent picker is a SearchableSelect (Radix Popover), not a native
 * <select>, so it is addressed by its accessible name — the `aria-label` that
 * carries the "Default agent" heading's key, since a <button> cannot be reached
 * by an external <label htmlFor>. Its options live in a portal that only exists
 * while the popup is open, so any assertion about them has to open it first.
 *
 * `Modal` is hand-rolled (createPortal + framer-motion), so there is no Radix
 * layer above the picker and the real component is driven directly. Keep it
 * that way — a stub here would stop testing the shipped dropdown. */
export function agentTrigger() {
  return screen.getByRole('button', { name: 'Default agent' })
}

/** Open the agent popup and return its filter box. The modal focuses its name
 *  field one animation frame after opening, and that focus move dismisses a
 *  popup already open, so wait it out first: a user cannot click inside it. */
export async function openAgentPopup(): Promise<HTMLElement> {
  // Queued after the modal's own rAF, so this resolves once that focus has run.
  await new Promise(resolve => requestAnimationFrame(resolve))
  fireEvent.click(agentTrigger())
  return screen.findByPlaceholderText('Type to filter…')
}

/** Open the agent popup and return its option labels in render order. */
export async function openAgents(): Promise<string[]> {
  await openAgentPopup()
  // findAllByRole, not findByRole: the latter throws on more than one match, and
  // every case here has at least the inherit/None row plus an agent.
  const opts = await screen.findAllByRole('option')
  return opts.map(o => o.textContent ?? '')
}

/** Pick an agent by its visible label, then wait for the popup to unmount. */
export async function pickAgent(label: string | RegExp) {
  await openAgentPopup()
  fireEvent.click(await screen.findByRole('option', { name: label }))
  await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
}
