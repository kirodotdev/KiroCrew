import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'

import ModelEffortDropdown from '../components/ModelEffortDropdown'
import AgentSelector from '../components/AgentSelector'
import type { KiroCrewAgent } from '../components/AgentSelector'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { api } from '../api/client'

// The nested ReasoningEffortDropdown persists slider picks over the wire
// (#5120); these tests never touch the slider, but the stub keeps an
// accidental future interaction from hitting a real fetch.
vi.spyOn(api, 'chatSlotReasoningEffort').mockResolvedValue({ ok: true } as never)

/**
 * A dropdown's filter box is CHROME. Its placeholder ("Type to filter…") is
 * prose, and its value is a partial query fragment — "opu" narrowing a list —
 * not an identifier the user transcribes. Both must therefore follow
 * Settings → Display → Font Family, which writes only `--font-body`
 * (hooks/useZoom.ts). Tailwind's `font-mono` resolves to `var(--mono)`, a token
 * that setting never writes, so a `font-mono` here pinned JetBrains Mono no
 * matter what the user picked. These were also the only `Input` call sites in
 * the app that overrode the primitive's own `font-body` (components/ui.tsx).
 *
 * Same reclassification as #1008 (chat chrome) and #1970 (message footer):
 * prose and labels inherit, and only verbatim identifiers — a command, a path,
 * a git ref — re-declare mono. The last test is the counterweight: inputs that
 * really do carry code must KEEP mono, so this rule cannot be over-applied into
 * "no input is ever monospace".
 */

const SRC = join(__dirname, '..')

const baseProps = {
  anchorRect: { right: 400, top: 300 } as DOMRect,
  dropdownRef: React.createRef<HTMLDivElement>(),
  inputRef: React.createRef<HTMLInputElement>(),
  models: [{ name: 'auto', description: 'Default' }, { name: 'claude-opus-4.8' }],
  activeModel: 'auto',
  onSelectModel: vi.fn(),
  filter: '',
  setFilter: vi.fn(),
  onClose: vi.fn(),
  hasEffort: false,
  slot: 'dashboard:1',
  currentEffort: '',
  onListKeyDown: vi.fn(),
}

const agents: KiroCrewAgent[] = [
  { name: 'coding', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Coding agent', source: 'kirocrew' },
]

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The nested ReasoningEffortDropdown persists picks into the slot store
  // (#5120), so the tree needs the redux context. A per-render store (not
  // the app singleton) keeps one test's persist from leaking into the next.
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
}

describe('dropdown filter inputs follow the Font Family setting', () => {
  it('does not pin the model picker filter to font-mono', () => {
    wrap(<ModelEffortDropdown {...baseProps} />)
    const input = screen.getByPlaceholderText('Type to filter…')
    expect(input.className).not.toContain('font-mono')
    // Guards against asserting on the wrong node: the Input primitive's own
    // font-body must be what survives, not a bare class-less input.
    expect(input.className).toContain('font-body')
  })

  it('does not pin the agent picker filter to font-mono', () => {
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={vi.fn()} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    const input = screen.getByPlaceholderText('Type to filter…')
    expect(input.className).not.toContain('font-mono')
    expect(input.className).toContain('font-body')
  })

  /**
   * The other five inputs live on surfaces too heavy to mount for a class
   * assertion (ChatPage, AgentsPage) or behind a portal opened by a chain of
   * clicks (ChatPane's two dropdowns). They share one class shape, so the shape
   * itself is the invariant: it must never carry mono again.
   */
  it('leaves no font-mono on the shared filter-input class shape', () => {
    const files = [
      'components/ModelEffortDropdown.tsx',
      'components/AgentSelector.tsx',
      'components/AgentSkillsEditor.tsx',
      'pages/AgentsPage.tsx',
      'pages/chat/ChatPageView.tsx',
    ]
    const offenders: string[] = []
    const uncovered: string[] = []
    for (const rel of files) {
      // Flattened FIRST. A line-by-line scan has a false negative a reviewer
      // found: reformat the className across lines and the shape stays on one
      // line while `font-mono` moves to another, so the scan reports zero
      // offenders while the input is still pinned to mono. Flattening also
      // makes the match a whole quoted run, so the assertion sees the complete
      // class list rather than one line of it.
      const flat = readFileSync(join(SRC, rel), 'utf8').replace(/\s+/g, ' ')
      const runs = [...flat.matchAll(/["'`]([^"'`]*px-2 py-1 text-\[13px\][^"'`]*)["'`]/g)]
      // Per file, never summed. An aggregate floor sits exactly on the boundary
      // here (one match per file), so a refactor that inlines one filter box
      // while any other listed file gains an unrelated line of the same shape
      // keeps the total at five and silently narrows the scan to four sites.
      if (runs.length === 0) uncovered.push(rel)
      for (const m of runs) if (m[1].includes('font-mono')) offenders.push(`${rel}: ${m[1]}`)
    }
    expect(uncovered).toEqual([])
    expect(offenders).toEqual([])
  })

  it('does not pin ChatPane\u2019s shared dropdown filter class to font-mono', () => {
    // Both of ChatPane's dropdowns read one `ddInputCls` constant, so the class
    // string is asserted where it is declared rather than where it is applied.
    const src = readFileSync(join(SRC, 'components/ChatPane.tsx'), 'utf8')
    const decl = src.split('\n').find(l => l.includes('ddInputCls ='))
    expect(decl).toBeDefined()
    expect(decl).not.toContain('font-mono')
    expect(decl).toContain('px-2 py-1 text-[13px]')
    // These two are RAW <input> elements portaled to document.body, not the
    // shared Input primitive, so dropping font-mono leaves them with no font
    // declaration at all — they would reach --font-body only through Tailwind
    // preflight's `font-family: inherit`. A reviewer flagged that as one
    // preflight change or one mono-carrying ancestor away from silently
    // reverting. Declaring font-body puts them on the same footing as the five
    // Input call sites, which get it from the primitive.
    expect(decl).toContain('font-body')
  })

  it('keeps mono on inputs that carry code rather than prose', () => {
    // Counterweight to the rule above. The hook command field holds a shell
    // command, so it EARNS monospace under the #1008 classification — a sweep
    // that stripped it too would be a regression, not a fix.
    const src = readFileSync(join(SRC, 'pages/HooksPage.tsx'), 'utf8')
    const cmd = src.split('\n').find(l => l.includes('<Input') && l.includes('setCommand'))
    expect(cmd).toBeDefined()
    expect(cmd).toContain('font-mono')
  })

  it('keeps mono on the one filter box whose corpus is itself monospace', () => {
    // The documented carve-out, asserted so it stays a decision instead of
    // decaying into an omission. LogsPage's "Filter logs" box is chrome by the
    // rule above and yet keeps mono on purpose: its value is scanned against log
    // lines that are themselves font-mono, and matches are highlighted inline in
    // those rows, so the query is aligned to its corpus. That clause is NOT part
    // of the general rule, which is exactly why it is written down here — a
    // reviewer found it as an apparent counter-example to this commit's claim.
    const flat = readFileSync(join(SRC, 'pages/LogsPage.tsx'), 'utf8').replace(/\s+/g, ' ')
    const at = flat.indexOf("pages.logsPage.filter_logs'")
    expect(at).toBeGreaterThan(-1)
    const open = flat.lastIndexOf('<input', at)
    const box = flat.slice(open, flat.indexOf('/>', at) + 2)
    expect(box).toContain('font-mono')
    // The corpus it is aligned to. If the log rows ever stop being monospace,
    // the justification for the box evaporates and this test should fail.
    expect(flat).toMatch(/data-testid="log-line"[^>]*font-mono|font-mono[^>]*data-testid="log-line"/)
  })
})
