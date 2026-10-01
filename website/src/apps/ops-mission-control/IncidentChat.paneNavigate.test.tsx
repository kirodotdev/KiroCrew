/**
 * IncidentChat's full-document navigation stays inside the pane's capability
 * prefix when the dashboard runs as a relayed pane.
 *
 * The Chromium proof computed this URL with a bare helper instead of driving the
 * component, so it could not catch a regression at the call site. This mounts the
 * real IncidentChat, resolves the runtime to a relayed pane, and invokes the real
 * SDK navigate the embed is handed. Dropping `dashboardNavigateUrl` from the
 * navigateFn makes the '/chat' case escape to the hub root and this test fails;
 * external and protocol-relative targets stay untouched either way.
 *
 * Isolated in its own module so the runtime singleton resolves to the pane and
 * never collides with the direct-mode navigation covered in IncidentChat.cov80.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { useNavigate } from '../../app-sdk'
import { initDashboardRuntime } from '../../lib/dashboardRuntime'

// Fresh module → fresh runtime singleton. Pin it to a relayed pane before the
// component resolves it; navigateFn reads the runtime live at call time.
initDashboardRuntime({ pathname: '/instance-pane/K_cap01/' })

const navigateRef = vi.hoisted(() => ({ current: (_p: string) => {} }))

// The embed is replaced by a probe that captures the real SDK navigate the
// provider publishes (IncidentChat's own navigateFn), so the call site is
// exercised rather than reimplemented.
vi.mock('../../app-sdk/ChatEmbed', () => {
  function ZzqNavProbe() {
    navigateRef.current = useNavigate()
    return null
  }
  return { default: ZzqNavProbe }
})

import IncidentChat from './IncidentChat'

describe('IncidentChat navigation under a relayed pane', () => {
  afterEach(() => vi.restoreAllMocks())

  it('routes a same-dashboard navigate under the capability prefix, leaving external targets alone', () => {
    const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    render(<IncidentChat incidentId="zzq-1" />)

    // Same-dashboard route: relocated under the pane prefix.
    navigateRef.current('/chat')
    expect(assign).toHaveBeenLastCalledWith('/instance-pane/K_cap01/chat')

    // Absolute external target: never rewritten into the pane.
    navigateRef.current('https://example.com/x')
    expect(assign).toHaveBeenLastCalledWith('https://example.com/x')

    // Protocol-relative target: also left untouched (it is not a root path).
    navigateRef.current('//evil.example/x')
    expect(assign).toHaveBeenLastCalledWith('//evil.example/x')
  })
})
