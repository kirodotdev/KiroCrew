/**
 * Closing a session tree — option B: refuse while any session in the tree runs, the pressed one included.
 *
 * The ✕ has two outcomes:
 *   - the lead or any descendant running → refuses; a notice lists which are running; nothing closes.
 *   - nothing running → closes silently (pref off) or asks once with useConfirm (pref on).
 *
 * There is no "close anyway" on the refusal notice.
 */
import { useState } from 'react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'

const chatConfig = vi.hoisted(() => ({ confirmCloseSession: false }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => chatConfig }))

const deleteSlot = vi.hoisted(() => vi.fn((key: string) => () => ({
  unwrap: async () => key,
})))
const resumeFromHistory = vi.hoisted(() => vi.fn(({ key }: { key: string; title: string }) => () => ({
  unwrap: async () => ({ ok: true, key }),
})))
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSlot,
  resumeFromHistory,
}))

import { store } from '../store'
import { planCloseTree, sidebarRowRunning, type CloseTreeRow } from '../lib/sessionCloseTree'
import { useCloseSessionTree } from '../hooks/useCloseSessionTree'
import { i18nT } from '../i18n/t'

interface Row extends CloseTreeRow {
  key: string
  title?: string
  running?: boolean
  subagents_running?: boolean
}

// `history_key` is what the server's slot payload carries for a dashboard slot.
const root = (key: string, title: string, running = false): Row =>
  ({ key, title, running, parent: null, history_key: `dashboard_${key}` })
const child = (key: string, title: string, parent: string, running = false): Row =>
  ({ key, title, running, parent: { slot: parent, key: parent }, history_key: `dashboard_${key}` })

const isRunning = (row: Row) => row.running === true
const closedOrder = () => deleteSlot.mock.calls.map(([key]) => key)

function Press({ rows }: { rows: Row[] }) {
  const { closeSessionTree, closeTreeDialog, closeTreeError, closeTreeNotice } = useCloseSessionTree({
    rows, isRunning, closeOne: (key: string) => closeOneSpy(key), openOne: (key: string) => openOneSpy(key),
  })
  return (
    <>
      <button data-testid="x" onClick={() => closeSessionTree('lead')}>x</button>
      {closeTreeDialog}
      {closeTreeError}
      {closeTreeNotice}
    </>
  )
}

function Harness({ rows }: { rows: Row[] }) {
  return (
    <Provider store={store}>
      <Press rows={rows} />
    </Provider>
  )
}

let closeOneSpy = vi.fn()
let openOneSpy = vi.fn()

/** lead ─ w1 (running) ─ sub1 (running) / sub2 ; w2 */
const RUNNING_TREE: Row[] = [
  root('lead', 'pipeline-conductor', true),
  child('w1', 'worker - babysit', 'lead', true),
  child('sub1', 'subworker - rerun lane', 'w1', true),
  child('sub2', 'subworker - log fetch', 'w1'),
  child('w2', 'worker - locale sweep', 'lead'),
]

/** Same tree, every session finished, the lead included. */
const IDLE_TREE: Row[] = RUNNING_TREE.map(r => ({ ...r, running: false }))

/** Same tree, every descendant finished but the lead itself still running. */
const LEAD_RUNNING_TREE: Row[] = IDLE_TREE.map(r => ({ ...r, running: r.key === 'lead' }))

beforeEach(() => {
  deleteSlot.mockReset()
  deleteSlot.mockImplementation((key: string) => () => ({ unwrap: async () => key }))
  closeOneSpy = vi.fn()
  openOneSpy = vi.fn()
  resumeFromHistory.mockReset()
  resumeFromHistory.mockImplementation(({ key }: { key: string; title: string }) => () => ({
    unwrap: async () => ({ ok: true, key }),
  }))
  chatConfig.confirmCloseSession = false
})

describe('planCloseTree', () => {
  it('groups descendants by depth below the pressed card', () => {
    const plan = planCloseTree(RUNNING_TREE, 'lead', isRunning)
    expect(plan.levels.map(l => [l.depth, l.sessions.map(s => s.key)])).toEqual([
      [0, ['lead']],
      [1, ['w1', 'w2']],
      [2, ['sub1', 'sub2']],
    ])
    expect(plan.total).toBe(5)
    expect(plan.descendantCount).toBe(4)
  })

  it('blocks when any descendant is running; counts the whole plan in runningTotal', () => {
    const plan = planCloseTree(RUNNING_TREE, 'lead', isRunning)
    expect(plan.runningDescendants).toBe(2)
    expect(plan.leadRunning).toBe(true)
    expect(plan.runningTotal).toBe(3)
    expect(plan.total).toBe(5)
    expect(plan.blocked).toBe(true)
    // Deepest-first order is kept for the unblocked close path.
    expect(plan.order.indexOf('sub1')).toBeLessThan(plan.order.indexOf('w1'))
    expect(plan.order.at(-1)).toBe('lead')
  })

  it('is not blocked when every session has finished, the lead included', () => {
    const plan = planCloseTree(IDLE_TREE, 'lead', isRunning)
    expect(plan.blocked).toBe(false)
    expect(plan.runningDescendants).toBe(0)
    expect(plan.leadRunning).toBe(false)
    expect(plan.order).toHaveLength(5)
  })

  it('blocks when the pressed session itself runs, even with every descendant idle', () => {
    const plan = planCloseTree(LEAD_RUNNING_TREE, 'lead', isRunning)
    expect(plan.blocked).toBe(true)
    expect(plan.leadRunning).toBe(true)
    expect(plan.runningDescendants).toBe(0)
    expect(plan.runningTotal).toBe(1)
  })

  it('blocks a running lead with nothing under it', () => {
    expect(planCloseTree([root('lead', 'solo', true)], 'lead', isRunning).blocked).toBe(true)
    expect(planCloseTree([root('lead', 'solo')], 'lead', isRunning).blocked).toBe(false)
  })

  it('plans nothing for an unknown key', () => {
    const plan = planCloseTree(RUNNING_TREE, 'gone', isRunning)
    expect(plan).toMatchObject({ total: 0, order: [], levels: [], blocked: false })
  })

  it('titles a session by its key when it has none', () => {
    const plan = planCloseTree([{ key: 'lead', parent: null }], 'lead', () => false)
    expect(plan.levels[0].sessions[0].title).toBe('lead')
  })
})

describe('useCloseSessionTree', () => {
  it('keeps ONE callback identity across a row-list change and still plans over new rows', async () => {
    const seen: Array<(key: string) => void> = []
    function Probe({ rows }: { rows: Row[] }) {
      const { closeSessionTree, closeTreeDialog } = useCloseSessionTree({
        rows, isRunning, closeOne: (key: string) => closeOneSpy(key),
      })
      seen.push(closeSessionTree)
      return (
        <>
          <button data-testid="x" onClick={() => closeSessionTree('lead')}>x</button>
          {closeTreeDialog}
        </>
      )
    }
    const user = userEvent.setup()
    const { rerender } = render(
      <Provider store={store}><Probe rows={[root('lead', 'solo')]} /></Provider>,
    )
    rerender(<Provider store={store}><Probe rows={IDLE_TREE} /></Provider>)
    expect(seen.length).toBeGreaterThan(1)
    expect(seen.at(-1)).toBe(seen[0])
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toHaveLength(5))
    expect(closeOneSpy).not.toHaveBeenCalled()
  })

  it('delegates an idle card with no descendants to the existing single close', async () => {
    const user = userEvent.setup()
    render(<Harness rows={[root('lead', 'solo')]} />)
    await user.click(screen.getByTestId('x'))
    expect(closeOneSpy).toHaveBeenCalledWith('lead')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(deleteSlot).not.toHaveBeenCalled()
  })

  it('closes a finished subtree silently with preference off (default)', async () => {
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toHaveLength(5))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2', 'lead'])
    expect(closeOneSpy).not.toHaveBeenCalled()
  })

  it('says what a prompt-free close did, and Undo reopens exactly those sessions parent-first', async () => {
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    const notice = await screen.findByTestId('close-tree-closed')
    expect(notice).toHaveTextContent('Closed 5 sessions')
    await user.click(screen.getByTestId('close-tree-undo'))
    await waitFor(() => expect(resumeFromHistory).toHaveBeenCalledTimes(5))
    // The HISTORY key Older sessions passes for the same row, never the bare slot
    // key `deleteSlot` closed: a bare key names no transcript.
    expect(resumeFromHistory.mock.calls.map(([a]) => a.key)).toEqual(
      ['dashboard_lead', 'dashboard_w2', 'dashboard_w1', 'dashboard_sub2', 'dashboard_sub1'],
    )
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2', 'lead'])
    expect(resumeFromHistory.mock.calls[0][0].title).toBe('pipeline-conductor')
    expect(screen.queryByTestId('close-tree-closed')).toBeNull()
    expect(screen.queryByTestId('close-tree-undo-failed')).toBeNull()
  })

  it('Undo sends the server history_key verbatim, never one built from the slot name', async () => {
    // An unbound channel-born slot: its name looks like a channel stem and the
    // server says its transcript is that stem; nothing on the client decides it.
    const rows: Row[] = [
      root('lead', 'pipeline-conductor'),
      { ...child('slack_1.1', 'slack thread', 'lead'), history_key: 'slack_1.1' },
    ]
    const user = userEvent.setup()
    render(<Harness rows={rows} />)
    await user.click(screen.getByTestId('x'))
    await user.click(await screen.findByTestId('close-tree-undo'))
    await waitFor(() => expect(resumeFromHistory).toHaveBeenCalledTimes(2))
    expect(resumeFromHistory.mock.calls.map(([a]) => a.key)).toEqual(['dashboard_lead', 'slack_1.1'])
  })

  it('Undo skips a session the payload gave no history_key and points to Older sessions', async () => {
    const rows: Row[] = [
      root('lead', 'pipeline-conductor'),
      { ...child('w1', 'no-key worker', 'lead'), history_key: undefined },
    ]
    const user = userEvent.setup()
    render(<Harness rows={rows} />)
    await user.click(screen.getByTestId('x'))
    const receipt = await screen.findByTestId('close-tree-closed')
    expect(screen.getByTestId('close-tree-elsewhere')).toHaveTextContent('Reopen no-key worker from Older sessions.')
    expect(receipt).not.toHaveTextContent('Reopen pipeline-conductor')
    await user.click(screen.getByTestId('close-tree-undo'))
    await waitFor(() => expect(resumeFromHistory).toHaveBeenCalledTimes(1))
    expect(resumeFromHistory.mock.calls[0][0].key).toBe('dashboard_lead')
  })

  it('offers no Undo when no closed session has a history_key', async () => {
    const rows: Row[] = [
      { ...root('lead', 'pipeline-conductor'), history_key: undefined },
      { ...child('w1', 'no-key worker', 'lead'), history_key: undefined },
    ]
    const user = userEvent.setup()
    render(<Harness rows={rows} />)
    await user.click(screen.getByTestId('x'))
    await screen.findByTestId('close-tree-closed')
    expect(screen.queryByTestId('close-tree-undo')).toBeNull()
    expect(screen.getByTestId('close-tree-elsewhere')).toHaveTextContent('pipeline-conductor, no-key worker')
  })

  it('sidebarRowRunning counts the slot\'s own subagents_running snapshot', () => {
    const none = new Set<string>()
    expect(sidebarRowRunning({ key: 'a' }, none, {})).toBe(false)
    expect(sidebarRowRunning({ key: 'a', subagents_running: true }, none, {})).toBe(true)
    expect(sidebarRowRunning({ key: 'a', subagents_running: false }, none, {})).toBe(false)
    expect(sidebarRowRunning({ key: 'a' }, new Set(['a']), {})).toBe(true)
    expect(sidebarRowRunning({ key: 'a' }, none, { a: 2 })).toBe(true)
    // Through the plan: a descendant with only the snapshot set blocks the close.
    const rows = [root('lead', 'l'), { ...child('w', 'w', 'lead'), subagents_running: true }]
    expect(planCloseTree(rows, 'lead', r => sidebarRowRunning(r, none, {})).blocked).toBe(true)
  })

  it('Undo names a session it could not reopen', async () => {
    resumeFromHistory.mockImplementation(({ key }: { key: string; title: string }) => () => ({
      unwrap: async () => ({ ok: key !== 'dashboard_w2', key }),
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await user.click(await screen.findByTestId('close-tree-undo'))
    const failed = await screen.findByTestId('close-tree-undo-failed')
    expect(failed).toHaveTextContent('worker - locale sweep')
    expect(failed).not.toHaveTextContent('worker - babysit')
  })

  it('the receipt counts only the sessions that did close', async () => {
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => {
        if (key === 'w1') throw new Error('save failed')
        return key
      },
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    // w1 refused, so the lead above it stays open too: sub1, sub2 and w2 closed.
    // Beside the "Still open" error, the receipt names what did close.
    expect(await screen.findByTestId('close-tree-closed')).toHaveTextContent(
      'Closed 3 sessions: worker - locale sweep, subworker - log fetch, subworker - rerun lane',
    )
  })

  it('asks once with the in-app confirm when the preference is on', async () => {
    chatConfig.confirmCloseSession = true
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    // The in-app confirm opens (not window.confirm).
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()
    // Nothing closed while the question is open.
    expect(deleteSlot).not.toHaveBeenCalled()
    // One count per role: the title counts the 4 UNDER it, the button all 5 closed.
    expect(await screen.findByRole('button', { name: /Close all 5/i })).toBeInTheDocument()
    expect(screen.getByText('Close this session and the 4 sessions under it?')).toBeInTheDocument()
    // A running session refuses before this ask, so the body says all finished.
    expect(screen.getByText(/They have all finished/)).toBeInTheDocument()
    // Confirm closes the whole tree.
    await user.click(screen.getByRole('button', { name: /Close all 5/i }))
    await waitFor(() => expect(closedOrder()).toHaveLength(5))
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2', 'lead'])
    // A close the person just confirmed gets no second receipt.
    expect(screen.queryByTestId('close-tree-closed')).toBeNull()
  })

  it('a running card with nothing under it keeps the plain single close', async () => {
    const user = userEvent.setup()
    render(<Harness rows={[root('lead', 'solo', true)]} />)
    await user.click(screen.getByTestId('x'))
    expect(closeOneSpy).toHaveBeenCalledWith('lead')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(deleteSlot).not.toHaveBeenCalled()
  })

  it('a running lead with idle descendants refuses, with the preference on or off', async () => {
    for (const asked of [false, true]) {
      chatConfig.confirmCloseSession = asked
      const user = userEvent.setup()
      const { unmount } = render(<Harness rows={LEAD_RUNNING_TREE} />)
      await user.click(screen.getByTestId('x'))
      await screen.findByRole('dialog')
      expect(screen.getByTestId('close-tree-title')).toHaveTextContent("Can't close: this session is still running")
      expect(screen.getByTestId('close-tree-can-close')).toHaveTextContent('This session can be closed once it finishes.')
      // The Stop hint shows for the running lead too: its own row is the link.
      expect(screen.getByTestId('close-tree-body')).toHaveTextContent("Open a running session and press Stop generation (the square button in the chat's message box), or let it finish.")
      // The lead is listed as Running, counted, and opens itself like any running row.
      const lead = screen.getByTestId('close-tree-session-lead')
      expect(lead).toHaveAttribute('data-status', 'running')
      expect(lead.tagName).toBe('BUTTON')
      expect(screen.getByTestId('close-tree-levels').querySelectorAll('[data-status="running"]')).toHaveLength(1)
      // No confirm: the refusal is the only dialog.
      expect(screen.queryByRole('button', { name: /Close all/i })).toBeNull()
      expect(deleteSlot).not.toHaveBeenCalled()
      unmount()
    }
  })

  it('an idle lead that leaves the lane rows once its workers close is read from allRows and closes', async () => {
    let rows: Row[] = IDLE_TREE
    function Shrink() {
      const [, force] = useState(0)
      const { closeSessionTree } = useCloseSessionTree({
        rows, allRows: IDLE_TREE, isRunning, closeOne: () => {},
      })
      return <button data-testid="x" onClick={() => { closeSessionTree('lead'); rows = IDLE_TREE.filter(r => r.key !== 'lead'); force(n => n + 1) }}>x</button>
    }
    const user = userEvent.setup()
    render(<Provider store={store}><Shrink /></Provider>)
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2', 'lead']))
  })

  it('the lead starting to run before its own close keeps it open', async () => {
    // w2 is the last close before the lead's; while it is pending, the lead starts a turn.
    let release!: () => void
    const held = new Promise<void>(r => { release = r })
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => { if (key === 'w2') await held; return key },
    }))
    const user = userEvent.setup()
    const { rerender } = render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2']))
    rerender(<Harness rows={IDLE_TREE.map(r => (r.key === 'lead' ? { ...r, running: true } : r))} />)
    release()
    expect(await screen.findByTestId('close-tree-kept-running')).toHaveTextContent(
      'Kept open: pipeline-conductor, because it started running.')
    expect(screen.queryByTestId('close-tree-refused')).toBeNull()
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2'])
  })

  it('a descendant that starts running mid-close is kept, with every session above it', async () => {
    // sub1's close is held open; while it is pending, sub2 starts a turn.
    let release!: () => void
    const held = new Promise<void>(r => { release = r })
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => { if (key === 'sub1') await held; return key },
    }))
    const user = userEvent.setup()
    const { rerender } = render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toEqual(['sub1']))
    rerender(<Harness rows={IDLE_TREE.map(r => (r.key === 'sub2' ? { ...r, running: true } : r))} />)
    release()
    // A skip is not a failure: a plain status line, never the red error.
    const notice = await screen.findByTestId('close-tree-kept')
    expect(screen.queryByTestId('close-tree-refused')).toBeNull()
    expect(notice).not.toHaveTextContent('Ask the agent')
    // sub2 is never closed, nor w1 and the lead above it; w2 is off that path.
    expect(closedOrder()).toEqual(['sub1', 'w2'])
    // One line per reason, each name beside the reason true for it.
    expect(screen.getByTestId('close-tree-kept-running')).toHaveTextContent(
      'Kept open: subworker - log fetch, because it started running.')
    const below = screen.getAllByTestId('close-tree-kept-below').map(el => el.textContent)
    expect(below).toEqual([
      'Kept open: worker - babysit, because subworker - log fetch under it did not close.',
      'Kept open: pipeline-conductor, because worker - babysit under it did not close.',
    ])
    expect(notice).not.toHaveTextContent(' or ')
  })

  it('a sibling branch that starts running mid-close keeps only its own path', async () => {
    let release!: () => void
    const held = new Promise<void>(r => { release = r })
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => { if (key === 'sub1') await held; return key },
    }))
    const user = userEvent.setup()
    const { rerender } = render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toEqual(['sub1']))
    rerender(<Harness rows={IDLE_TREE.map(r => (r.key === 'w2' ? { ...r, running: true } : r))} />)
    release()
    expect(await screen.findByTestId('close-tree-kept-running')).toHaveTextContent(
      'Kept open: worker - locale sweep, because it started running.')
    expect(screen.getByTestId('close-tree-kept-below')).toHaveTextContent(
      'Kept open: pipeline-conductor, because worker - locale sweep under it did not close.')
    expect(screen.queryByTestId('close-tree-refused')).toBeNull()
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1'])
  })

  it('a planned descendant that leaves the live rows mid-close is kept, with every session above it', async () => {
    // sub1's close is held open; while it is pending the lane is left, so the
    // live rows no longer carry the rest of the tree and their state is unknown.
    let release!: () => void
    const held = new Promise<void>(r => { release = r })
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => { if (key === 'sub1') await held; return key },
    }))
    const user = userEvent.setup()
    const { rerender } = render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toEqual(['sub1']))
    rerender(<Harness rows={IDLE_TREE.filter(r => r.key === 'lead')} />)
    release()
    expect(await screen.findByTestId('close-tree-kept-unknown')).toHaveTextContent(
      'Kept open: subworker - log fetch, worker - locale sweep, because their state could not be read.')
    expect(screen.queryByTestId('close-tree-kept-running')).toBeNull()
    expect(screen.queryByTestId('close-tree-refused')).toBeNull()
    // Nothing past the held close is deleted: not the unseen workers, not the lead above them.
    expect(closedOrder()).toEqual(['sub1'])
  })

  it('cancelling the preference confirm closes nothing', async () => {
    chatConfig.confirmCloseSession = true
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await user.click(await screen.findByRole('button', { name: /Cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(deleteSlot).not.toHaveBeenCalled()
  })

  it('refuses while the lead and descendants run: notice lists levels, nothing closes', async () => {
    const user = userEvent.setup()
    render(<Harness rows={RUNNING_TREE} />)
    await user.click(screen.getByTestId('x'))
    // The notice opens.
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    // Nothing closed.
    expect(deleteSlot).not.toHaveBeenCalled()
    // Level headings name depth.
    const levels = screen.getByTestId('close-tree-levels')
    expect(levels.querySelectorAll('[data-testid^="close-tree-level-"]')).toHaveLength(3)
    expect(screen.getByTestId('close-tree-level-0')).toHaveTextContent('This session')
    expect(screen.getByTestId('close-tree-level-1')).toHaveTextContent('1 level under')
    expect(screen.getByTestId('close-tree-level-2')).toHaveTextContent('2 levels under')
    // Every session named; every session UNDER the lead marked.
    for (const row of RUNNING_TREE) {
      const entry = screen.getByTestId(`close-tree-session-${row.key}`)
      expect(entry).toHaveTextContent(row.title!)
      if (row.key === 'lead') continue
      expect(entry).toHaveTextContent(row.running ? 'Running' : 'Finished')
    }
    // The pressed session is running too, so it is marked and counted like the
    // rows under it: the title counts all 3, the same number the X shows, and the
    // list shows exactly those 3 running rows.
    const lead = screen.getByTestId('close-tree-session-lead')
    expect(screen.getByTestId('close-tree-tag-lead')).toHaveTextContent(/^Running$/)
    expect(lead).toHaveAttribute('data-status', 'running')
    expect(screen.getByTestId('close-tree-title')).toHaveTextContent("Can't close: 3 sessions are still running, including this one")
    expect(levels.querySelectorAll('[data-status="running"]')).toHaveLength(3)
    // The body says how to act, and does not contradict the refusal banner.
    // It says the lead can be closed later, and names the composer's Stop control.
    expect(screen.getByTestId('close-tree-can-close')).toHaveTextContent('This session can be closed once it and the sessions under it finish.')
    expect(screen.getByTestId('close-tree-body')).toHaveTextContent("Open a running session and press Stop generation (the square button in the chat's message box), or let it finish.")
    expect(screen.getByTestId('close-tree-body').textContent).toContain(i18nT('components.chatInput.stop_generation'))
    // Exactly ONE action: dismiss. No "close anyway" button.
    const buttons = screen.getAllByRole('button')
    void buttons // suppress unused lint warning
    expect(screen.getByTestId('close-tree-dismiss')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Close all/i })).toBeNull()
  })

  it('a row running only through subagents is badged so in the notice', async () => {
    // w2's own turn is idle, but its subagents are working.
    function BgPress({ rows }: { rows: Row[] }) {
      const { closeSessionTree, closeTreeDialog } = useCloseSessionTree({
        rows,
        isRunning: (r: Row) => r.running === true || r.subagents_running === true,
        isBackgroundOnly: (r: Row) => r.running !== true && r.subagents_running === true,
        closeOne: () => {},
      })
      return (<><button data-testid="x" onClick={() => closeSessionTree('lead')}>x</button>{closeTreeDialog}</>)
    }
    const rows: Row[] = [
      root('lead', 'pipeline-conductor'),
      child('w1', 'worker - babysit', 'lead', true),
      { ...child('w2', 'worker - locale sweep', 'lead'), subagents_running: true },
    ]
    const user = userEvent.setup()
    render(<Provider store={store}><BgPress rows={rows} /></Provider>)
    await user.click(screen.getByTestId('x'))
    await screen.findByRole('dialog')
    expect(screen.getByTestId('close-tree-session-w2')).toHaveTextContent('Running in background')
    expect(screen.getByTestId('close-tree-session-w1')).toHaveTextContent('Running')
    expect(screen.getByTestId('close-tree-session-w1')).not.toHaveTextContent('background')
    expect(screen.getByTestId('close-tree-title')).toHaveTextContent("Can't close: 2 sessions under this one are still running")
    // An idle lead carries no status tag.
    expect(screen.queryByTestId('close-tree-tag-lead')).toBeNull()
    expect(screen.getByTestId('close-tree-session-lead')).not.toHaveAttribute('data-status')
    expect(screen.getByTestId('close-tree-can-close')).toHaveTextContent('This session can be closed once the sessions under it finish.')
  })

  it('a running row in the notice opens that session and closes the notice', async () => {
    const user = userEvent.setup()
    render(<Harness rows={RUNNING_TREE} />)
    await user.click(screen.getByTestId('x'))
    const running = await screen.findByTestId('close-tree-session-sub1')
    expect(running.tagName).toBe('BUTTON')
    // A finished row is not a control.
    expect(screen.getByTestId('close-tree-session-sub2').tagName).not.toBe('BUTTON')
    await user.click(running)
    expect(openOneSpy).toHaveBeenCalledWith('sub1')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(deleteSlot).not.toHaveBeenCalled()
  })

  it('dismissing the notice closes nothing', async () => {
    const user = userEvent.setup()
    render(<Harness rows={RUNNING_TREE} />)
    await user.click(screen.getByTestId('x'))
    await user.click(await screen.findByTestId('close-tree-dismiss'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(deleteSlot).not.toHaveBeenCalled()
    expect(closeOneSpy).not.toHaveBeenCalled()
  })

  it('reports a refused close by name instead of swallowing it', async () => {
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => {
        if (key === 'w1') throw new Error('save failed')
        return key
      },
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    const notice = await screen.findByTestId('close-tree-refused')
    expect(notice).toHaveTextContent('worker - babysit')
    expect(notice).not.toHaveTextContent('save failed')
    expect(notice).not.toHaveTextContent('worker - locale sweep')
    // The lead above the refusal is never asked to close, so the refused worker
    // keeps its lead instead of standing alone at the top level.
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w1', 'w2'])
    // The error names only the session whose close failed; the lead kept open
    // above it is plain status, not a second failure.
    expect(notice).toHaveTextContent('Still open: worker - babysit. Try closing it again in a moment.')
    expect(notice).not.toHaveTextContent('pipeline-conductor')
    expect(screen.getByTestId('close-tree-kept-below')).toHaveTextContent(
      'Kept open: pipeline-conductor, because worker - babysit under it did not close.')
  })

  it('a deep refusal keeps every ancestor open and still closes the siblings', async () => {
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => {
        if (key === 'sub1') throw new Error('save failed')
        return key
      },
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    const notice = await screen.findByTestId('close-tree-refused')
    // sub1 refused -> w1 and lead stay; sub2 and w2 are off the refused path.
    expect(closedOrder()).toEqual(['sub1', 'sub2', 'w2'])
    expect(notice).toHaveTextContent('subworker - rerun lane')
    expect(notice).not.toHaveTextContent('worker - babysit')
    expect(screen.getAllByTestId('close-tree-kept-below').map(el => el.textContent)).toEqual([
      'Kept open: worker - babysit, because subworker - rerun lane under it did not close.',
      'Kept open: pipeline-conductor, because worker - babysit under it did not close.',
    ])
    expect(notice).not.toHaveTextContent('subworker - log fetch')
    expect(await screen.findByTestId('close-tree-closed')).toHaveTextContent('Closed 2 sessions')
  })

  it('one refused session reads in the singular', async () => {
    // Only the lead refuses, so exactly one session is still open.
    deleteSlot.mockImplementation((key: string) => () => ({
      unwrap: async () => {
        if (key === 'lead') throw new Error('save failed')
        return key
      },
    }))
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    const notice = await screen.findByTestId('close-tree-refused')
    expect(notice).toHaveTextContent('Still open: pipeline-conductor. Try closing it again in a moment.')
  })

  it('reports nothing when every close succeeds', async () => {
    const user = userEvent.setup()
    render(<Harness rows={IDLE_TREE} />)
    await user.click(screen.getByTestId('x'))
    await waitFor(() => expect(closedOrder()).toHaveLength(5))
    expect(screen.queryByTestId('close-tree-refused')).toBeNull()
    expect(screen.queryByTestId('close-tree-kept')).toBeNull()
  })
})

describe('close-tree copy: singular forms', () => {
  it('reads correctly for exactly one', () => {
    expect(i18nT('hooks.useCloseSessionTree.title', { count: 1 })).toBe("Can't close: 1 session under this one is still running")
    expect(i18nT('pages.chatSidebar.close_blocked', { count: 1 })).toBe("Can't close: 1 session is still running")
    expect(i18nT('pages.chatSidebar.close_blocked_short', { count: 1 })).toBe("Can't close: 1 running")
    expect(i18nT('hooks.useCloseSessionTree.title_self')).toBe("Can't close: this session is still running")
    expect(i18nT('hooks.useCloseSessionTree.title_including_self', { count: 1 })).toBe("Can't close: 1 session is still running, including this one")
    expect(i18nT('hooks.useCloseSessionTree.pref_title', { count: 1 })).toBe('Close this session and the 1 session under it?')
    expect(i18nT('hooks.useCloseSessionTree.pref_confirm', { count: 1 })).toBe('Close all 1')
    expect(i18nT('hooks.useCloseSessionTree.closed', { count: 1 })).toBe('Closed 1 session')
    expect(i18nT('hooks.useCloseSessionTree.level', { count: 1 })).toBe('1 level under')
    expect(i18nT('pages.chatSidebar.close_session_tree', { count: 1, total: 2 })).toBe('Close all 2: this session and the 1 under it (you can undo or reopen them from Older sessions)')
    expect(i18nT('hooks.useCloseSessionTree.refused', { count: 1, names: 'a' })).toBe('Still open: a. Try closing it again in a moment.')
    expect(i18nT('hooks.useCloseSessionTree.kept_running', { count: 1, names: 'a' })).toBe('Kept open: a, because it started running.')
    expect(i18nT('hooks.useCloseSessionTree.kept_unknown', { count: 1, names: 'a' })).toBe('Kept open: a, because its state could not be read.')
    expect(i18nT('hooks.useCloseSessionTree.kept_below', { count: 1, names: 'a', cause: 'c' })).toBe('Kept open: a, because c under it did not close.')
    expect(i18nT('pages.chatSidebar.close_all', { count: 1 })).toBe('Close all 1')
    expect(i18nT('pages.chatSidebar.close_session_tree_hint', { count: 1 })).toBe('This session and the 1 under it. You can undo, or reopen them from Older sessions.')
    expect(i18nT('hooks.useCloseSessionTree.undo_failed', { count: 1, names: 'a' })).toBe('Not reopened: a. You can still open it from Older sessions.')
  })

  it('reads correctly for many', () => {
    expect(i18nT('hooks.useCloseSessionTree.title', { count: 3 })).toBe("Can't close: 3 sessions under this one are still running")
    expect(i18nT('pages.chatSidebar.close_blocked', { count: 4 })).toBe("Can't close: 4 sessions are still running")
    expect(i18nT('pages.chatSidebar.close_blocked_short', { count: 4 })).toBe("Can't close: 4 running")
    expect(i18nT('hooks.useCloseSessionTree.title_including_self', { count: 4 })).toBe("Can't close: 4 sessions are still running, including this one")
    expect(i18nT('hooks.useCloseSessionTree.closed', { count: 5 })).toBe('Closed 5 sessions')
    expect(i18nT('pages.chatSidebar.close_session_tree', { count: 4, total: 5 })).toBe('Close all 5: this session and the 4 under it (you can undo or reopen them from Older sessions)')
    expect(i18nT('hooks.useCloseSessionTree.refused', { count: 2, names: 'a, b' })).toBe('Still open: a, b. Try closing them again in a moment.')
    expect(i18nT('hooks.useCloseSessionTree.kept_running', { count: 2, names: 'a, b' })).toBe('Kept open: a, b, because they started running.')
    expect(i18nT('hooks.useCloseSessionTree.kept_unknown', { count: 2, names: 'a, b' })).toBe('Kept open: a, b, because their state could not be read.')
    expect(i18nT('hooks.useCloseSessionTree.kept_below', { count: 2, names: 'a, b', cause: 'c' })).toBe('Kept open: a, b, because c under them did not close.')
    expect(i18nT('pages.chatSidebar.close_all', { count: 5 })).toBe('Close all 5')
    expect(i18nT('pages.chatSidebar.close_session_tree_hint', { count: 4 })).toBe('This session and the 4 under it. You can undo, or reopen them from Older sessions.')
    expect(i18nT('hooks.useCloseSessionTree.undo_failed', { count: 2, names: 'a, b' })).toBe('Not reopened: a, b. You can still open them from Older sessions.')
  })
})
