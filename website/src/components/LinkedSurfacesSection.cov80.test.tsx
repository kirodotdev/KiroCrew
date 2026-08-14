import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import LinkedSurfacesSection from './LinkedSurfacesSection'
import { addSlotOptimistic } from '../store/dashboardSlice'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import type { ChatSlot, ConfiguredChannelTarget, SessionLink } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      channelTargets: vi.fn(),
      slackLink: vi.fn(),
      unlinkSlack: vi.fn(),
      linkMirror: vi.fn(),
      remindMirror: vi.fn(),
      unlinkMirror: vi.fn(),
    },
  }
})

/**
 * happy-dom cannot drive a real Radix menu open (no PointerEvent), so both
 * menu families collapse to plain buttons. `onSelect` gets a cancelable Event
 * so the unavailable-target branch can really call `preventDefault()`.
 */
function stubItem(prefix: string) {
  const Item = ({ children, onSelect, ...rest }: {
    children?: React.ReactNode
    onSelect?: (e: Event) => void
    'aria-disabled'?: boolean
    className?: string
  }) => (
    <button
      type="button"
      aria-disabled={rest['aria-disabled']}
      className={rest.className}
      onClick={() => onSelect?.(new Event('select', { cancelable: true }))}
    >
      {children}
    </button>
  )
  return { [`${prefix}Item`]: Item }
}

vi.mock('./ui/dropdown-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...stubItem('DropdownMenu'),
}))
vi.mock('./ui/context-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...stubItem('ContextMenu'),
}))

const channelTargets = vi.mocked(api.channelTargets)
const slackLink = vi.mocked(api.slackLink)
const unlinkSlack = vi.mocked(api.unlinkSlack)
const linkMirror = vi.mocked(api.linkMirror)
const remindMirror = vi.mocked(api.remindMirror)
const unlinkMirror = vi.mocked(api.unlinkMirror)

const SLOT = 'zzq-slot'
const L = (k: string, vars?: Record<string, unknown>) =>
  i18nT(`components.linkedSurfacesSection.${k}`, vars)

function link(over: Partial<SessionLink> = {}): SessionLink {
  return { channel: 'discord', label: 'zzq-guild', target: 't-1', direction: 'out', live: true, ...over }
}

function target(over: Partial<ConfiguredChannelTarget> = {}): ConfiguredChannelTarget {
  return {
    channel_type: 'discord',
    target_id: 'zzq-target',
    label: 'zzq-target-label',
    available: true,
    unavailable_reason: '',
    ...over,
  }
}

function mount(slot: Partial<ChatSlot> = {}, variant: 'dropdown' | 'context' = 'dropdown') {
  const store = createTestStore()
  store.dispatch(addSlotOptimistic({
    key: SLOT, messages: 0, running: false, ...slot,
  } as ChatSlot))
  const view = renderWithProviders(
    <LinkedSurfacesSection slotKey={SLOT} variant={variant} />,
    { store },
  )
  return { store, ...view }
}

const notifications = (store: ReturnType<typeof createTestStore>) =>
  store.getState().notifications.items.map(n => `${n.kind}:${n.title}`)

const slotOf = (store: ReturnType<typeof createTestStore>) =>
  store.getState().dashboard.slots.find(s => s.key === SLOT)!

describe('LinkedSurfacesSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    channelTargets.mockResolvedValue([] as never)
    slackLink.mockResolvedValue({ ok: true, channel: 'C-zzq', thread_ts: '1.2' } as never)
    unlinkSlack.mockResolvedValue({ ok: true } as never)
    linkMirror.mockResolvedValue({ ok: true, conversation_id: 'conv-zzq' } as never)
    remindMirror.mockResolvedValue({ ok: true } as never)
    unlinkMirror.mockResolvedValue({ ok: true } as never)
  })

  describe('ConnectedBadge', () => {
    it('labels an origin link Origin and offers no actions', async () => {
      mount({ links: [link({ direction: 'origin' })] })
      expect(await screen.findByRole('status')).toHaveTextContent(
        L('connected', { label: 'zzq-guild' }),
      )
      expect(screen.getByRole('status')).toHaveTextContent(L('origin'))
      expect(screen.queryByRole('button')).not.toBeInTheDocument()
    })

    it('labels a two-way link Two-way', async () => {
      mount({ links: [link({ direction: 'both' })] })
      expect(await screen.findByRole('status')).toHaveTextContent(L('two_way'))
    })

    it('appends Offline to a dead mirror and hides the reminder action', async () => {
      mount({ links: [link({ live: false })] })
      const badge = await screen.findByRole('status')
      expect(badge).toHaveTextContent(`${L('mirror')} · ${L('offline')}`)
      expect(screen.queryByText(L('post_reminder', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      // The release action survives, so a dead link can still be let go.
      expect(screen.getByText(L('stop_mirroring', { label: 'zzq-guild' }))).toBeInTheDocument()
    })

    it('an offline origin link keeps its role and is never marked Offline', async () => {
      mount({ links: [link({ direction: 'origin', live: false })] })
      expect(await screen.findByRole('status')).toHaveTextContent(L('origin'))
      expect(screen.getByRole('status')).not.toHaveTextContent(L('offline'))
    })
  })

  describe('legacy slack_linked slot', () => {
    it('synthesises a Slack link row with both Slack actions', async () => {
      mount({ slack_linked: true })
      expect(await screen.findByRole('status')).toHaveTextContent(
        L('connected', { label: 'Slack' }),
      )
      expect(screen.getByText(i18nT('components.slackLinkSection.post_reminder_in_slack')))
        .toBeInTheDocument()
    })

    it('posting a Slack reminder sends no explicit channel', async () => {
      mount({ slack_linked: true })
      fireEvent.click(
        await screen.findByText(i18nT('components.slackLinkSection.post_reminder_in_slack')),
      )
      await waitFor(() => expect(slackLink).toHaveBeenCalledWith(SLOT, undefined))
    })

    it('a successful re-link stores the returned channel and thread', async () => {
      const { store } = mount({ slack_linked: true })
      fireEvent.click(
        await screen.findByText(i18nT('components.slackLinkSection.post_reminder_in_slack')),
      )
      await waitFor(() => expect(slotOf(store).slack_channel).toBe('C-zzq'))
      expect(slotOf(store).slack_thread_ts).toBe('1.2')
    })

    it('a not-ok Slack response leaves the slot untouched', async () => {
      slackLink.mockResolvedValue({ ok: false } as never)
      const { store } = mount({ slack_linked: true })
      fireEvent.click(
        await screen.findByText(i18nT('components.slackLinkSection.post_reminder_in_slack')),
      )
      await waitFor(() => expect(slackLink).toHaveBeenCalled())
      expect(slotOf(store).slack_channel).toBeUndefined()
    })

    it('unlinking clears the Slack fields and drops the link', async () => {
      const { store } = mount({ slack_linked: true, links: [link({ channel: 'slack' })] })
      fireEvent.click(
        await screen.findByText(i18nT('components.slackLinkSection.unlink_from_slack')),
      )
      await waitFor(() => expect(slotOf(store).slack_linked).toBe(false))
      expect(slotOf(store).links).toEqual([])
      expect(slotOf(store).slack_channel).toBeUndefined()
    })

    it('a failed unlink warns and keeps the session linked', async () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
      unlinkSlack.mockRejectedValue(new Error('zzq-unlink-failed'))
      const { store } = mount({ slack_linked: true })
      fireEvent.click(
        await screen.findByText(i18nT('components.slackLinkSection.unlink_from_slack')),
      )
      await waitFor(() => expect(warn).toHaveBeenCalledWith(
        'unlinkSlack failed; session stays linked', expect.any(Error),
      ))
      expect(slotOf(store).slack_linked).toBe(true)
      warn.mockRestore()
    })

    it('a failed link warns without clearing anything', async () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
      slackLink.mockRejectedValue(new Error('zzq-link-failed'))
      mount({ slack_linked: true })
      fireEvent.click(
        await screen.findByText(i18nT('components.slackLinkSection.post_reminder_in_slack')),
      )
      await waitFor(() => expect(warn).toHaveBeenCalledWith(
        'slackLink failed', expect.any(Error),
      ))
      warn.mockRestore()
    })
  })

  describe('mirror reminder', () => {
    it('a delivered reminder is reported in the notification feed', async () => {
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('post_reminder', { label: 'zzq-guild' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `success:${L('reminder_sent', { label: 'zzq-guild' })}`,
      ]))
      expect(remindMirror).toHaveBeenCalledWith(SLOT)
    })

    it("a rejected reminder surfaces the backend's own reason", async () => {
      remindMirror.mockRejectedValue(new Error('zzq-503-not-live'))
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('post_reminder', { label: 'zzq-guild' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('reminder_failed', { reason: 'zzq-503-not-live' })}`,
      ]))
    })

    it('a non-Error rejection falls back to the generic reason', async () => {
      remindMirror.mockRejectedValue('zzq-not-an-error')
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('post_reminder', { label: 'zzq-guild' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('reminder_failed', { reason: 'unknown error' })}`,
      ]))
    })
  })

  describe('stopping a mirror', () => {
    it('a declined confirm leaves the link in place', async () => {
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('stop_mirroring', { label: 'zzq-guild' })))
      expect(confirm).toHaveBeenCalledWith(L('confirm_stop_mirroring', { label: 'zzq-guild' }))
      expect(unlinkMirror).not.toHaveBeenCalled()
      expect(slotOf(store).links).toHaveLength(1)
      confirm.mockRestore()
    })

    it('a one-way mirror is stopped, not released', async () => {
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('stop_mirroring', { label: 'zzq-guild' })))
      await waitFor(() => expect(slotOf(store).links).toEqual([]))
      expect(notifications(store)).toEqual([
        `success:${L('mirror_stopped', { label: 'zzq-guild' })}`,
      ])
      confirm.mockRestore()
    })

    it('a two-way binding is released, with the release confirm and message', async () => {
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
      const { store } = mount({ links: [link({ direction: 'both' })] })
      fireEvent.click(await screen.findByText(L('release', { label: 'zzq-guild' })))
      expect(confirm).toHaveBeenCalledWith(L('confirm_release', { label: 'zzq-guild' }))
      await waitFor(() => expect(notifications(store)).toEqual([
        `success:${L('released', { label: 'zzq-guild' })}`,
      ]))
      confirm.mockRestore()
    })

    it('a failed stop is reported and the link stays', async () => {
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
      unlinkMirror.mockRejectedValue(new Error('zzq-stop-failed'))
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('stop_mirroring', { label: 'zzq-guild' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('stop_failed', { reason: 'zzq-stop-failed' })}`,
      ]))
      expect(slotOf(store).links).toHaveLength(1)
      confirm.mockRestore()
    })

    it('a non-Error stop failure falls back to the generic reason', async () => {
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
      unlinkMirror.mockRejectedValue('zzq-not-an-error')
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('stop_mirroring', { label: 'zzq-guild' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('stop_failed', { reason: 'unknown error' })}`,
      ]))
      confirm.mockRestore()
    })
  })

  describe('configured-target picker', () => {
    it('renders nothing while the target list has not resolved', async () => {
      let release: (v: unknown) => void = () => {}
      channelTargets.mockReturnValue(new Promise(r => { release = r }) as never)
      const { container } = mount()
      await waitFor(() => expect(channelTargets).toHaveBeenCalled())
      expect(container.textContent).toBe('')
      release([])
    })

    it('a non-array payload degrades to an empty picker instead of throwing', async () => {
      channelTargets.mockResolvedValue({ oops: true } as never)
      const { container } = mount()
      await waitFor(() => expect(channelTargets).toHaveBeenCalled())
      expect(container.querySelectorAll('button')).toHaveLength(0)
    })

    it('links an available target and keeps any origin link', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText('zzq-target-label'))
      await waitFor(() => expect(linkMirror).toHaveBeenCalledWith(SLOT, 'discord', 'zzq-target'))
      await waitFor(() => expect(slotOf(store).links).toEqual([{
        channel: 'discord', label: 'zzq-target-label', target: 'conv-zzq',
        direction: 'out', live: true,
      }]))
    })

    it('falls back to the configured target id when no conversation is returned', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      linkMirror.mockResolvedValue({ ok: true } as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText('zzq-target-label'))
      await waitFor(() => expect(slotOf(store).links?.[0].target).toBe('zzq-target'))
    })

    it('a not-ok link response writes no link', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      linkMirror.mockResolvedValue({ ok: false } as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText('zzq-target-label'))
      await waitFor(() => expect(linkMirror).toHaveBeenCalled())
      expect(slotOf(store).links).toBeUndefined()
    })

    it('a failed link is reported in the notification feed', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      linkMirror.mockRejectedValue(new Error('zzq-link-refused'))
      const { store } = mount()
      fireEvent.click(await screen.findByText('zzq-target-label'))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('link_failed', { reason: 'zzq-link-refused' })}`,
      ]))
    })

    it('a non-Error link failure falls back to the generic reason', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      linkMirror.mockRejectedValue('zzq-not-an-error')
      const { store } = mount()
      fireEvent.click(await screen.findByText('zzq-target-label'))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('link_failed', { reason: 'unknown error' })}`,
      ]))
    })

    it('a slack target routes through the slack-link path, not the mirror path', async () => {
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'dm', label: 'zzq-unused' }),
      ] as never)
      mount()
      fireEvent.click(
        await screen.findByText(i18nT('components.slackLinkSection.send_to_slack')),
      )
      await waitFor(() => expect(slackLink).toHaveBeenCalledWith(SLOT, 'dm'))
      expect(linkMirror).not.toHaveBeenCalled()
    })

    it('a non-dm slack target keeps its own label', async () => {
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'C-zzq', label: 'zzq-chan' }),
      ] as never)
      mount()
      fireEvent.click(await screen.findByText('zzq-chan'))
      await waitFor(() => expect(slackLink).toHaveBeenCalledWith(SLOT, 'C-zzq'))
    })

    it('an unavailable target explains itself and refuses the click', async () => {
      channelTargets.mockResolvedValue([
        target({ available: false, unavailable_reason: 'zzq-transport-absent' }),
      ] as never)
      const { store } = mount()
      const row = await screen.findByText('zzq-target-label')
      expect(screen.getByText('zzq-transport-absent')).toBeInTheDocument()
      expect(row.closest('button')).toHaveAttribute('aria-disabled', 'true')
      fireEvent.click(row)
      await waitFor(() => expect(notifications(store)).toEqual(['error:zzq-transport-absent']))
      expect(linkMirror).not.toHaveBeenCalled()
    })

    it('an unavailable target with no reason uses the generic explanation', async () => {
      channelTargets.mockResolvedValue([target({ available: false })] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText('zzq-target-label'))
      await waitFor(() => expect(notifications(store)).toEqual([`error:${L('unavailable')}`]))
    })

    it('the picker is hidden once a mirror exists', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      mount({ links: [link()] })
      await waitFor(() => expect(channelTargets).toHaveBeenCalled())
      expect(screen.queryByText('zzq-target-label')).not.toBeInTheDocument()
    })
  })

  it('behaves identically inside the context-menu family', async () => {
    channelTargets.mockResolvedValue([target()] as never)
    mount({}, 'context')
    fireEvent.click(await screen.findByText('zzq-target-label'))
    await waitFor(() => expect(linkMirror).toHaveBeenCalledWith(SLOT, 'discord', 'zzq-target'))
  })

  it('renders nothing at all for an unknown slot key', async () => {
    channelTargets.mockResolvedValue([] as never)
    const { container } = renderWithProviders(
      <LinkedSurfacesSection slotKey="zzq-missing" variant="dropdown" />,
    )
    await waitFor(() => expect(channelTargets).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
