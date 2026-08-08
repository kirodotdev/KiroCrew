/**
 * Tests for the Security Scanner builtin page.
 *
 * The page is self-contained (fetch → the builtin backend), so the seam worth
 * pinning is the wiring: it must query the ``/api/apps/security-scanner/*``
 * surface on mount, render the status the backend returns, and switch tabs to
 * the findings list. ``fetch`` is stubbed per-URL; no network, no timers fire
 * (the 30s poll interval never elapses under the test clock).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import SecurityScannerPage from '../SecurityScannerPage'

const STATUS = {
  running: false,
  patterns_total: 2,
  findings_total: 3,
  coverage: { 'auth-bypass': 2, 'path-traversal': 1 },
  findings_by_status: { exploited: 1 },
  avg_false_positive_rate: 0,
}
const FINDINGS = {
  findings: [
    { id: 'f1', topic: 'auth-bypass', title: 'Weak owner check', location: 'taskrunner.py:41', severity: 'high', status: 'exploited' },
  ],
}
const KNOWLEDGE = { patterns: [{ id: 'p1', topic: 'auth-bypass', pattern: 'single-owner trust', source: 'seed', confidence: 0.9 }] }

function makeFetch() {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    let body: unknown = {}
    if (url.includes('/status')) body = STATUS
    else if (url.includes('/knowledge')) body = KNOWLEDGE
    else if (url.includes('/findings')) body = FINDINGS
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(body),
    } as Response)
  })
}

describe('SecurityScannerPage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', makeFetch())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('renders the header immediately', () => {
    render(<SecurityScannerPage />)
    expect(screen.getByText('Security Scanner')).toBeTruthy()
  })

  it('queries the builtin status/findings/knowledge endpoints on mount', async () => {
    render(<SecurityScannerPage />)
    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]))
      expect(calls.some((u) => u.includes('/api/apps/security-scanner/status'))).toBe(true)
      expect(calls.some((u) => u.includes('/api/apps/security-scanner/findings'))).toBe(true)
      expect(calls.some((u) => u.includes('/api/apps/security-scanner/knowledge'))).toBe(true)
    })
  })

  it('renders coverage from the status payload', async () => {
    render(<SecurityScannerPage />)
    await waitFor(() => expect(screen.getByText('Knowledge Coverage by Attack Surface')).toBeTruthy())
    expect(screen.getByText('auth-bypass')).toBeTruthy()
    expect(screen.getByText('path-traversal')).toBeTruthy()
  })

  it('switches to the Findings tab and lists a finding', async () => {
    render(<SecurityScannerPage />)
    await waitFor(() => expect(screen.getByText('Knowledge Coverage by Attack Surface')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Findings' }))
    expect(await screen.findByText('Weak owner check')).toBeTruthy()
    expect(screen.getByText('taskrunner.py:41')).toBeTruthy()
  })

  it('shows an error state when the backend is unreachable', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('boom'))),
    )
    render(<SecurityScannerPage />)
    expect(await screen.findByText(/Could not reach the scanner backend/)).toBeTruthy()
  })

  it('posts ingested report text to the knowledge/ingest endpoint', async () => {
    render(<SecurityScannerPage />)
    await waitFor(() => expect(screen.getByText('Knowledge Coverage by Attack Surface')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Knowledge' }))
    const box = await screen.findByPlaceholderText(/Paste a security report/)
    fireEvent.change(box, { target: { value: 'os.path.join escape in loader' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ingest & Learn' }))
    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
      const ingest = calls.find((c) => String(c[0]).includes('/knowledge/ingest'))
      expect(ingest).toBeTruthy()
      expect(String((ingest![1] as RequestInit).body)).toContain('os.path.join escape')
    })
  })

  it('launches a background scan slot when Scan Now is clicked', async () => {
    render(<SecurityScannerPage />)
    await waitFor(() => expect(screen.getByText('Knowledge Coverage by Attack Surface')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Scan Now/ }))
    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
      const chat = calls.find((c) => String(c[0]).includes('/api/chat?ws=1'))
      expect(chat).toBeTruthy()
      expect(String((chat![1] as RequestInit).body)).toContain('security-scanner-scan')
    })
  })
})
