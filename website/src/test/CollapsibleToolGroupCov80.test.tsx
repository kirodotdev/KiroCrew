/**
 * CollapsibleToolGroup — the approval and disclosure behaviour the existing
 * purpose-preview suite does not reach.
 *
 * Three things live here: the header label state machine (running → approval
 * needed → resolved, with a count when several approvals are queued), the
 * optimistic approval dispatch (resolve locally at once, roll back if the
 * round-trip fails), and the disclosure rules — expand on click, auto-collapse
 * when a run finishes, but never against a user's own toggle.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import { i18nT } from '../i18n/t'

const T = (k: string, vars?: Record<string, unknown>) => i18nT(`pages.chat.collapsibleToolGroup.${k}`, vars)

/** The one header button, whatever label it is currently wearing. */
const header = () => screen.getAllByRole('button')[0]

afterEach(() => vi.restoreAllMocks())

describe('CollapsibleToolGroup header label', () => {
  it('counts the tool calls when idle', () => {
    renderWithProviders(<CollapsibleToolGroup count={3}><div>zzq-child</div></CollapsibleToolGroup>)
    expect(screen.getByText(T('tool_call', { count: 3 }))).toBeInTheDocument()
  })

  it('says running while tools are in flight', () => {
    renderWithProviders(<CollapsibleToolGroup count={1} isRunning><div>zzq-child</div></CollapsibleToolGroup>)
    expect(screen.getByText(T('running_tools'))).toBeInTheDocument()
    expect(screen.getByLabelText(T('running'))).toBeInTheDocument()
  })

  it('asks for approval, and reports the queue depth when more than one is pending', () => {
    const { unmount } = renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission onApprove={() => {}}><div>zzq-child</div></CollapsibleToolGroup>,
    )
    expect(screen.getAllByLabelText(T('approval_needed')).length).toBeGreaterThan(0)
    unmount()

    renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission pendingPermCount={3} onApprove={() => {}}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    // The count and the phrase are separate text nodes in one span, so assert
    // against the header's own text rather than a single text node.
    expect(header().textContent).toContain(`3 ${T('approvals_pending')}`)
  })
})

describe('CollapsibleToolGroup approval dispatch', () => {
  const cases: [string, string][] = [
    [T('approve'), T('approved')],
    [T('trust'), T('trusted')],
    [T('reject'), T('rejected')],
  ]

  for (const [buttonLabel, resolvedLabel] of cases) {
    it(`${buttonLabel} reports its decision and shows the resolved label`, async () => {
      const onApprove = vi.fn()
      renderWithProviders(
        <CollapsibleToolGroup count={1} hasPermission permissionMeta={{ tool_input: 'zzq --run' }} onApprove={onApprove}>
          <div>zzq-child</div>
        </CollapsibleToolGroup>,
      )
      fireEvent.click(screen.getByText(buttonLabel))

      await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1))
      expect(onApprove.mock.calls[0][0]).toBe(resolvedLabel.toLowerCase() === 'trusted' ? 'trust' : resolvedLabel.toLowerCase())
      expect(screen.getByText(resolvedLabel)).toBeInTheDocument()
      // The approval affordances are gone once resolved.
      expect(screen.queryByText(T('approve'))).not.toBeInTheDocument()
    })
  }

  it('rolls the optimistic resolution back when the round-trip fails', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    const onApprove = vi.fn().mockImplementation(() => Promise.reject(new Error('zzq gateway down')))
    renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission permissionMeta={{ tool_input: 'zzq --run' }} onApprove={onApprove}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    fireEvent.click(screen.getByText(T('approve')))

    // Back to "approval needed", with the buttons live again.
    await waitFor(() => expect(screen.getByText(T('approve'))).not.toBeDisabled())
    expect(screen.queryByText(T('approved'))).not.toBeInTheDocument()
    expect(err).toHaveBeenCalled()
  })

  it('pretty-prints a structured tool_input in the preview', () => {
    const { container } = renderWithProviders(
      <CollapsibleToolGroup
        count={1}
        hasPermission
        permissionMeta={{ tool_input: { path: '/zzq/file', mode: 'create' } }}
        onApprove={() => {}}
      >
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    const pre = container.querySelector('pre')!.textContent!
    expect(pre).toContain('"path"')
    expect(pre).toContain('\n')
  })

  it('renders no preview when there is no permission meta at all', () => {
    const { container } = renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission onApprove={() => {}}><div>zzq-child</div></CollapsibleToolGroup>,
    )
    expect(container.querySelector('pre')).toBeNull()
  })

  it('truncates a very long preview', () => {
    const { container } = renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission permissionMeta={{ tool_input: 'z'.repeat(400) }} onApprove={() => {}}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    const pre = container.querySelector('pre')!.textContent!
    expect(pre.endsWith('…')).toBe(true)
    expect(pre.length).toBeLessThan(400)
  })
})

describe('CollapsibleToolGroup disclosure', () => {
  it('is collapsed by default and reveals the children on click', () => {
    renderWithProviders(<CollapsibleToolGroup count={1}><div>zzq-child</div></CollapsibleToolGroup>)
    expect(screen.queryByText('zzq-child')).not.toBeInTheDocument()

    fireEvent.click(header())
    expect(screen.getByText('zzq-child')).toBeInTheDocument()
    expect(header()).toHaveAttribute('aria-expanded', 'true')
  })

  it('starts expanded when autoExpand is set', () => {
    renderWithProviders(<CollapsibleToolGroup count={1} autoExpand><div>zzq-child</div></CollapsibleToolGroup>)
    expect(screen.getByText('zzq-child')).toBeInTheDocument()
  })

  it('offers the activity link only while the viewer is closed', () => {
    const onViewActivity = vi.fn()
    const { rerender } = renderWithProviders(
      <CollapsibleToolGroup count={1} autoExpand onViewActivity={onViewActivity}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    fireEvent.click(screen.getByText(T('view_full_activity')))
    expect(onViewActivity).toHaveBeenCalledTimes(1)

    rerender(
      <CollapsibleToolGroup count={1} autoExpand onViewActivity={onViewActivity} activityOpen>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
    )
    expect(screen.queryByText(T('view_full_activity'))).not.toBeInTheDocument()
  })

  it('auto-collapses when the run finishes, unless the user toggled it', () => {
    const { rerender } = renderWithProviders(
      <CollapsibleToolGroup count={1} isRunning autoExpand><div>zzq-child</div></CollapsibleToolGroup>,
    )
    expect(screen.getByText('zzq-child')).toBeInTheDocument()

    rerender(<CollapsibleToolGroup count={1} autoExpand={false}><div>zzq-child</div></CollapsibleToolGroup>)
    expect(screen.queryByText('zzq-child')).not.toBeInTheDocument()
  })

  it('keeps a user-expanded group open after the run finishes', () => {
    const { rerender } = renderWithProviders(
      <CollapsibleToolGroup count={1} isRunning><div>zzq-child</div></CollapsibleToolGroup>,
    )
    fireEvent.click(header())
    expect(screen.getByText('zzq-child')).toBeInTheDocument()

    rerender(<CollapsibleToolGroup count={1}><div>zzq-child</div></CollapsibleToolGroup>)
    expect(screen.getByText('zzq-child')).toBeInTheDocument()
  })
})
