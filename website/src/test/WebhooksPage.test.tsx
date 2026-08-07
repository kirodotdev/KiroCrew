/**
 * Webhooks page — rail-and-detail shell contract.
 *
 * What these pin (the properties a "does it render" test would miss):
 * - the rail always shows all four groups: the pinned Setup row, Tokens,
 *   Registered contexts, Recent runs
 * - a fresh install (zero tokens / contexts / runs) explains each empty group
 *   in plain language instead of rendering three blank gaps
 * - rail selection is what drives the detail pane
 * - both generated secrets appear exactly once, in a dismissible reveal
 * - revoking takes two deliberate clicks (the first one must not mutate)
 * - the three freshness tiers are visually and textually distinguishable
 * - the kill switch has three distinct banner states, and turning it OFF
 *   takes two clicks while turning it back ON takes one
 * - the request example switches between the signed and bearer-only forms
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { WebhooksView } from '../api/client'

const webhooks = vi.fn()
const createWebhookToken = vi.fn()
const deleteWebhookToken = vi.fn()
const deleteWebhookContext = vi.fn()
const testWebhook = vi.fn()
const setWebhooksEnabled = vi.fn()

// useIsMobile reads window.matchMedia at MODULE load, so a per-test matchMedia
// stub cannot move it. Mock the hook with a mutable flag instead — desktop by
// default, flipped by the narrow-viewport test. Same pattern as
// ChatPage.responsivePanel.test.tsx.
let mockIsMobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mockIsMobile }))

vi.mock('../api/client', () => ({
  api: {
    webhooks: (...a: unknown[]) => webhooks(...a),
    createWebhookToken: (...a: unknown[]) => createWebhookToken(...a),
    deleteWebhookToken: (...a: unknown[]) => deleteWebhookToken(...a),
    deleteWebhookContext: (...a: unknown[]) => deleteWebhookContext(...a),
    testWebhook: (...a: unknown[]) => testWebhook(...a),
    setWebhooksEnabled: (...a: unknown[]) => setWebhooksEnabled(...a),
  },
}))

import WebhooksPage from '../pages/WebhooksPage'

const NOW = Math.floor(Date.now() / 1000)

const EMPTY: WebhooksView = {
  enabled: false,
  switch_on: true,
  has_tokens: false,
  url: 'http://localhost:6776/api/hooks/agent',
  slots: { in_use: 0, max: 6 },
  limits: {
    session_key_prefix: 'hook:', message_max: 49999,
    timeout_default: 599, timeout_max: 3593, max_concurrent: 6,
    signature_window_seconds: 300,
  },
  tokens: [], contexts: [], runs: [],
}

const POPULATED: WebhooksView = {
  ...EMPTY,
  enabled: true,
  has_tokens: true,
  slots: { in_use: 2, max: 6 },
  tokens: [
    {
      id: 'wht_7f3a91', label: 'Review Bot', display_prefix: 'kc_whk_4f2b', last4: '1f3a',
      created_at: NOW - 7200, last_used_at: NOW - 480,
      require_signature: true, legacy: false,
    },
  ],
  contexts: [
    {
      hook_id: 'review:pr-123', session_key: 'hook:review:pr-123',
      registered_at: NOW - 480, age_seconds: 480, freshness: 'fresh',
      context_summary: 'Reviewing PR #123; awaiting the next analysis pass.', context_chars: 412,
    },
    {
      hook_id: 'deploy:prod-4471', session_key: 'hook:deploy:prod-4471',
      registered_at: NOW - 21600, age_seconds: 21600, freshness: 'stale',
      context_summary: 'Deploy 4471 is mid-rollout at 25%.', context_chars: 268,
    },
    {
      hook_id: 'ci:build-88', session_key: 'hook:ci:build-88',
      registered_at: NOW - 172800, age_seconds: 172800, freshness: 'expired',
      context_summary: 'Build 88 failed on the Coverage Gate.', context_chars: 96,
    },
  ],
  runs: [
    {
      id: 'run_1', hook_id: 'review:pr-123', session_key: 'hook:review:pr-123',
      name: 'Review Bot', outcome: 'completed', started_at: NOW - 480,
      duration_ms: 42000, result_chars: 2150, token_id: 'wht_7f3a91',
      delivered: true, detail: 'Delivered to notifications + Slack DM',
    },
    {
      id: 'run_2', hook_id: 'deploy:prod-4471', session_key: 'hook:deploy:prod-4471',
      name: 'Deploy Bot', outcome: 'timeout', started_at: NOW - 3060,
      duration_ms: 599000, result_chars: 0, token_id: 'wht_7f3a91',
      delivered: false, detail: 'Turn exceeded the 599s timeout and was cancelled.',
    },
  ],
}

function mount(view: WebhooksView) {
  webhooks.mockResolvedValue(view)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <WebhooksPage />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  // Desktop unless a test opts in; a leaked true would silently change the
  // rendered shell for every test after it.
  mockIsMobile = false
})
afterEach(cleanup)

describe('Webhooks rail', () => {
  it('renders all four groups', async () => {
    mount(POPULATED)
    expect(await screen.findByTestId('webhook-row-setup')).toBeTruthy()
    // Scoped to the rail: "Tokens" is also a detail-pane section heading.
    const rail = within(screen.getByTestId('webhook-rail'))
    expect(rail.getByText('Tokens')).toBeTruthy()
    expect(rail.getByText('Registered contexts')).toBeTruthy()
    expect(rail.getByText('Recent runs')).toBeTruthy()
    // One row per entity, in the group it belongs to.
    expect(screen.getByTestId('webhook-row-token-wht_7f3a91')).toBeTruthy()
    expect(screen.getByTestId('webhook-row-context-review:pr-123')).toBeTruthy()
    expect(screen.getByTestId('webhook-row-run-run_1')).toBeTruthy()
  })

  it('explains every empty group on a fresh install', async () => {
    mount(EMPTY)
    expect(await screen.findByTestId('webhook-row-setup')).toBeTruthy()
    const rail = within(screen.getByTestId('webhook-rail'))
    // The pinned row states the disabled reason rather than a bare count.
    expect(rail.getByText(/Disabled — no token set/)).toBeTruthy()
    expect(rail.getByText(/No tokens yet\./)).toBeTruthy()
    expect(rail.getByText(/Nothing registered\./)).toBeTruthy()
    expect(rail.getByText(/No calls recorded yet\./)).toBeTruthy()
    // And the groups themselves are still present, so the page shape is stable.
    expect(rail.getByText('Tokens')).toBeTruthy()
    expect(rail.getByText('Registered contexts')).toBeTruthy()
    expect(rail.getByText('Recent runs')).toBeTruthy()
  })

  it('filters every group from one input', async () => {
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')
    fireEvent.change(screen.getByLabelText('Filter webhooks'), { target: { value: 'pr-123' } })
    expect(screen.getByTestId('webhook-row-context-review:pr-123')).toBeTruthy()
    expect(screen.queryByTestId('webhook-row-context-ci:build-88')).toBeNull()
    expect(screen.queryByTestId('webhook-row-token-wht_7f3a91')).toBeNull()
  })
})

describe('Webhooks detail pane', () => {
  it('swaps pane when a rail row is selected', async () => {
    mount(POPULATED)
    // The rail's Setup row only renders once the snapshot has loaded.
    await screen.findByTestId('webhook-row-setup')
    const detail = screen.getByTestId('webhook-detail')
    expect(detail.getAttribute('data-pane')).toBe('setup')
    expect(screen.getByTestId('webhook-detail-title').textContent).toContain('Setup')

    fireEvent.click(screen.getByTestId('webhook-row-context-review:pr-123'))
    expect(screen.getByTestId('webhook-detail').getAttribute('data-pane')).toBe('context')
    expect(screen.getByTestId('webhook-detail-title').textContent).toBe('review:pr-123')

    fireEvent.click(screen.getByTestId('webhook-row-token-wht_7f3a91'))
    expect(screen.getByTestId('webhook-detail').getAttribute('data-pane')).toBe('token')
    expect(screen.getByTestId('webhook-detail-title').textContent).toBe('Review Bot')

    fireEvent.click(screen.getByTestId('webhook-row-run-run_2'))
    expect(screen.getByTestId('webhook-detail').getAttribute('data-pane')).toBe('run')
    expect(screen.getByTestId('webhook-detail-title').textContent).toContain('Timed out')

    fireEvent.click(screen.getByTestId('webhook-row-setup'))
    expect(screen.getByTestId('webhook-detail').getAttribute('data-pane')).toBe('setup')
  })

  it('reports the destinations that actually took delivery, not both', async () => {
    // `delivered` is `bool(destinations)` on the backend: true when EITHER the
    // dashboard notification or the Slack DM landed. A static "notification +
    // Slack DM" label therefore claimed a DM had gone out on runs where Slack
    // failed. The run's `detail` carries the exact set, so it must be shown.
    mount({
      ...POPULATED,
      runs: [{
        ...POPULATED.runs[0],
        id: 'run_partial',
        delivered: true,
        detail: 'Delivered to notifications',
      }],
    })
    await screen.findByTestId('webhook-row-setup')
    fireEvent.click(screen.getByTestId('webhook-row-run-run_partial'))
    const detail = screen.getByTestId('webhook-detail')
    expect(detail.textContent).toContain('Delivered to notifications')
    // The claim under test: a Slack DM must NOT be asserted for this run.
    expect(detail.textContent).not.toContain('Slack DM')
  })

  it('renders the three freshness tiers distinctly', async () => {
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')

    const dots = ['review:pr-123', 'deploy:prod-4471', 'ci:build-88']
      .map(id => screen.getByTestId(`webhook-freshness-${id}`))
    expect(dots.map(d => d.getAttribute('data-freshness'))).toEqual(['fresh', 'stale', 'expired'])
    // Distinct theme tokens — a single colour for all three would make the tier
    // unreadable at a glance.
    const classes = dots.map(d => d.className)
    expect(new Set(classes).size).toBe(3)

    // Each tier's detail banner states what injection actually does at that tier.
    fireEvent.click(screen.getByTestId('webhook-row-context-review:pr-123'))
    expect(screen.getByTestId('webhook-context-banner').textContent).toContain('injected verbatim')
    fireEvent.click(screen.getByTestId('webhook-row-context-deploy:prod-4471'))
    expect(screen.getByTestId('webhook-context-banner').textContent).toContain('out-of-date warning')
    fireEvent.click(screen.getByTestId('webhook-row-context-ci:build-88'))
    expect(screen.getByTestId('webhook-context-banner').textContent).toContain('dropped')
  })

  it('needs a confirm click before deleting a context', async () => {
    deleteWebhookContext.mockResolvedValue({ ok: true })
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')
    fireEvent.click(screen.getByTestId('webhook-row-context-review:pr-123'))

    fireEvent.click(screen.getByTestId('webhook-delete-context'))
    expect(deleteWebhookContext).not.toHaveBeenCalled()
    expect(screen.getByTestId('webhook-delete-context').textContent).toContain('Confirm delete')

    fireEvent.click(screen.getByTestId('webhook-delete-context'))
    await waitFor(() => expect(deleteWebhookContext).toHaveBeenCalledWith('review:pr-123'))
  })
})

describe('Webhooks token lifecycle', () => {
  it('shows both raw secrets once and dismisses them together', async () => {
    createWebhookToken.mockResolvedValue({
      ok: true,
      token: 'kc_whk_TESTSECRET0123456789abcdefghij',
      signing_secret: 'kc_whs_SIGNSECRET0123456789abcdefghij',
      entry: { ...POPULATED.tokens[0], id: 'wht_new', label: 'CI Bot' },
    })
    mount(EMPTY)
    await screen.findByTestId('webhook-row-setup')
    expect(screen.queryByTestId('webhook-token-reveal')).toBeNull()

    fireEvent.change(screen.getByLabelText('New token label'), { target: { value: 'CI Bot' } })
    fireEvent.click(screen.getByText('Generate token'))
    // Signing is on by default, so the mint asks for a signing secret.
    await waitFor(() => expect(createWebhookToken).toHaveBeenCalledWith('CI Bot', true))

    const reveal = await screen.findByTestId('webhook-token-reveal')
    // BOTH one-time values are present, and both are labelled for what they do.
    expect(reveal.textContent).toContain('kc_whk_TESTSECRET0123456789abcdefghij')
    expect(reveal.textContent).toContain('kc_whs_SIGNSECRET0123456789abcdefghij')
    expect(screen.getByTestId('webhook-reveal-signing-secret')).toBeTruthy()
    expect(screen.getByLabelText('Copy webhook token')).toBeTruthy()
    expect(screen.getByLabelText('Copy signing secret')).toBeTruthy()
    // The wording has to make the one-shot, unrecoverable nature explicit — for
    // the signing secret too, which is stored retrievably server-side but is
    // never displayed again.
    expect(reveal.textContent).toMatch(/shown once/i)
    expect(reveal.textContent).toMatch(/cannot be shown again|recover/i)
    expect(screen.getByTestId('webhook-reveal-signing-secret').textContent)
      .toMatch(/unrecoverable/i)

    // Dismissal is two-step and covers both secrets: they are unrecoverable once
    // the banner closes, and the dismiss button sits beside the copy buttons, so
    // one stray click must not destroy them.
    fireEvent.click(screen.getByTestId('webhook-reveal-dismiss'))
    expect(screen.getByTestId('webhook-token-reveal')).toBeTruthy()
    expect(screen.getByTestId('webhook-token-reveal').textContent)
      .toContain('kc_whk_TESTSECRET0123456789abcdefghij')
    expect(screen.getByTestId('webhook-token-reveal').textContent)
      .toContain('kc_whs_SIGNSECRET0123456789abcdefghij')

    fireEvent.click(screen.getByTestId('webhook-reveal-dismiss-confirm'))
    await waitFor(() => expect(screen.queryByTestId('webhook-token-reveal')).toBeNull())
  })

  /**
   * A second mint must not be reachable while the first secrets are on screen.
   *
   * The reveal pane holds the ONLY copy of a token and its signing secret. A
   * second mint calls setRevealed, which replaces them — the first credential
   * stays active on the server but becomes unusable, and nothing can recover it.
   * The mint button is therefore disabled until the pane is dismissed.
   */
  it('will not mint again while unrecoverable secrets are still displayed', async () => {
    createWebhookToken.mockResolvedValue({
      ok: true,
      token: 'kc_whk_FIRSTSECRET0123456789abcdefgh',
      signing_secret: 'kc_whs_FIRSTSIGN0123456789abcdefghij',
      entry: { ...POPULATED.tokens[0], id: 'wht_first', label: 'First' },
    })
    mount(EMPTY)
    await screen.findByTestId('webhook-row-setup')

    fireEvent.change(screen.getByLabelText('New token label'), { target: { value: 'First' } })
    fireEvent.click(screen.getByText('Generate token'))
    await screen.findByTestId('webhook-token-reveal')
    expect(createWebhookToken).toHaveBeenCalledTimes(1)

    // A label is present and the button is visible, but minting is refused.
    fireEvent.change(screen.getByLabelText('New token label'), { target: { value: 'Second' } })
    const mint = screen.getByText('Generate token').closest('button')!
    expect(mint.disabled).toBe(true)
    fireEvent.click(mint)
    expect(createWebhookToken).toHaveBeenCalledTimes(1)

    // The first secrets are untouched.
    expect(screen.getByTestId('webhook-token-reveal').textContent)
      .toContain('kc_whk_FIRSTSECRET0123456789abcdefgh')
    expect(screen.getByTestId('webhook-token-reveal').textContent)
      .toContain('kc_whs_FIRSTSIGN0123456789abcdefghij')

    // Dismissing releases the block, so minting is not permanently wedged.
    fireEvent.click(screen.getByTestId('webhook-reveal-dismiss'))
    fireEvent.click(screen.getByTestId('webhook-reveal-dismiss-confirm'))
    await waitFor(() => expect(screen.queryByTestId('webhook-token-reveal')).toBeNull())
    expect(screen.getByText('Generate token').closest('button')!.disabled).toBe(false)
  })

  it('mints bearer-only when signing is switched off, and reveals one secret', async () => {
    createWebhookToken.mockResolvedValue({
      ok: true,
      token: 'kc_whk_BEARERONLY0123456789abcdefghij',
      entry: { ...POPULATED.tokens[0], id: 'wht_b', label: 'Old CI', require_signature: false },
    })
    mount(EMPTY)
    await screen.findByTestId('webhook-row-setup')

    fireEvent.change(screen.getByLabelText('New token label'), { target: { value: 'Old CI' } })
    fireEvent.click(screen.getByLabelText('Require request signing'))
    fireEvent.click(screen.getByText('Generate token'))
    await waitFor(() => expect(createWebhookToken).toHaveBeenCalledWith('Old CI', false))

    const reveal = await screen.findByTestId('webhook-token-reveal')
    expect(reveal.textContent).toContain('kc_whk_BEARERONLY0123456789abcdefghij')
    // No signing secret exists for this token, so no second field is invented.
    expect(screen.queryByTestId('webhook-reveal-signing-secret')).toBeNull()
  })

  it('requires two clicks to revoke a token', async () => {
    deleteWebhookToken.mockResolvedValue({ ok: true })
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')

    const revoke = screen.getByTestId('webhook-revoke-wht_7f3a91')
    fireEvent.click(revoke)
    expect(deleteWebhookToken).not.toHaveBeenCalled()
    expect(screen.getByTestId('webhook-revoke-wht_7f3a91').textContent).toContain('Confirm revoke')

    fireEvent.click(screen.getByTestId('webhook-revoke-wht_7f3a91'))
    await waitFor(() => expect(deleteWebhookToken).toHaveBeenCalledWith('wht_7f3a91'))
  })

  it('offers no revoke for the legacy config token', async () => {
    mount({
      ...POPULATED,
      tokens: [{
        id: 'legacy', label: 'Legacy token (config)', display_prefix: 'kc_whk_0000',
        last4: '0000', created_at: NOW - 86400, last_used_at: null,
        require_signature: false, legacy: true,
      }],
    })
    await screen.findByTestId('webhook-row-setup')
    expect(screen.queryByTestId('webhook-revoke-legacy')).toBeNull()
    expect(screen.getByText(/Remove/).textContent).toContain('to revoke')
  })
})

describe('Webhooks kill switch', () => {
  it('shows a distinct banner for each of the three states', async () => {
    // 1. On, with tokens — the live warning.
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')
    expect(screen.getByTestId('webhook-banner-live')).toBeTruthy()
    expect(screen.queryByTestId('webhook-banner-no-tokens')).toBeNull()
    expect(screen.queryByTestId('webhook-banner-off')).toBeNull()
    cleanup()

    // 2. On, but no token exists yet — the purpose statement, not a warning.
    mount(EMPTY)
    await screen.findByTestId('webhook-row-setup')
    expect(screen.getByTestId('webhook-banner-no-tokens')).toBeTruthy()
    expect(screen.queryByTestId('webhook-banner-live')).toBeNull()
    expect(screen.queryByTestId('webhook-banner-off')).toBeNull()
    cleanup()

    // 3. Switched off with tokens still stored — a third state, not a rerun of
    //    the "no tokens" one, and it must say the tokens are kept.
    mount({ ...POPULATED, enabled: false, switch_on: false })
    await screen.findByTestId('webhook-row-setup')
    const off = screen.getByTestId('webhook-banner-off')
    expect(off.textContent).toMatch(/503/)
    expect(off.textContent).toMatch(/tokens and run history are kept/i)
    expect(screen.queryByTestId('webhook-banner-live')).toBeNull()
    expect(screen.queryByTestId('webhook-banner-no-tokens')).toBeNull()
  })

  it('needs two clicks to turn off, and says nothing is destroyed', async () => {
    setWebhooksEnabled.mockResolvedValue({ ok: true, switch_on: false })
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')

    fireEvent.click(screen.getByTestId('webhook-switch-off'))
    expect(setWebhooksEnabled).not.toHaveBeenCalled()
    // The armed state has to explain that this is reversible before the user
    // commits to it.
    expect(screen.getByTestId('webhook-switch-row').textContent)
      .toMatch(/tokens and run history are kept/i)

    fireEvent.click(screen.getByTestId('webhook-switch-off-confirm'))
    await waitFor(() => expect(setWebhooksEnabled).toHaveBeenCalledWith(false))
  })

  it('backs out of the off-confirm without mutating', async () => {
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')
    fireEvent.click(screen.getByTestId('webhook-switch-off'))
    fireEvent.click(screen.getByText('Keep it on'))
    expect(setWebhooksEnabled).not.toHaveBeenCalled()
    expect(screen.getByTestId('webhook-switch-off')).toBeTruthy()
  })

  it('turns back on with a single click', async () => {
    setWebhooksEnabled.mockResolvedValue({ ok: true, switch_on: true })
    mount({ ...POPULATED, enabled: false, switch_on: false })
    await screen.findByTestId('webhook-row-setup')

    fireEvent.click(screen.getByTestId('webhook-switch-on'))
    await waitFor(() => expect(setWebhooksEnabled).toHaveBeenCalledWith(true))
  })

  it('blocks the test request while switched off, for the stated reason', async () => {
    mount({ ...POPULATED, enabled: false, switch_on: false })
    await screen.findByTestId('webhook-row-setup')
    const btn = screen.getByText('Send test request').closest('button')
    expect(btn?.disabled).toBe(true)
    expect(btn?.title).toMatch(/switched off/i)
  })
})

describe('Webhooks request signing', () => {
  it('shows per-token whether a signature is required', async () => {
    mount({
      ...POPULATED,
      tokens: [
        POPULATED.tokens[0],
        {
          id: 'wht_bearer', label: 'Old CI', display_prefix: 'kc_whk_9a1c', last4: 'cc02',
          created_at: NOW - 3600, last_used_at: null,
          require_signature: false, legacy: false,
        },
      ],
    })
    await screen.findByTestId('webhook-row-setup')
    // Setup pane token table.
    expect(screen.getByTestId('webhook-signing-wht_7f3a91').getAttribute('data-signing'))
      .toBe('required')
    expect(screen.getByTestId('webhook-signing-wht_bearer').getAttribute('data-signing'))
      .toBe('bearer-only')

    // Token pane restates it in words, not just a badge.
    fireEvent.click(screen.getByTestId('webhook-row-token-wht_bearer'))
    expect(screen.getByTestId('webhook-detail').textContent).toMatch(/bearer header alone/i)
    fireEvent.click(screen.getByTestId('webhook-row-token-wht_7f3a91'))
    expect(screen.getByTestId('webhook-detail').textContent).toMatch(/X-KiroCrew-Signature/)
  })

  it('switches the request example between the signed and bearer-only forms', async () => {
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')

    // The stored token requires signing, so the signed form is the default.
    expect(screen.getByTestId('webhook-example-mode').getAttribute('data-mode')).toBe('signed')
    const signed = screen.getByLabelText('Example signed request').textContent || ''
    expect(signed).toContain('openssl dgst -sha256 -hmac')
    expect(signed).toContain('X-KiroCrew-Timestamp: $TS')
    expect(signed).toContain('X-KiroCrew-Signature: sha256=$SIG')
    expect(signed).toContain('--data-raw "$BODY"')
    expect(signed).toContain('300 seconds of gateway time')

    fireEvent.click(within(screen.getByTestId('webhook-example-mode')).getByText('Bearer only'))
    expect(screen.getByTestId('webhook-example-mode').getAttribute('data-mode')).toBe('bearer')
    const bearer = screen.getByLabelText('Example curl request').textContent || ''
    expect(bearer).toContain("Authorization: Bearer <token>")
    expect(bearer).not.toContain('openssl')
    expect(bearer).not.toContain('X-KiroCrew-Signature')

    fireEvent.click(within(screen.getByTestId('webhook-example-mode')).getByText('Signed'))
    expect(screen.getByLabelText('Example signed request').textContent).toContain('openssl')
  })

  it('shell-quotes a hostile hook id so a copied example cannot run it', async () => {
    // register_hook decides the hook id, so it is not our string. Inside a
    // single-quoted shell word, one apostrophe would close the quote and the
    // rest would be executed by whoever pastes the snippet.
    const hostile = "pr'; printf INJECTED; #"
    mount({
      ...POPULATED,
      contexts: [{ ...POPULATED.contexts[0], hook_id: hostile, session_key: `hook:${hostile}` }],
    })
    await screen.findByTestId('webhook-row-setup')
    // The hostile id only reaches the CONTEXT pane's example, so select it.
    fireEvent.click(screen.getByTestId(`webhook-row-context-${hostile}`))

    const signed = screen.getByLabelText(`Example request for ${hostile}`).textContent || ''
    const bodyLine = signed.split('\n').find((l) => l.startsWith('BODY=')) || ''
    expect(bodyLine).toContain("'\\''")
    // The real property is that BODY is ONE well-formed single-quoted word:
    // strip every escaped-quote sequence and exactly the opening and closing
    // quotes may remain. A naive `not.toContain("'; printf")` would not do —
    // the correctly escaped form legitimately contains that substring.
    const withoutEscapes = bodyLine.slice('BODY='.length).replaceAll("'\\''", '\u0000')
    expect(withoutEscapes.startsWith("'")).toBe(true)
    expect(withoutEscapes.endsWith("'")).toBe(true)
    expect(withoutEscapes.slice(1, -1)).not.toContain("'")
  })

  it('collapses the rail on a narrow viewport so the detail pane stays usable', async () => {
    // At 375px a 300px fixed rail left the detail controls ~70px wide. On a
    // phone the two columns become a drill-down instead: strip + detail by
    // default, rail full-width while browsing, back to detail once an entry is
    // chosen. The resize edge goes away entirely — no width there serves both.
    mockIsMobile = true
    mount(POPULATED)

    // Default: icon strip, detail pane visible, no resize edge.
    await screen.findByTestId('webhook-detail-title')
    expect(screen.queryByTestId('webhook-rail')).toBeNull()
    expect(screen.queryByLabelText('Resize webhooks rail')).toBeNull()

    // Browsing: the rail opens full-width and the detail pane steps aside.
    fireEvent.click(screen.getByLabelText('Expand webhooks rail'))
    const rail = await screen.findByTestId('webhook-rail')
    expect(rail.style.width).toBe('100%')

    // Choosing an entry hands the screen back to the detail pane.
    fireEvent.click(screen.getByTestId('webhook-row-token-wht_7f3a91'))
    expect(screen.queryByTestId('webhook-rail')).toBeNull()
    expect(screen.getByTestId('webhook-detail-title').textContent).toContain('Review Bot')
  })

  it('keeps the resizable rail on a desktop viewport', async () => {
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')

    expect(screen.getByTestId('webhook-rail')).toBeTruthy()
    expect(screen.getByLabelText('Resize webhooks rail')).toBeTruthy()
  })

  it('lets the page shrink below its content width', async () => {
    // The page root is itself a flex ITEM of the dashboard shell, and a flex
    // item defaults to `min-width: auto` — it refuses to go below min-content.
    // Without `min-w-0` here the rail plus detail pane pinned the page at about
    // 846px whatever the viewport, so on a phone the detail side was clipped
    // rather than laid out narrow. jsdom computes no layout, so this asserts the
    // class contract that produces it.
    const { container } = mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')

    const root = container.firstElementChild as HTMLElement
    expect(root.className).toContain('min-w-0')
    expect(root.className).toContain('w-full')
  })

  it('gives the token table its own horizontal scroller', async () => {
    // Six columns cannot reflow under ~560px. They must scroll inside their own
    // box rather than overflow the pane, so the surrounding copy still wraps.
    mount(POPULATED)
    const table = await screen.findByRole('table')
    const scroller = table.parentElement as HTMLElement
    expect(scroller.className).toContain('overflow-x-auto')
    expect(table.className).toContain('min-w-max')
  })

  it('defaults the example to bearer-only when no token requires a signature', async () => {
    mount({
      ...POPULATED,
      tokens: [{ ...POPULATED.tokens[0], require_signature: false }],
    })
    await screen.findByTestId('webhook-row-setup')
    expect(screen.getByTestId('webhook-example-mode').getAttribute('data-mode')).toBe('bearer')
  })

  it('states the signing window and replay rule in the limits grid', async () => {
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')
    const detail = screen.getByTestId('webhook-detail').textContent || ''
    expect(detail).toContain('HMAC-SHA256 · ±300s window')
    expect(detail).toMatch(/cannot be replayed/i)
  })
})

describe('Webhooks failure handling', () => {
  it('stays usable when the endpoint is unavailable', async () => {
    webhooks.mockRejectedValue(new Error('HTTP 404'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <WebhooksPage />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/Webhook settings are unavailable/)).toBeTruthy()
    // The reference content still renders from the empty view.
    expect(screen.getByText('Endpoint')).toBeTruthy()
    expect(screen.getByTestId('webhook-row-setup')).toBeTruthy()
  })

  it('reports the outcome of a test request', async () => {
    testWebhook.mockResolvedValue({ ok: true, status: 202, session_key: 'hook:test:1' })
    mount(POPULATED)
    await screen.findByTestId('webhook-row-setup')
    fireEvent.click(screen.getByText('Send test request'))
    const banner = await screen.findByTestId('webhook-test-result')
    expect(banner.textContent).toContain('202')
    expect(banner.textContent).toContain('hook:test:1')
  })
})
