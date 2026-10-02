/** "Merge into parent…" appears only on a fork, explains a disabled row inline,
 *  and opens the dialog through the ChatPage-scoped provider. The parent's
 *  card names the fork and opens it. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import MergeBackMenuItem from '../components/MergeBackMenuItem'
import MergedFromLabel from '../pages/chat/MergedFromLabel'
import { MergeBackDialogProvider, useMergeBackDialog } from '../hooks/useMergeBackDialog'
import { mergedFromOf } from '../types/mergeBack'
import { mergeBackErrorMessage } from '../utils/mergeBackError'
import { i18nT } from '../i18n/t'

function Item({ children, disabled, onSelect, ...rest }: { children?: ReactNode; disabled?: boolean; onSelect?: (event: Event) => void; 'data-testid'?: string }) {
  return (
    <button type="button" disabled={disabled} onClick={e => onSelect?.(e.nativeEvent)} data-testid={rest['data-testid']}>
      {children}
    </button>
  )
}

function OpenFork() {
  const dialog = useMergeBackDialog()
  return <span data-testid="open-fork">{dialog?.forkKey ?? ''}</span>
}

const FORK: ChatSlot = { key: 'fork', title: '↳ Fork of A', messages: 4, running: false, forked_from: 'dashboard:a' }

function mountItem(slot: ChatSlot, { provider = true } = {}) {
  const store = createTestStore()
  store.dispatch(sseSlots([slot]))
  const item = <><MergeBackMenuItem Item={Item} slotKey={slot.key} /><OpenFork /></>
  return renderWithProviders(provider ? <MergeBackDialogProvider>{item}</MergeBackDialogProvider> : item, { store })
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MergeBackMenuItem', () => {
  it('renders nothing on a chat that is not a fork', () => {
    mountItem({ key: 'plain', title: 'Plain', messages: 2, running: false })
    expect(screen.queryByTestId('merge-back')).not.toBeInTheDocument()
  })

  it('renders nothing outside the provider', () => {
    mountItem(FORK, { provider: false })
    expect(screen.queryByTestId('merge-back')).not.toBeInTheDocument()
  })

  it('opens the dialog for this fork', async () => {
    const user = userEvent.setup()
    mountItem(FORK)
    await user.click(screen.getByTestId('merge-back'))
    expect(screen.getByTestId('open-fork')).toHaveTextContent('fork')
  })

  // The reason says why merging is off, not which kind of chat this is: a
  // greyed row named "Incognito chat" told the person nothing they did not know.
  it.each([
    [{ running: true }, 'A turn is still running'],
    [{ memory_mode: 'incognito' as const }, 'Nothing leaves an incognito chat'],
    [{ memory_mode: 'temporary' as const }, 'Nothing leaves a temporary chat'],
    // A parent outside the dashboard is named by what it is, so the cause is
    // something the person knows: the product the chat is on, or the job.
    [{ forked_from: 'slack:1700000000.000001' }, 'The parent is a Slack chat'],
    // A channel-born parent not bound to its channel session: the gateway
    // refuses it for the same reason, so the row says so before the dialog would.
    [{ forked_from: 'dashboard:slack_1700000000.000001' }, 'The parent is a Slack chat'],
    [{ forked_from: 'dashboard:discord_1700000000' }, 'The parent is a Discord chat'],
    [{ forked_from: 'unified:owner' }, 'The parent is a direct-message chat'],
    [{ forked_from: 'cron:daily' }, 'The parent is a scheduled job'],
    // A session outside the dashboard the client cannot name keeps the generic reason.
    [{ forked_from: 'hook:review-42' }, 'The parent chat runs outside the dashboard'],
  ])('is disabled with its reason for %o', (over, reason) => {
    mountItem({ ...FORK, ...over })
    const row = screen.getByTestId('merge-back')
    expect(row).toBeDisabled()
    expect(row).toHaveTextContent(reason)
  })

  it('is enabled for a dashboard parent', () => {
    mountItem(FORK)
    expect(screen.getByTestId('merge-back')).toBeEnabled()
  })

  // Only the bare `cron:` session key names a job: a dashboard chat whose own key
  // spells `cron_` is an ordinary dashboard chat to the gateway.
  it('is enabled for a dashboard parent whose key only resembles a scheduled job', () => {
    mountItem({ ...FORK, forked_from: 'dashboard:cron_daily' })
    expect(screen.getByTestId('merge-back')).toBeEnabled()
  })

  // The parent's kind outranks the fork's turn: the turn ends, the parent stays
  // what it is.
  it('names the parent, not the running turn, on a running fork of a Slack chat', () => {
    mountItem({ ...FORK, running: true, forked_from: 'slack:1700000000.000001' })
    expect(screen.getByTestId('merge-back')).toHaveTextContent('The parent is a Slack chat')
  })

  // Lowercase stems only, as the gateway mints them: a person's own title can
  // spell a channel's name without the chat having started there.
  it('is enabled for a dashboard parent whose key only resembles a channel stem', () => {
    mountItem({ ...FORK, forked_from: 'dashboard:Slack_thread_triage' })
    expect(screen.getByTestId('merge-back')).toBeEnabled()
  })
})

describe('MergedFromLabel', () => {
  const block = { session: 'dashboard:fork', slot: 'fork', title: '↳ Fork of A', createdAt: '2026-10-02T18:00:00+00:00', through: 'm', messages: 2 }

  it('names the fork and opens it', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    render(<MergedFromLabel block={block} onOpen={onOpen} />)
    expect(screen.getByTestId('merged-from-label')).toHaveTextContent('Merged from ↳ Fork of A')
    expect(screen.getByTestId('merged-from-label')).toHaveTextContent('2 messages')
    await user.click(screen.getByTestId('merged-from-open'))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('is plain text where there is no way to open a session', () => {
    render(<MergedFromLabel block={{ ...block, messages: 1 }} />)
    expect(screen.queryByTestId('merged-from-open')).not.toBeInTheDocument()
    expect(screen.getByTestId('merged-from-label')).toHaveTextContent('1 message')
  })

  it('names an untitled fork generically', () => {
    render(<MergedFromLabel block={{ ...block, title: '' }} />)
    expect(screen.getByTestId('merged-from-label')).toHaveTextContent('Merged from a fork')
  })
})

describe('mergedFromOf', () => {
  const good = { mergedFrom: { session: 's', slot: 'f', title: '', createdAt: '2026-10-02T18:00:00+00:00', through: 'm', messages: 1 } }

  it('reads a well-formed block', () => {
    expect(mergedFromOf(good)).toEqual(good.mergedFrom)
  })

  it.each([
    [undefined],
    [{}],
    [{ mergedFrom: 'x' }],
    [{ mergedFrom: { ...good.mergedFrom, messages: 0 } }],
    [{ mergedFrom: { ...good.mergedFrom, messages: 1.5 } }],
    [{ mergedFrom: { ...good.mergedFrom, slot: '' } }],
    [{ mergedFrom: { ...good.mergedFrom, title: 7 } }],
    [{ mergedFrom: { ...good.mergedFrom, createdAt: null } }],
    [{ mergedFrom: { ...good.mergedFrom, createdAt: undefined } }],
    [{ mergedFrom: (({ createdAt: _, ...rest }) => rest)(good.mergedFrom) }],
    [{ mergedFrom: { ...good.mergedFrom, createdAt: 'x'.repeat(129) } }],
  ])('refuses %o', meta => {
    expect(mergedFromOf(meta)).toBeNull()
  })
})

describe('mergeBackErrorMessage', () => {
  const coded = (code: string) => ({ body: JSON.stringify({ error: 'x', code }) })

  it('words each refusal in the user language', () => {
    expect(mergeBackErrorMessage(coded('fork_running'))).toBe('This fork is still running a turn. Merge when it finishes.')
    expect(mergeBackErrorMessage(coded('deferred_note_too_large'))).toBe(mergeBackErrorMessage(coded('summary_too_long')))
  })

  it.each([
    ['parent_not_open', 'utils.mergeBackError.parent_not_open'],
    ['parent_deleted', 'utils.mergeBackError.parent_deleted'],
    ['parent_unconfirmed', 'utils.mergeBackError.parent_unconfirmed'],
    ['parent_not_dashboard', 'utils.mergeBackError.parent_not_dashboard'],
    ['nothing_to_merge', 'utils.mergeBackError.nothing_to_merge'],
    ['fork_running', 'utils.mergeBackError.fork_running'],
    ['merge_back_restricted', 'utils.mergeBackError.restricted'],
    ['merge_draft_in_flight', 'utils.mergeBackError.draft_in_flight'],
    ['merge_summary_failed', 'utils.mergeBackError.summary_failed'],
    ['merge_point_missing', 'utils.mergeBackError.merge_point_missing'],
    ['merge_draft_stale', 'utils.mergeBackError.merge_point_missing'],
    ['already_merged', 'utils.mergeBackError.already_merged'],
    ['merge_queue_full', 'utils.mergeBackError.queue_full'],
    ['deferred_notes_full', 'utils.mergeBackError.parent_holds_too_many'],
    ['summary_too_long', 'utils.mergeBackError.summary_too_long'],
    ['deferred_note_too_large', 'utils.mergeBackError.summary_too_long'],
  ])('words %s with its own line', (code, key) => {
    const message = mergeBackErrorMessage(coded(code))
    expect(message).toBe(i18nT(key))
    expect(message).not.toBe(key)
    expect(message).not.toBe(i18nT('utils.mergeBackError.failed'))
  })

  it('falls back to one generic line', () => {
    expect(mergeBackErrorMessage(new Error('socket hang up'))).toBe('The merge did not go through. Try again.')
    expect(mergeBackErrorMessage(coded('something_new'))).toBe('The merge did not go through. Try again.')
  })

  // The refusal states the rule in the person's words and names the parent's
  // kind where the fork's `forked_from` says it; generic where it does not.
  const RULE = 'A merge can only go into a chat that runs in the dashboard.'
  it.each([
    ['slack:1700000000.000001', `${RULE} This chat's parent is a Slack chat.`],
    ['dashboard:discord_1700000000', `${RULE} This chat's parent is a Discord chat.`],
    ['unified:owner', `${RULE} This chat's parent is a direct-message chat.`],
    ['cron:daily', `${RULE} This chat's parent is a scheduled job.`],
    ['hook:review-42', `${RULE} This chat's parent runs outside it.`],
    // The gateway knows a channel-born parent the key does not say, so the
    // refusal can arrive for a key that reads as a dashboard chat here.
    ['dashboard:parent', `${RULE} This chat's parent runs outside it.`],
    ['', `${RULE} This chat's parent runs outside it.`],
  ])('names the parent of %s when it refuses a parent outside the dashboard', (parentSession, message) => {
    expect(mergeBackErrorMessage(coded('parent_not_dashboard'), parentSession)).toBe(message)
  })
})
