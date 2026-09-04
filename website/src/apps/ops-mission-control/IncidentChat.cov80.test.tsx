/**
 * IncidentChat — the live investigation panel on an ops incident row.
 *
 * The two wiring requirements this component exists to satisfy are exactly what
 * is asserted: the embed has an AppApiProvider ancestor, and that provider's
 * permission scope includes BOTH `/api/chat*` (what the embed polls/posts) and
 * `/api/approvals*` (what a tool card's Approve button calls — omit it and the
 * button fails silently). ChatEmbed itself is replaced by a probe that reads the
 * SDK context, so no polling happens and the contract is observable.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { useAppApi, useAppEvents, useAppInfo, useNavigate, useNotify } from '../../app-sdk'
import { i18nT } from '../../i18n/t'
import IncidentChat, { incidentSlotKey } from './IncidentChat'

const probeProps = vi.hoisted(() => ({ current: {} as Record<string, unknown> }))
const unsubscribes = vi.hoisted(() => ({ current: [] as unknown[] }))

vi.mock('../../app-sdk/ChatEmbed', () => {
  // Named + capitalised so the SDK hooks below are inside a real component as
  // far as react-hooks/rules-of-hooks is concerned.
  function ZzqChatEmbedProbe(props: Record<string, unknown>) {
    probeProps.current = props
    const info = useAppInfo()
    const notify = useNotify()
    const navigate = useNavigate()
    // Proves the provider's subscribeFn returns a usable unsubscribe: the SDK
    // hook calls it on mount and invokes the return value on cleanup.
    useAppEvents('slots', () => unsubscribes.current.push('zzq-event'))
    // Present only so a missing provider would throw here too.
    useAppApi()
    return (
      <div data-testid="zzq-embed">
        <span data-testid="zzq-perms">{info.permissions.api.join(',')}</span>
        <span data-testid="zzq-events">{info.permissions.events.join(',')}</span>
        <span data-testid="zzq-app">{info.name}</span>
        <button type="button" data-testid="zzq-notify" onClick={() => notify('zzq hello')}>
          zzq-notify
        </button>
        <button type="button" data-testid="zzq-nav" onClick={() => navigate('/zzq-elsewhere')}>
          zzq-navigate
        </button>
      </div>
    )
  }
  return { default: ZzqChatEmbedProbe }
})

describe('incidentSlotKey', () => {
  it('namespaces the slot per incident', () => {
    expect(incidentSlotKey('zzq-42')).toBe('ops-mission-control-zzq-42')
  })
})

describe('IncidentChat', () => {
  it('mounts the embed against the incident slot with an ask placeholder', () => {
    render(<IncidentChat incidentId="zzq-42" />)
    expect(screen.getByTestId('zzq-embed')).toBeInTheDocument()
    expect(probeProps.current.slotKey).toBe('ops-mission-control-zzq-42')
    expect(probeProps.current.placeholder).toBe(
      i18nT('apps.opsMissionControl.incidentChat.ask_about_incident', { incidentId: 'zzq-42' }),
    )
  })

  it('scopes the provider to the chat AND approvals APIs', () => {
    render(<IncidentChat incidentId="zzq-42" />)
    const perms = screen.getByTestId('zzq-perms').textContent ?? ''
    expect(perms.split(',')).toEqual(
      expect.arrayContaining([
        '/api/chat',
        '/api/chat/*',
        '/api/approvals',
        '/api/approvals/*',
        '/api/apps/ops-mission-control',
        '/api/apps/ops-mission-control/*',
      ]),
    )
    expect(screen.getByTestId('zzq-events').textContent).toBe('slots,notification')
    expect(screen.getByTestId('zzq-app').textContent).toBe('ops-mission-control')
  })

  it('appends the incident title to the header when one is known', () => {
    render(<IncidentChat incidentId="zzq-42" title="zzq disk pressure" />)
    expect(
      screen.getByText(
        i18nT('apps.opsMissionControl.incidentChat.live_investigation_header', {
          incident: 'zzq-42',
          title: ' — zzq disk pressure',
        }),
      ),
    ).toBeInTheDocument()
  })

  it('renders the header without a title suffix when none is known', () => {
    render(<IncidentChat incidentId="zzq-42" />)
    expect(
      screen.getByText(
        i18nT('apps.opsMissionControl.incidentChat.live_investigation_header', {
          incident: 'zzq-42',
          title: '',
        }),
      ),
    ).toBeInTheDocument()
  })

  it('unmounts cleanly, so subscribeFn returned a real unsubscribe', () => {
    // useAppEvents calls subscribe() on mount and its RETURN VALUE on cleanup;
    // a subscribeFn returning undefined would throw during unmount.
    const { unmount } = render(<IncidentChat incidentId="zzq-42" />)
    expect(() => unmount()).not.toThrow()
  })

  it('navigates the whole window (the board is not inside the SPA router here)', () => {
    render(<IncidentChat incidentId="zzq-42" />)
    fireEvent.click(screen.getByTestId('zzq-nav'))
    expect(window.location.pathname).toBe('/zzq-elsewhere')
  })

  it('notify is a no-op rather than a missing callback', () => {
    render(<IncidentChat incidentId="zzq-42" />)
    expect(() => fireEvent.click(screen.getByTestId('zzq-notify'))).not.toThrow()
  })
})
