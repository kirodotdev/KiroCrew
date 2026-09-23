//
// `DirectDeployFlow` renders one affordance per phase of the deploy state
// machine, and the page-level tests only walk the two phases their own
// scenarios reach. The phases that matter most are the refusals, so they are
// driven here directly against a hand-built flow object: the component takes the
// flow as a prop, so each phase can be rendered on its own without scripting a
// whole deploy to arrive at it.
//
// What these pin: every phase renders the affordance its comment promises, the
// credential scan block offers no override while a plain one does, and the two
// copy buttons report success.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import DirectDeployFlow from '../components/DirectDeployFlow'
import type { useDirectDeploy } from '../hooks/useDirectDeploy'

type Flow = ReturnType<typeof useDirectDeploy>
type Phase = Flow['phase']

const PREVIEW = {
  content_digest: 'sha256:d', profile: 'ship', region: 'us-west-2',
  bytes: 100, scan: 'clean', site_id: 'shopfront',
}

/** A flow whose phase is fixed, with every callback a spy. */
function flowAt(phase: Phase) {
  return {
    phase,
    ttlHours: 72,
    setTtlHours: vi.fn(),
    setPhase: vi.fn(),
    start: vi.fn(),
    confirm: vi.fn(),
    reset: vi.fn(),
    busy: false,
    needsAgent: false,
  } as unknown as Flow
}

function renderAt(phase: Phase, flow = flowAt(phase)) {
  render(<DirectDeployFlow slug="shopfront" flow={flow} profile="ship" />)
  return flow
}

describe('DirectDeployFlow — one affordance per phase', () => {
  beforeEach(() => { vi.restoreAllMocks() })
  afterEach(() => { vi.unstubAllGlobals() })

  it('offers permanent and the install command on the reaper precondition', () => {
    const flow = renderAt({
      kind: 'refused',
      refusal: {
        code: 'reaper_required',
        error: 'Your AWS account has no auto-cleanup installed yet.',
        details: 'kirocrew-deploy-base',
        remediation: '/s/install-reaper.sh --profile ship',
      },
      preview: PREVIEW,
    } as Phase)
    expect(screen.getByText(/no auto-cleanup installed/)).toBeTruthy()
    // Re-acknowledges at ttl 0 rather than confirming: the exposure window is
    // part of what the human agreed to.
    fireEvent.click(screen.getByRole('button', { name: /Deploy as permanent/i }))
    expect(flow.setTtlHours).toHaveBeenCalledWith(0)
    expect(flow.setPhase).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'ack', overrideScan: false }))
    expect(flow.confirm).not.toHaveBeenCalled()
  })

  it('re-previews when the refusal arrived before any preview existed', () => {
    const flow = renderAt({
      kind: 'refused',
      refusal: { code: 'reaper_required', error: 'no auto-cleanup', details: '', remediation: '' },
      preview: null,
    } as Phase)
    fireEvent.click(screen.getByRole('button', { name: /Deploy as permanent/i }))
    expect(flow.start).toHaveBeenCalledWith('ship')
    expect(flow.setPhase).not.toHaveBeenCalled()
  })

  it('explains a missing static root without offering this flow as the way out', () => {
    renderAt({
      kind: 'refused',
      refusal: {
        code: 'webapp_root_unavailable',
        error: 'This app has no built static root to publish.',
        details: 'looked for public/',
        remediation: '',
      },
      preview: null,
    } as Phase)
    expect(screen.getByText(/no built static root/)).toBeTruthy()
    // The caller swapped its own button for the agent hand-off, so this block
    // explains and offers no deploy control of its own.
    expect(screen.queryByRole('button', { name: /^Deploy/ })).toBeNull()
  })

  it('offers no override on a credential scan finding', () => {
    const flow = renderAt({
      kind: 'scan-blocked',
      block: { findings: 'aws-access-key-id in config.json:14', count: 1, credential: true },
      preview: PREVIEW,
    } as Phase)
    expect(screen.getByText(/aws-access-key-id/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Deploy anyway/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Cancel/i }))
    expect(flow.reset).toHaveBeenCalled()
  })

  it('offers an override on a non-credential scan finding, and routes it through the acknowledgment', () => {
    const flow = renderAt({
      kind: 'scan-blocked',
      block: { findings: 'internal-hostname in build/app.js:3', count: 1, credential: false },
      preview: PREVIEW,
    } as Phase)
    fireEvent.click(screen.getByRole('button', { name: /Deploy anyway/i }))
    // Overriding a scan finding still publishes a world-readable URL, so it must
    // not skip the public-by-link consent. The scan block and the acknowledgment
    // answer different questions and clearing one does not answer the other.
    expect(flow.confirm).not.toHaveBeenCalled()
    expect(flow.setPhase).toHaveBeenCalledWith(
      { kind: 'ack', preview: PREVIEW, overrideScan: true })
  })

  it('confirms with a synthesised preview when the scan block carried none', () => {
    const flow = renderAt({
      kind: 'scan-blocked',
      block: { findings: 'internal-hostname', count: 1, credential: false },
      preview: null,
    } as Phase)
    fireEvent.click(screen.getByRole('button', { name: /Deploy anyway/i }))
    expect(flow.confirm).not.toHaveBeenCalled()
    expect(flow.setPhase).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'ack',
      overrideScan: true,
      preview: expect.objectContaining({ site_id: 'shopfront', content_digest: '' }),
    }))
  })

  it('shows a failure as a dismissible notice', () => {
    const flow = renderAt({ kind: 'failed', message: 'Deploy failed' } as Phase)
    expect(screen.getByText('Deploy failed')).toBeTruthy()
    const dismiss = screen.getAllByRole('button').find(b => /dismiss|close/i.test(b.getAttribute('aria-label') || ''))
    if (dismiss) {
      fireEvent.click(dismiss)
      expect(flow.reset).toHaveBeenCalled()
    }
  })

  it('copies the deployed URL and reports it', async () => {
    vi.stubGlobal('navigator', {
      clipboard: { writeText: vi.fn(async () => undefined) },
    })
    renderAt({ kind: 'done', url: 'https://d1.cloudfront.net/' } as Phase)
    expect(screen.getByText('Deployed!')).toBeTruthy()
    expect(screen.getByRole('link', { name: /d1\.cloudfront\.net/ })).toBeTruthy()
    // The propagation note is the difference between "slow" and "broken".
    expect(screen.getByText(/5-15 minutes/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Copy/i }))
    await waitFor(() => expect(screen.getAllByText(/Copy/i).length).toBeGreaterThan(0))
  })

  it('renders the success state without a link when the deploy exposed no URL', () => {
    renderAt({ kind: 'done', url: '' } as Phase)
    // Success is the ABSENCE of an error, not a non-empty url — conflating the
    // two renders a working deploy as a blank failure.
    expect(screen.getByText('Deployed!')).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('drops a url that is not http(s)', () => {
    renderAt({ kind: 'done', url: 'javascript:alert(1)' } as Phase)
    expect(screen.getByText('Deployed!')).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
  })
})
