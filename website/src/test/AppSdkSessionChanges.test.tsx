/**
 * The App SDK's `SessionChanges` reaches only what the hosting app declares,
 * and its `DiffView` needs nothing.
 *
 * An external app's list and diff are backed by its scoped transport. The
 * list's read and the diff's read are sibling paths, so each is declared on its
 * own: an undeclared one is a refusal with no request, and declaring one never
 * admits the other. A builtin page keeps the dashboard's client, and a surface
 * with no app identity fails closed.
 *
 * The list and diff sit behind the SDK wrapper's lazy `import()`, so the first
 * thing they draw waits on that chunk (LAZY).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const mockApi = vi.hoisted(() => ({
  sessionFiles: vi.fn(),
  sessionFileDiff: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
// The diff renderer draws each side's text; its own highlighting is not under test.
vi.mock('../pierre', () => ({
  PierreFilePair: ({ oldFile, newFile }: { oldFile: { contents: string } | null; newFile: { contents: string } | null }) => (
    <div data-testid="pierre-pair">
      <span data-testid="pierre-old">{oldFile?.contents ?? ''}</span>
      <span data-testid="pierre-new">{newFile?.contents ?? ''}</span>
    </div>
  ),
}))

import { AppApiProvider, DiffView, SessionChanges, type AppApi } from '../app-sdk/index'
import { AppScopedApiProvider, apiPathAllowed } from '../app-sdk/scopedApi'
import { AppIdentityProvider } from '../app-sdk/identity'
import { SESSION_CHANGES_PATHS, scopedSessionFilesClient } from '../app-sdk/SessionChanges'

const SLOT = 'dashboard:chat-7-1787502679'
const DIR = '/home/u/proj'
const FILE = `${DIR}/src/app.ts`

// The wait for the lazy chunk to load and commit (docs/testing.md).
const LAZY = { timeout: 5000 }

const listing = {
  files: [{ path: FILE, ts: '2026-11-20T12:00:00Z', exists: true }],
  truncated: false,
}
const pair = { path: FILE, available: true, before: 'const a = 1\n', after: 'const a = 2\n' }

let fetchMock: ReturnType<typeof vi.fn>
let diffBody: unknown
let appSeq = 0

function pathOf(call: unknown[]): string {
  return new URL(String(call[0]), 'http://localhost').pathname
}

beforeEach(() => {
  for (const fn of Object.values(mockApi)) fn.mockReset()
  mockApi.sessionFiles.mockResolvedValue(listing)
  mockApi.sessionFileDiff.mockResolvedValue(pair)
  diffBody = pair
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname
    const body = path === SESSION_CHANGES_PATHS.sessionFiles ? listing
      : path === SESSION_CHANGES_PATHS.sessionFileDiff ? diffBody
        : {}
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function externalApp(allowedApiPaths: string[], children: ReactNode) {
  // One app id per mount: an external app's query client is held per id.
  appSeq += 1
  return (
    <AppApiProvider appName={`changes-app-${appSeq}`} allowedApiPaths={allowedApiPaths} navigateFn={() => {}}>
      {children}
    </AppApiProvider>
  )
}

const changes = <SessionChanges slotKey={SLOT} projectDir={DIR} />

async function showChanges() {
  const row = await screen.findByTestId('session-file-row', undefined, LAZY)
  fireEvent.click(within(row).getByTestId('session-file-diff'))
}

describe('scopedSessionFilesClient', () => {
  it('sends the queries the dashboard client sends', async () => {
    const api = { get: vi.fn(async () => ({})) } as unknown as AppApi & { get: ReturnType<typeof vi.fn> }
    const c = scopedSessionFilesClient(api)
    await c.sessionFiles(SLOT)
    await c.sessionFileDiff(SLOT, '/a b/c&d.ts')
    expect(api.get.mock.calls).toEqual([
      [`/api/project/session-files?slot=${encodeURIComponent(SLOT)}`],
      [`/api/project/session-file-diff?slot=${encodeURIComponent(SLOT)}&path=${encodeURIComponent('/a b/c&d.ts')}`],
    ])
  })

  it('needs each read declared on its own: neither path covers the other', () => {
    const { sessionFiles, sessionFileDiff } = SESSION_CHANGES_PATHS
    expect(apiPathAllowed([sessionFiles], sessionFileDiff)).toBe(false)
    expect(apiPathAllowed([sessionFileDiff], sessionFiles)).toBe(false)
    expect(apiPathAllowed(['/api/project/session-file'], sessionFileDiff)).toBe(false)
    expect(apiPathAllowed(['/api/project/*'], sessionFiles)).toBe(true)
    expect(apiPathAllowed(['/api/project/*'], sessionFileDiff)).toBe(true)
  })
})

describe('the App SDK SessionChanges', () => {
  it('refuses an external app that does not declare the list, and sends nothing', async () => {
    // The diff's path, or a neighbour, is not the list's read.
    render(externalApp(['/api/apps/changes-app', SESSION_CHANGES_PATHS.sessionFileDiff, '/api/project/tree'], changes))
    const listRefusal = await screen.findByTestId('app-session-changes-not-declared')
    expect(listRefusal).toHaveTextContent(SESSION_CHANGES_PATHS.sessionFiles)
    // The reason comes before the path, and it ends on who can fix it.
    expect(listRefusal).toHaveTextContent("can't show this chat's changes. This app's developer hasn't allowed it")
    expect(listRefusal).toHaveTextContent("Only the app's developer can change this.")
    // The hand-off says what the agent does, not a second person to fix it.
    expect(within(listRefusal).getByRole('button', { name: 'Ask the agent to explain this' })).toBeInTheDocument()
    expect(screen.queryByTestId('session-file-row')).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.sessionFiles).not.toHaveBeenCalled()
  })

  it('fails closed with no app identity and no SDK provider', async () => {
    const client = new QueryClient()
    render(<QueryClientProvider client={client}>{changes}</QueryClientProvider>)
    expect(await screen.findByTestId('app-session-changes-not-declared')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.sessionFiles).not.toHaveBeenCalled()
  })

  it('fails closed with no app identity even under a scoped provider that declares both reads', async () => {
    const client = new QueryClient()
    render(
      <QueryClientProvider client={client}>
        <AppScopedApiProvider appName="scoped-no-identity" allowedApiPaths={[SESSION_CHANGES_PATHS.sessionFiles, SESSION_CHANGES_PATHS.sessionFileDiff]} navigateFn={() => {}}>
          {changes}
        </AppScopedApiProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByTestId('app-session-changes-not-declared')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.sessionFiles).not.toHaveBeenCalled()
  })

  it('shows the list with only its read declared, and refuses the diff without asking for it', async () => {
    render(externalApp([SESSION_CHANGES_PATHS.sessionFiles], changes))
    await showChanges()
    const diffRefusal = await screen.findByTestId('app-session-changes-diff-not-declared')
    expect(diffRefusal).toHaveTextContent(SESSION_CHANGES_PATHS.sessionFileDiff)
    // Said as the one file's changes, the row's "Show changes": the list of
    // changes beside it does show. The reason comes before the path, and it
    // ends on who can fix it.
    expect(diffRefusal).toHaveTextContent("can't show this file's changes. This app's developer hasn't allowed it")
    expect(diffRefusal).toHaveTextContent("Only the app's developer can change this.")
    expect(within(diffRefusal).getByRole('button', { name: 'Ask the agent to explain this' })).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-pair')).toBeNull()
    const paths = fetchMock.mock.calls.map(pathOf)
    expect(paths).toContain(SESSION_CHANGES_PATHS.sessionFiles)
    expect(paths).not.toContain(SESSION_CHANGES_PATHS.sessionFileDiff)
    expect(Object.values(mockApi).every(fn => fn.mock.calls.length === 0)).toBe(true)
  })

  it('shows the picked file\'s recorded change once both reads are declared, through the app transport', async () => {
    render(externalApp([SESSION_CHANGES_PATHS.sessionFiles, SESSION_CHANGES_PATHS.sessionFileDiff], changes))
    expect(await screen.findByTestId('app-session-changes-none', undefined, LAZY)).toBeInTheDocument()
    await showChanges()
    expect(await screen.findByTestId('pierre-old', undefined, LAZY)).toHaveTextContent('const a = 1')
    expect(screen.getByTestId('pierre-new')).toHaveTextContent('const a = 2')
    const diffCall = fetchMock.mock.calls.find(call => pathOf(call) === SESSION_CHANGES_PATHS.sessionFileDiff)!
    const query = new URL(String(diffCall[0]), 'http://localhost').searchParams
    expect([query.get('slot'), query.get('path')]).toEqual([SLOT, FILE])
    expect(Object.values(mockApi).every(fn => fn.mock.calls.length === 0)).toBe(true)
  })

  it('says so when the record holds no whole pair', async () => {
    diffBody = { path: FILE, available: false, before: '', after: '' }
    render(externalApp([SESSION_CHANGES_PATHS.sessionFiles, SESSION_CHANGES_PATHS.sessionFileDiff], changes))
    await showChanges()
    expect(await screen.findByTestId('app-session-changes-diff-unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-pair')).toBeNull()
  })

  it('keeps the dashboard client for a builtin page, as the chat window has it', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <AppIdentityProvider appId="code-review-sage" origin="builtin">{changes}</AppIdentityProvider>
      </QueryClientProvider>,
    )
    await showChanges()
    expect(await screen.findByTestId('pierre-new', undefined, LAZY)).toHaveTextContent('const a = 2')
    expect(mockApi.sessionFiles).toHaveBeenCalledWith(SLOT)
    expect(mockApi.sessionFileDiff).toHaveBeenCalledWith(SLOT, FILE)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('the App SDK DiffView', () => {
  it('renders both texts with no app identity and sends nothing', async () => {
    render(<DiffView filePath="src/app.ts" original="old text" modified="new text" />)
    expect(await screen.findByTestId('pierre-old', undefined, LAZY)).toHaveTextContent('old text')
    expect(screen.getByTestId('pierre-new')).toHaveTextContent('new text')
    expect(screen.getByText('src/app.ts')).toBeInTheDocument()
    await waitFor(() => expect(fetchMock).not.toHaveBeenCalled())
    expect(mockApi.sessionFileDiff).not.toHaveBeenCalled()
  })
})
