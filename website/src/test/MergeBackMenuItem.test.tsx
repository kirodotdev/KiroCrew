/** "Merge into parent…" appears only on a fork, explains a disabled row inline,
 *  and opens the dialog through the ChatPage-scoped provider. The parent's
 *  card names the fork and opens it. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
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

async function mountItem(slot: ChatSlot, { provider = true, others = [] as ChatSlot[] } = {}) {
  const store = createTestStore()
  store.dispatch(sseSlots([slot, ...others]))
  const item = <><MergeBackMenuItem Item={Item} slotKey={slot.key} /><OpenFork /></>
  const rendered = renderWithProviders(provider ? <MergeBackDialogProvider>{item}</MergeBackDialogProvider> : item, { store })
  await act(async () => {})
  return rendered
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MergeBackMenuItem', () => {
  it('renders nothing on a chat that is not a fork', async () => {
    await mountItem({ key: 'plain', title: 'Plain', messages: 2, running: false })
    expect(screen.queryByTestId('merge-back')).not.toBeInTheDocument()
  })

  it('renders nothing outside the provider', async () => {
    await mountItem(FORK, { provider: false })
    expect(screen.queryByTestId('merge-back')).not.toBeInTheDocument()
  })

  it('opens the dialog for this fork', async () => {
    const user = userEvent.setup()
    await mountItem(FORK)
    await user.click(screen.getByTestId('merge-back'))
    expect(screen.getByTestId('open-fork')).toHaveTextContent('fork')
  })

  it.each([
    [{ running: true }, 'turn running'],
    [{ memory_mode: 'incognito' as const }, 'incognito chat'],
    [{ memory_mode: 'temporary' as const }, 'temporary chat'],
    [{ forked_from: 'slack:1700000000.000001' }, 'parent runs in Slack'],
    [{ forked_from: 'unified:owner' }, 'parent is a direct message'],
    [{ forked_from: 'cron:daily' }, 'parent is a scheduled job'],
    [{ forked_from: 'hook:review-42' }, 'parent runs outside the dashboard'],
  ])('is disabled with its reason for %o', async (over, reason) => {
    await mountItem({ ...FORK, ...over })
    const row = screen.getByTestId('merge-back')
    expect(row).toBeDisabled()
    expect(row).toHaveTextContent(reason)
  })

  it('is enabled for a dashboard parent', async () => {
    await mountItem(FORK)
    expect(screen.getByTestId('merge-back')).toBeEnabled()
  })

  it('is enabled for a dashboard parent whose key only resembles a scheduled job', async () => {
    await mountItem({ ...FORK, forked_from: 'dashboard:cron_daily' })
    expect(screen.getByTestId('merge-back')).toBeEnabled()
  })

  it('names the parent, not the running turn, on a running fork of a Slack chat', async () => {
    await mountItem({ ...FORK, running: true, forked_from: 'slack:1700000000.000001' })
    expect(screen.getByTestId('merge-back')).toHaveTextContent('parent runs in Slack')
  })

  it('is enabled for a dashboard parent whose key only resembles a channel stem', async () => {
    await mountItem({ ...FORK, forked_from: 'dashboard:Slack_thread_triage' })
    expect(screen.getByTestId('merge-back')).toBeEnabled()
  })

  it.each(['dashboard:slack_notes', 'dashboard:slack_1700000000.000001', 'dashboard:discord_1700000000'])(
    'leaves a dashboard parent named %s to the gateway when its row records no origin',
    async forkedFrom => {
      await mountItem({ ...FORK, forked_from: forkedFrom }, {
        others: [{ key: forkedFrom.slice('dashboard:'.length), title: 'Parent', messages: 2, running: false }],
      })
      expect(screen.getByTestId('merge-back')).toBeEnabled()
    },
  )

  it.each([
    ['slack', 'parent runs in Slack'],
    ['discord', 'parent runs in Discord'],
    ['unified', 'parent is a direct message'],
  ])('names a dashboard parent born in %s by its recorded origin', async (channel, reason) => {
    await mountItem({ ...FORK, forked_from: 'dashboard:born' }, {
      others: [{
        key: 'born', title: 'Parent', messages: 2, running: false,
        links: [{ channel, label: channel, target: 'C1/1700000000.000001', direction: 'origin' }],
      }],
    })
    const row = screen.getByTestId('merge-back')
    expect(row).toBeDisabled()
    expect(row).toHaveTextContent(reason)
  })

  it('does not take an outbound mirror for where the parent was born', async () => {
    await mountItem({ ...FORK, forked_from: 'dashboard:mirrored' }, {
      others: [{
        key: 'mirrored', title: 'Parent', messages: 2, running: false,
        links: [{ channel: 'slack', label: 'Slack', target: 'C1/1700000000.000001', direction: 'out' }],
      }],
    })
    expect(screen.getByTestId('merge-back')).toBeEnabled()
  })
})

describe('MergedFromLabel', () => {
  const block = { session: 'dashboard:fork', slot: 'fork', title: '↳ Fork of A', createdAt: '2026-10-02T18:00:00+00:00', through: 'm', messages: 2 }

  it('names the fork and opens it', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    render(<MergedFromLabel block={block} onOpen={onOpen} />)
    const opener = screen.getByTestId('merged-from-open')
    expect(screen.getByTestId('merged-from-label')).toHaveTextContent('Merged from ↳ Fork of A')
    expect(screen.getByTestId('merged-from-label')).toHaveTextContent('2 messages')
    expect(opener.tagName).toBe('BUTTON')
    expect(opener).toHaveAttribute('type', 'button')
    expect(opener).toHaveTextContent('↳ Fork of A')
    expect(opener).toHaveClass('text-accent', 'font-medium')
    expect(opener).not.toHaveClass('px-2.5', 'py-1', 'text-[13px]', 'hover:bg-bg-hover')
    await user.click(opener)
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it.each(['<br/>', '<fork>x</fork>', '<0>x</0>', '{{title}}'])('shows a fork titled %s as literal text', title => {
    render(<MergedFromLabel block={{ ...block, title }} onOpen={vi.fn()} />)
    const opener = screen.getByTestId('merged-from-open')
    expect(opener.textContent).toBe(title)
    expect(opener.children).toHaveLength(0)
    expect(screen.getByTestId('merged-from-label')).toHaveTextContent(`Merged from ${title}`)
  })

  it('shows a markup title as literal text where there is no way to open a session', () => {
    render(<MergedFromLabel block={{ ...block, title: '<br/>' }} />)
    expect(screen.getByTestId('merged-from-label')).toHaveTextContent('Merged from <br/>')
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

  it('reads a card written before cards recorded an identity as an empty one', () => {
    const { createdAt: _, ...legacy } = good.mergedFrom
    expect(mergedFromOf({ mergedFrom: legacy })).toEqual({ ...good.mergedFrom, createdAt: '' })
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

  it('tells the person to remove credential-shaped text, not to try again', () => {
    const message = mergeBackErrorMessage(coded('context_secret_shaped'))
    expect(message).toBe(i18nT('utils.mergeBackError.secret_shaped'))
    expect(message).toMatch(/password or access token/)
    expect(message).not.toBe(i18nT('utils.mergeBackError.failed'))
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

  const RULE = 'A merge can only go into a chat that runs in the dashboard.'
  it.each([
    ['slack:1700000000.000001', `${RULE} This chat's parent is a Slack chat.`],
    ['dashboard:discord_1700000000', `${RULE} This chat's parent is a Discord chat.`],
    ['unified:owner', `${RULE} This chat's parent is a direct-message chat.`],
    ['cron:daily', `${RULE} This chat's parent is a scheduled job.`],
    ['hook:review-42', `${RULE} This chat's parent runs outside it.`],
    ['dashboard:parent', `${RULE} This chat's parent runs outside it.`],
    ['', `${RULE} This chat's parent runs outside it.`],
  ])('names the parent of %s when it refuses a parent outside the dashboard', (parentSession, message) => {
    expect(mergeBackErrorMessage(coded('parent_not_dashboard'), parentSession)).toBe(message)
  })
})
