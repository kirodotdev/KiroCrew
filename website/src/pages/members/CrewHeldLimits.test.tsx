import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { MemberRosterRow } from '../../api/client'
import type { CronJob } from '../../types'

const memberHeldLimits = vi.fn()
const memberHeldLimitsRelease = vi.fn()

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberHeldLimits: (...a: unknown[]) => memberHeldLimits(...a),
      memberHeldLimitsRelease: (...a: unknown[]) => memberHeldLimitsRelease(...a),
    },
  }
})

import CrewHeldLimits from './CrewHeldLimits'

const member = { name: 'helper', slug: 'helper-id', display_name: 'Helper' } as unknown as MemberRosterRow
const SHELL = { text: 'deny shell commands', capability: 'shell' }
const HOOK = { text: 'the agentSpawn hook', capability: 'none' }
const KEY = ['member-held-limits', 'helper-id', 'helper']
const refusal = (status: number) => Object.assign(new Error('refused'), { status })
const job = (id: string, name: string, enabled: boolean, approval_mode?: string) =>
  ({ id, name, enabled, approval_mode }) as unknown as CronJob
/** `a` sits before `b` in document order. */
const before = (a: Element, b: Element) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0

beforeEach(() => {
  memberHeldLimits.mockReset()
  memberHeldLimitsRelease.mockReset()
})

describe('CrewHeldLimits (the "Asks you first" section)', () => {
  it('renders nothing while no held limit makes the bot ask', async () => {
    memberHeldLimits.mockResolvedValue({ limits: [HOOK], kiro_cli: true })
    const { container, queryClient } = renderWithProviders(<CrewHeldLimits member={member} schedules={[]} schedulesLoading={false} />)
    await waitFor(() => expect(queryClient.getQueryState(KEY)?.status).toBe('success'))
    expect(memberHeldLimits).toHaveBeenCalledWith('helper-id', 'helper')
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for a caller the owner gate refuses', async () => {
    memberHeldLimits.mockRejectedValue(refusal(403))
    const { container, queryClient } = renderWithProviders(<CrewHeldLimits member={member} schedules={[]} schedulesLoading={false} />)
    await waitFor(() => expect(queryClient.getQueryState(KEY)?.status).toBe('error'))
    expect(container).toBeEmptyDOMElement()
  })

  it('says a read that failed otherwise', async () => {
    memberHeldLimits.mockRejectedValue(refusal(500))
    renderWithProviders(<CrewHeldLimits member={member} schedules={[]} schedulesLoading={false} />)
    expect(await screen.findByTestId('crew-held-limits-error')).toHaveTextContent("Couldn't load what this Bot asks you first.")
  })

  it('lists every limit that makes the bot ask with what it covers, under its heading', async () => {
    memberHeldLimits.mockResolvedValue({
      limits: [SHELL, HOOK, { text: 'the use_aws hook', capability: 'tool:use_aws' }, { text: 'an odd one', capability: 'something-new' }],
      kiro_cli: false,
    })
    renderWithProviders(<CrewHeldLimits member={member} schedules={[]} schedulesLoading={false} />)
    const section = await screen.findByTestId('crew-held-limits')
    expect(section).toHaveTextContent('Asks you first')
    const rows = within(section).getAllByTestId('crew-held-limit').map(r => r.textContent)
    expect(rows).toEqual([
      'Asks before running commandsInstead of your old setting: “deny shell commands”',
      'Asks before using the use_aws toolInstead of your old setting: “the use_aws hook”',
      'Asks before every actionInstead of your old setting: “an odd one”',
    ])
    expect(within(section).queryByTestId('crew-held-limits-kiro-cli')).toBeNull()
  })

  it.each([
    ['fs_read', true, true],
    ['*', true, true],
    ['shell', true, false],
    ['fs_read', false, false],
  ])('names Kiro CLI\'s own-folder reads for a %s limit only on Kiro CLI (kiro_cli=%s)', async (capability, kiroCli, shown) => {
    memberHeldLimits.mockResolvedValue({ limits: [{ text: 'a limit', capability }], kiro_cli: kiroCli })
    renderWithProviders(<CrewHeldLimits member={member} schedules={[]} schedulesLoading={false} />)
    await screen.findByTestId('crew-held-limits')
    expect(screen.queryByTestId('crew-held-limits-kiro-cli') !== null).toBe(shown)
  })

  it('confirms naming every limit that makes the bot ask, then posts every held limit', async () => {
    const limits = [SHELL, HOOK]
    memberHeldLimits.mockResolvedValueOnce({ limits, kiro_cli: false }).mockResolvedValue({ limits: [], kiro_cli: false })
    memberHeldLimitsRelease.mockResolvedValue({ released: limits })
    renderWithProviders(<CrewHeldLimits member={member} schedules={[]} schedulesLoading={false} />)
    fireEvent.click(await screen.findByTestId('crew-held-limits-release'))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Stop asking for these limits?')
    const body = within(dialog).getByText(
      'After you confirm, nothing is left of your old settings below: the approval settings of “Helper” decide these actions, so a setting or a schedule that approves on its own may run them without asking you.',
    )
    const modeLine = within(dialog).getByTestId('crew-held-limits-confirm-mode')
    expect(modeLine.textContent).toBe(
      'After this: Helper runs tools without asking you first, in every chat with it.',
    )
    const limitsList = within(dialog).getByTestId('crew-held-limits-confirm-list')
    expect(before(body, limitsList)).toBe(true)
    expect(before(limitsList, modeLine)).toBe(true)
    expect(within(dialog).queryByTestId('crew-held-limits-confirm-schedules')).toBeNull()
    const items = within(dialog).getAllByRole('listitem')
    expect(items.map(li => li.textContent)).toEqual([
      `Asks before running commandsInstead of your old setting: “${SHELL.text}”`,
    ])
    expect(items.map(li => Array.from(li.children, line => line.textContent))).toEqual([
      ['Asks before running commands', `Instead of your old setting: “${SHELL.text}”`],
    ])
    fireEvent.click(within(dialog).getByRole('button', { name: 'Stop asking' }))

    await waitFor(() => expect(memberHeldLimitsRelease).toHaveBeenCalledWith('helper-id', 'helper', limits))
    const released = await screen.findByTestId('crew-held-limits-released')
    expect(released).toHaveTextContent('“Helper” no longer asks you first because of its old settings. Its approval settings decide from now on.')
    expect(released).toHaveAttribute('role', 'status')
    expect(screen.queryByTestId('crew-held-limits')).toBeNull()
  })

  it('keeps the section, not the notice, when the refetch after a release still holds a limit', async () => {
    memberHeldLimits.mockResolvedValue({ limits: [SHELL], kiro_cli: false })
    let resolveRelease!: (v: unknown) => void
    memberHeldLimitsRelease.mockReturnValue(new Promise(r => { resolveRelease = r }))
    renderWithProviders(<CrewHeldLimits member={member} schedules={[]} schedulesLoading={false} />)
    fireEvent.click(await screen.findByTestId('crew-held-limits-release'))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Stop asking' }))

    await waitFor(() => expect(screen.getByTestId('crew-held-limits-release')).toBeDisabled())
    resolveRelease({ released: [SHELL] })
    await waitFor(() => expect(screen.getByTestId('crew-held-limits-release')).toBeEnabled())
    expect(memberHeldLimits).toHaveBeenCalledTimes(2)
    expect(within(screen.getByTestId('crew-held-limits')).getAllByTestId('crew-held-limit').map(r => r.textContent)).toEqual([
      `Asks before running commandsInstead of your old setting: “${SHELL.text}”`,
    ])
    expect(screen.queryByTestId('crew-held-limits-released')).toBeNull()
  })

  it('names each enabled schedule that approves on its own, and no other', async () => {
    memberHeldLimits.mockResolvedValue({ limits: [SHELL], kiro_cli: false })
    const schedules = [
      job('a', 'nightly report', true, 'auto'),
      job('b', 'weekly digest', true),
      job('c', 'paused sweep', false, 'auto'),
    ]
    renderWithProviders(<CrewHeldLimits member={member} schedules={schedules} schedulesLoading={false} />)
    fireEvent.click(await screen.findByTestId('crew-held-limits-release'))

    const dialog = await screen.findByRole('dialog')
    const sentence = '1 schedule approves on its own and will run them without asking you.'
    expect(within(dialog).getByText(sentence)).toBeInTheDocument()
    const schedulesList = within(dialog).getByTestId('crew-held-limits-confirm-schedules')
    expect(schedulesList).toHaveAccessibleName(sentence)
    expect(schedulesList).not.toBe(within(dialog).getByTestId('crew-held-limits-confirm-list'))
    expect(before(within(dialog).getByTestId('crew-held-limits-confirm-mode'), schedulesList)).toBe(true)
    const listed = within(schedulesList).getAllByRole('listitem')
    expect(listed.map(li => li.textContent)).toEqual(['nightly report'])
  })

  it.each([
    [{ schedulesLoading: true }, false],
    [{ schedulesLoading: false, schedulesError: true }, true],
  ])('keeps the release shut until the schedules are read, and says when they could not be (%o)', async (flags, failed) => {
    memberHeldLimits.mockResolvedValue({ limits: [SHELL], kiro_cli: false })
    renderWithProviders(<CrewHeldLimits member={member} schedules={[]} {...flags} />)
    expect(await screen.findByTestId('crew-held-limits-release')).toBeDisabled()
    const notice = screen.queryByTestId('crew-held-limits-schedules-error')
    if (failed) expect(notice).toHaveTextContent('Could not load this bot’s schedules.')
    else expect(notice).toBeNull()
  })

  it('says the limits changed when the release is refused with 409', async () => {
    memberHeldLimits.mockResolvedValue({ limits: [SHELL], kiro_cli: false })
    memberHeldLimitsRelease.mockRejectedValue(refusal(409))
    renderWithProviders(<CrewHeldLimits member={member} schedules={[]} schedulesLoading={false} />)
    fireEvent.click(await screen.findByTestId('crew-held-limits-release'))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Stop asking' }))

    expect(await screen.findByTestId('crew-held-limits-release-error')).toHaveTextContent(
      'These limits changed. Check the list and try again.',
    )
    expect(memberHeldLimits).toHaveBeenCalledTimes(2)
  })
})
