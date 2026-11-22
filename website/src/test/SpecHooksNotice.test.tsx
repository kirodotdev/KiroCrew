import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import {
  defaultMessageRenderers,
  mergeRenderers,
  resolveRenderer,
  type MessageRenderContext,
  type MessageRenderer,
} from '../app-sdk/messageRenderers'
import { DURABLE_PREF_KEYS } from '../lib/uiPrefs'
import {
  PROJECT_HOOK_FILES_DISMISSED_KEY,
  SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY,
  isProjectHookFilesNotice,
  isSpecHooksRunNowhereNotice,
} from '../pages/chat/SpecHooksNotice'
import type { ChatMessage } from '../types'

const KEY = 'kirocrew:spec-hooks-run-nowhere-notice-dismissed'
const RUN_NOWHERE_TEXT =
  "\u2139\uFE0F Agent kirocrew has hooks on postToolUse. This backend does not run an agent's hooks, so they have no effect in this session."

function runNowhere(ts = '2026-11-06T00:00:00.000Z'): ChatMessage {
  return {
    role: 'notice',
    content: RUN_NOWHERE_TEXT,
    cls: 'msg msg-info',
    ts,
    meta: { kind: 'spec_hooks', spec_hooks_notice: 'run_nowhere' },
  }
}

/** Notices that must keep showing after a dismiss: the other rows of the same
 *  kind, and a notice with no kind at all. */
const OTHERS: ChatMessage[] = [
  {
    role: 'notice',
    content: "\u2139\uFE0F Agent kirocrew has hooks on preToolUse. This backend does not run an agent's preToolUse hooks, so they have no effect in this session. Its other hooks run.",
    ts: '2026-11-06T00:00:01.000Z',
    meta: { kind: 'spec_hooks' },
  },
  {
    role: 'notice',
    content: '\u2139 Project hooks for agent kirocrew do not run in this session: Crew does not run commands from a project checkout.',
    ts: '2026-11-06T00:00:02.000Z',
    meta: { kind: 'spec_hooks' },
  },
  {
    role: 'notice',
    content: '\u2139\uFE0F The model returned nothing twice.',
    ts: '2026-11-06T00:00:03.000Z',
  },
]

const ctx = { row: (node: React.ReactNode) => node, handOff: true } as unknown as MessageRenderContext

function draw(m: ChatMessage, renderers: readonly MessageRenderer[] = defaultMessageRenderers) {
  return resolveRenderer(m, renderers)!.render(m, ctx)
}

function plainDirty(): string[] {
  return JSON.parse(localStorage.getItem('mc-ui-prefs-plain-dirty') ?? '[]')
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('the hooks-have-no-effect notice', () => {
  it('offers a Dismiss link, and stores nothing until it is clicked', () => {
    render(<>{draw(runNowhere())}</>)
    expect(screen.getByTestId('notice-card-text').textContent).toContain('This backend does not run an agent\'s hooks')
    const link = screen.getByRole('button', { name: 'Dismiss' })
    expect(link.getAttribute('data-testid')).toBe('spec-hooks-notice-dismiss')
    expect(link.classList.contains('underline')).toBe(true)
    expect(localStorage.getItem(KEY)).toBeNull()
    expect(plainDirty()).not.toContain(KEY)
  })

  it('hides the row on click, as the user own durable choice, and stays hidden on a later session or reload', () => {
    const first = render(<>{draw(runNowhere())}</>)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('notice-card')).toBeNull()
    expect(localStorage.getItem(KEY)).toBe('1')
    // Marked as an explicit choice, so a host backup never overwrites it.
    expect(plainDirty()).toContain(KEY)
    first.unmount()
    // A new session's row, and a reload's fresh mount, draw nothing at all.
    expect(draw(runNowhere('2026-11-07T00:00:00.000Z'))).toBeNull()
    const again = render(<>{draw(runNowhere())}</>)
    expect(again.container.innerHTML).toBe('')
  })

  it('is backed up to the host with the other dismissed tips', () => {
    expect(SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY).toBe(KEY)
    expect(DURABLE_PREF_KEYS).toContain(KEY)
  })

  it('hides every copy on screen at once', () => {
    render(<>{draw(runNowhere('2026-11-06T00:00:00.000Z'))}{draw(runNowhere('2026-11-06T01:00:00.000Z'))}</>)
    expect(screen.getAllByTestId('spec-hooks-notice-dismiss')).toHaveLength(2)
    fireEvent.click(screen.getAllByTestId('spec-hooks-notice-dismiss')[0])
    expect(screen.queryAllByTestId('notice-card')).toHaveLength(0)
  })

  it('hides when another tab dismisses it', () => {
    render(<>{draw(runNowhere())}</>)
    act(() => {
      localStorage.setItem(KEY, '1')
      window.dispatchEvent(new StorageEvent('storage', { key: KEY, newValue: '1' }))
    })
    expect(screen.queryByTestId('notice-card')).toBeNull()
  })

  it('leaves every other notice, of its kind or not, showing and without a link', () => {
    localStorage.setItem(KEY, '1')
    for (const m of OTHERS) expect(isSpecHooksRunNowhereNotice(m)).toBe(false)
    render(<>{OTHERS.map(m => <div key={m.ts}>{draw(m)}</div>)}</>)
    expect(screen.getAllByTestId('notice-card')).toHaveLength(OTHERS.length)
    expect(screen.queryByTestId('spec-hooks-notice-dismiss')).toBeNull()
  })

  it('needs both tags: a different value, another role or a kind alone is not this notice', () => {
    expect(isSpecHooksRunNowhereNotice(runNowhere())).toBe(true)
    expect(isSpecHooksRunNowhereNotice({ ...runNowhere(), meta: { kind: 'spec_hooks', spec_hooks_notice: 'other' } })).toBe(false)
    expect(isSpecHooksRunNowhereNotice({ ...runNowhere(), meta: { spec_hooks_notice: 'run_nowhere' } })).toBe(false)
    expect(isSpecHooksRunNowhereNotice({ ...runNowhere(), role: 'assistant' })).toBe(false)
    // The words alone never match: a legacy row without the tag keeps its plain card.
    expect(isSpecHooksRunNowhereNotice({ ...runNowhere(), meta: { kind: 'spec_hooks' } })).toBe(false)
  })

  it('keeps its link when a host draws the notice role its own way', () => {
    const host: MessageRenderer = { id: 'host_notice', roles: ['notice'], render: () => <p data-testid="host-notice" /> }
    const merged = mergeRenderers([host])
    expect(resolveRenderer(runNowhere(), merged)!.id).toBe('spec_hooks_run_nowhere')
    expect(resolveRenderer(OTHERS[0], merged)!.id).toBe('host_notice')
  })

  it('keeps the notice and says so when the browser refuses the save', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError')
    })
    render(<>{draw(runNowhere())}</>)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.getByTestId('notice-card')).toBeInTheDocument()
    expect(screen.getByTestId('spec-hooks-notice-save-error').textContent).toContain('Not saved')
    vi.restoreAllMocks()
    expect(localStorage.getItem(KEY)).toBeNull()
  })
})

const HOOK_FILES_KEY = 'kirocrew:project-hook-files-notice-dismissed'
const HOOK_FILES_TEXT =
  "\u2139 This project has hook files in .kiro/hooks that do not run in this session: Crew cannot read this backend's workspace-trust decision, so it does not run commands from a project checkout. Your own ~/.kiro/hooks files still run."
const MARK_A = '0123456789abcdef'
const MARK_B = 'fedcba9876543210'

function hookFiles(mark: unknown = MARK_A, ts = '2026-11-22T00:00:00.000Z'): ChatMessage {
  return {
    role: 'notice',
    content: HOOK_FILES_TEXT,
    cls: 'msg msg-info',
    ts,
    meta: { kind: 'spec_hooks', spec_hooks_notice: 'project_hook_files', project_hook_files_mark: mark },
  }
}

const storedMarks = (): unknown => JSON.parse(localStorage.getItem(HOOK_FILES_KEY) ?? 'null')
const hookFilesLinks = () => screen.queryAllByTestId('project-hook-files-notice-dismiss')

describe('the project hook files notice', () => {
  it('offers a Dismiss link, and stores nothing until it is clicked', () => {
    render(<>{draw(hookFiles())}</>)
    expect(screen.getByTestId('notice-card-text').textContent).toContain('hook files in .kiro/hooks')
    const link = screen.getByRole('button', { name: 'Dismiss' })
    expect(link.getAttribute('data-testid')).toBe('project-hook-files-notice-dismiss')
    expect(localStorage.getItem(HOOK_FILES_KEY)).toBeNull()
    expect(plainDirty()).not.toContain(HOOK_FILES_KEY)
  })

  it('stores the mark on click, as the user own durable choice, and hides every copy with it', () => {
    const first = render(<>{draw(hookFiles(MARK_A, 't1'))}{draw(hookFiles(MARK_A, 't2'))}</>)
    expect(hookFilesLinks()).toHaveLength(2)
    fireEvent.click(hookFilesLinks()[0])
    expect(screen.queryAllByTestId('notice-card')).toHaveLength(0)
    expect(storedMarks()).toEqual([MARK_A])
    expect(plainDirty()).toContain(HOOK_FILES_KEY)
    first.unmount()
    // Another chat of the same folder, and a reload's fresh mount, draw nothing.
    expect(draw(hookFiles(MARK_A, 't3'))).toBeNull()
    expect(render(<>{draw(hookFiles())}</>).container.innerHTML).toBe('')
  })

  it('keeps showing, with its link, for another folder or another set of file names', () => {
    localStorage.setItem(HOOK_FILES_KEY, JSON.stringify([MARK_A]))
    render(<>{draw(hookFiles(MARK_B))}</>)
    expect(screen.getByTestId('notice-card')).toBeInTheDocument()
    expect(hookFilesLinks()).toHaveLength(1)
  })

  it('hides when another tab dismisses its mark', () => {
    render(<>{draw(hookFiles())}</>)
    act(() => {
      localStorage.setItem(HOOK_FILES_KEY, JSON.stringify([MARK_A]))
      window.dispatchEvent(new StorageEvent('storage', { key: HOOK_FILES_KEY, newValue: JSON.stringify([MARK_A]) }))
    })
    expect(screen.queryByTestId('notice-card')).toBeNull()
  })

  it('is hidden by neither the run-nowhere dismiss nor hides the run-nowhere row', () => {
    localStorage.setItem(KEY, '1')
    render(<>{draw(hookFiles())}</>)
    expect(hookFilesLinks()).toHaveLength(1)
    fireEvent.click(hookFilesLinks()[0])
    localStorage.removeItem(KEY)
    render(<>{draw(runNowhere())}</>)
    expect(screen.getByTestId('spec-hooks-notice-dismiss')).toBeInTheDocument()
  })

  it('leaves an untagged legacy row and a row with a malformed mark a plain card, with no link', () => {
    const legacy: ChatMessage = { ...hookFiles(), meta: { kind: 'spec_hooks' } }
    const malformed = [hookFiles('ABCDEF0123456789'), hookFiles('0123'), hookFiles(42), hookFiles(null)]
    for (const m of [legacy, ...malformed]) expect(isProjectHookFilesNotice(m)).toBe(false)
    expect(isProjectHookFilesNotice(hookFiles())).toBe(true)
    expect(isProjectHookFilesNotice({ ...hookFiles(), role: 'assistant' })).toBe(false)
    render(<>{[legacy, ...malformed].map((m, i) => <div key={i}>{draw(m)}</div>)}</>)
    expect(screen.getAllByTestId('notice-card')).toHaveLength(5)
    expect(screen.queryAllByRole('button', { name: 'Dismiss' })).toHaveLength(0)
  })

  it('keeps its link when a host draws the notice role its own way', () => {
    const host: MessageRenderer = { id: 'host_notice', roles: ['notice'], render: () => <p data-testid="host-notice" /> }
    expect(resolveRenderer(hookFiles(), mergeRenderers([host]))!.id).toBe('spec_hooks_project_hook_files')
  })

  it('is backed up to the host with the other dismissed tips', () => {
    expect(PROJECT_HOOK_FILES_DISMISSED_KEY).toBe(HOOK_FILES_KEY)
    expect(DURABLE_PREF_KEYS).toContain(HOOK_FILES_KEY)
  })

  it('keeps the notice and says so when the browser refuses the save', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError')
    })
    render(<>{draw(hookFiles())}</>)
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.getByTestId('notice-card')).toBeInTheDocument()
    expect(screen.getByTestId('project-hook-files-notice-save-error').textContent).toContain('Not saved')
    vi.restoreAllMocks()
    expect(localStorage.getItem(HOOK_FILES_KEY)).toBeNull()
  })

  it('keeps both marks when two folders are dismissed one after the other, and reads the list fresh', () => {
    render(<>{draw(hookFiles(MARK_A, 't1'))}{draw(hookFiles(MARK_B, 't2'))}</>)
    // Another tab dismissed a third folder after this one drew its rows.
    localStorage.setItem(HOOK_FILES_KEY, JSON.stringify(['1111111111111111']))
    fireEvent.click(hookFilesLinks()[0])
    fireEvent.click(hookFilesLinks()[0])
    expect(storedMarks()).toEqual(['1111111111111111', MARK_A, MARK_B])
    expect(screen.queryAllByTestId('notice-card')).toHaveLength(0)
  })

  it('reads an unparsable stored value as no dismissed folder', () => {
    localStorage.setItem(HOOK_FILES_KEY, '{not json')
    render(<>{draw(hookFiles())}</>)
    expect(hookFilesLinks()).toHaveLength(1)
    fireEvent.click(hookFilesLinks()[0])
    expect(storedMarks()).toEqual([MARK_A])
  })
})
