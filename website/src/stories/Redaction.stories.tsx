import type { Meta, StoryObj } from '@storybook/react-vite'
import { useEffect, useRef, useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { RedactionSection } from '../pages/settings/SecurityPanel'

/**
 * The redaction UI, reproducing the RFC prototype's conversation
 * (docs/request-for-change/assets/redaction-explain-reveal-prototype.html,
 * variants C then A): a credentials file with three removed values, a blocked
 * reviewers link, and the first-run coach. Stories that show a card open it by
 * clicking its marker, the same click a reader makes.
 */

const CRED = '[REDACTED: credential]'
const LINK = '[REDACTED: suspicious URL to reviews.corp.example]'
const REVIEWS_FILTER = encodeURIComponent(JSON.stringify({
  status: ['open'], owner: 'rayrayxu', range: ['3', '0', 'd'].join(''), cols: ['id', 'title', 'owner'],
}))
const REVIEWS_URL =
  `https://reviews.corp.example/reviews?filter=${REVIEWS_FILTER}&sort=-created&view=table`
  + '&page=1&per_page=50&since=2026-09-01&until=2026-09-30&labels=needs-review%2Csecurity&team=platform'

const REPLY = [
  '**Your dev profile is set up correctly.** Region is `us-west-2`; `~/.aws/credentials` has complete key pairs for both profiles, and dev also carries a session token (it expires):',
  '',
  '```ini',
  '[default]',
  'aws_access_key_id = AKIA…EXAMPLE',
  CRED,
  '',
  '[dev]',
  'aws_access_key_id = AKIA…DEVKEY',
  CRED,
  CRED,
  '```',
  '',
  `The reviewers filter page: ${LINK}`,
  '',
  'To switch to dev and verify identity:',
  '',
  '```bash',
  'AWS_PROFILE=dev aws sts get-caller-identity',
  '```',
].join('\n')

const FILE = (section: string) => ({ type: 'file', path: '~/.aws/credentials', section })
const REDACTIONS = [
  { ordinal: 0, rule: 'aws_secret_access_key', label: 'aws_secret_access_key = ', source: FILE('default'), view_command: 'aws configure get aws_secret_access_key --profile default', profile_command: null },
  { ordinal: 1, rule: 'aws_secret_access_key', label: 'aws_secret_access_key = ', source: FILE('dev'), view_command: 'aws configure get aws_secret_access_key --profile dev', profile_command: null },
  { ordinal: 2, rule: 'aws_session_token', label: 'aws_session_token = ', source: FILE('dev'), view_command: 'aws configure get aws_session_token --profile dev', profile_command: 'AWS_PROFILE=dev aws sts get-caller-identity' },
]
const BLOCKED = [
  { domain: 'reviews.corp.example', rule: 'exfil_query_length', path: '/reviews', query_chars: REVIEWS_URL.length - REVIEWS_URL.indexOf('?') - 1, url: REVIEWS_URL, url_withheld: null },
]

/** Renders the reply, then clicks `selector` (the n-th match) so a card is open. */
function Reply({ click, nth = 0, then, coach = false, blocked = BLOCKED }: {
  click?: string
  nth?: number
  then?: string
  coach?: boolean
  blocked?: unknown
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!click) return
    const el = ref.current?.querySelectorAll<HTMLElement>(`[data-testid="${click}"]`)[nth]
    el?.click()
    if (then) setTimeout(() => ref.current?.querySelector<HTMLElement>(then)?.click(), 50)
  }, [click, nth, then])
  return (
    <div ref={ref} className="max-w-[900px] p-4 text-[14px] leading-6 text-text">
      <MarkdownRenderer content={REPLY} redactions={REDACTIONS} blockedLinks={blocked} slotKey="story" redactionCoach={coach} />
    </div>
  )
}

const meta = {
  title: 'Chat/Redaction',
  component: Reply,
  parameters: { layout: 'fullscreen' },
} satisfies Meta<typeof Reply>
export default meta
type Story = StoryObj<typeof meta>

/** Variant C: the first reply in a session with removed values carries the coach. */
export const FirstRunCoach: Story = { args: { coach: true } }
/** Variant A: later replies carry only the lock tags and the chip. */
export const InlineMarkers: Story = { args: {} }
export const CredentialCard: Story = { args: { click: 'credential-tag', nth: 0 } }
export const SessionTokenCard: Story = { args: { click: 'credential-tag', nth: 2 } }
export const CredentialMoreWays: Story = { args: { click: 'credential-tag', nth: 0, then: '[data-testid="credential-more"] > button' } }
export const BlockedLinkCard: Story = { args: { click: 'blocked-link-inspect' } }
export const BlockedLinkFullUrl: Story = { args: { click: 'blocked-link-inspect', then: '[data-testid="blocked-link-review"] > button' } }
export const OpenOnceConfirm: Story = { args: { click: 'blocked-link-inspect', then: '[data-testid="blocked-link-open-once"]' } }
export const AllowConfirm: Story = { args: { click: 'blocked-link-inspect', then: '[data-testid="blocked-link-allow"]' } }
export const LinkAddressWithheld: Story = {
  args: {
    click: 'blocked-link-inspect',
    blocked: [{ ...BLOCKED[0], url: null, url_withheld: 'credential' }],
  },
}
export const LinkAddressTooLong: Story = {
  args: {
    click: 'blocked-link-inspect',
    blocked: [{ ...BLOCKED[0], url: null, url_withheld: 'length' }],
  },
}

// ── credential card states ───────────────────────────────────────────────

/** A secret found in a command's output: the card offers the command the
 *  agent ran, with the red check-before-Enter warning. */
const COMMAND_REDACTIONS = REDACTIONS.map(r => r.ordinal === 0
  ? { ...r, source: { type: 'command', command: 'cat ~/.aws/credentials' }, view_command: 'cat ~/.aws/credentials' }
  : r)

/** Clicks `steps` in order (`selector@n` clicks the n-th match); the
 *  Terminal answers every prefill as taken. */
function Scripted({ steps, redactions = REDACTIONS }: { steps: string[]; redactions?: unknown }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const onPrefill = (e: Event) => {
      const d = (e as CustomEvent).detail
      window.dispatchEvent(new CustomEvent('mc:prefill-terminal-result', { detail: { reqId: d.reqId, ok: true } }))
    }
    window.addEventListener('mc:prefill-terminal', onPrefill)
    let i = 0
    const next = () => {
      if (i >= steps.length) return
      const [sel, n] = steps[i++].split('@')
      ref.current?.querySelectorAll<HTMLElement>(sel)[Number(n ?? 0)]?.click()
      setTimeout(next, 400)
    }
    const t = setTimeout(next, 100)
    return () => { clearTimeout(t); window.removeEventListener('mc:prefill-terminal', onPrefill) }
  }, [steps])
  return (
    <div ref={ref} className="max-w-[900px] p-4 text-[14px] leading-6 text-text">
      <MarkdownRenderer content={REPLY} redactions={redactions} blockedLinks={BLOCKED} slotKey="story" />
    </div>
  )
}

const FIRST_TAG = '[data-testid="credential-tag"]'
export const CredentialTechnicalDetails: StoryObj<typeof Scripted> = {
  render: () => <Scripted steps={[FIRST_TAG, '[data-testid="credential-technical"] > button']} />,
}
export const SessionTokenMoreWays: StoryObj<typeof Scripted> = {
  render: () => <Scripted steps={[`${FIRST_TAG}@2`, '[data-testid="credential-more"] > button']} />,
}
/** Open in Terminal, taken: the command sits in the Terminal, not run. */
export const CredentialTerminalAdded: StoryObj<typeof Scripted> = {
  render: () => <Scripted steps={[FIRST_TAG, '[data-testid="redaction-open-terminal"]']} />,
}
/** A command source: the pre-fill carries the agent's command and the warning. */
export const CredentialCommandSource: StoryObj<typeof Scripted> = {
  render: () => <Scripted steps={[FIRST_TAG]} redactions={COMMAND_REDACTIONS} />,
}

// ── outcomes: the card after each action ─────────────────────────────────

/** Stands in for the dashboard: answers the allow/revoke routes and, when a
 *  host is allowed, re-serves the reply the way the server does (the link
 *  restored, its record gone). */
function Outcome({ steps, allowed = false }: { steps: string[]; allowed?: boolean }) {
  const [restored, setRestored] = useState(allowed)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const a = api as unknown as Record<string, unknown>
    a.redactionAllowHost = async () => ({ ok: true, workspace: 'default' })
    a.redactionRevokeHost = async () => ({ ok: true, removed: true })
    window.open = (() => null) as typeof window.open
    const onChange = () => setRestored(r => !r)
    window.addEventListener('mc:redaction-hosts-changed', onChange)
    let i = 0
    const next = () => {
      if (i >= steps.length) return
      ref.current?.querySelector<HTMLElement>(steps[i++])?.click()
      setTimeout(next, 400)
    }
    const t = setTimeout(next, 100)
    return () => { clearTimeout(t); window.removeEventListener('mc:redaction-hosts-changed', onChange) }
  }, [steps])
  const content = restored ? REPLY.replace(LINK, `[the reviewers filter page](${REVIEWS_URL})`) : REPLY
  return (
    <div ref={ref} className="max-w-[900px] p-4 text-[14px] leading-6 text-text">
      <MarkdownRenderer content={content} redactions={REDACTIONS} blockedLinks={restored ? [] : BLOCKED} slotKey="story" messageTs="2026-09-25T00:00:00Z" />
    </div>
  )
}

const INSPECT = '[data-testid="blocked-link-inspect"]'
/** Open once, confirmed: the link opened in a new tab and stays blocked here. */
export const LinkOpenedOnce: StoryObj<typeof Outcome> = {
  render: () => <Outcome steps={[INSPECT, '[data-testid="blocked-link-open-once"]', '[data-testid="blocked-link-open-confirmed"]']} />,
}
/** Allow for this host, confirmed: the reply re-renders the host's link as a
 *  plain link and the card stays open with Undo. */
export const LinkAllowed: StoryObj<typeof Outcome> = {
  render: () => <Outcome steps={[INSPECT, '[data-testid="blocked-link-allow"]', '[data-testid="blocked-link-allow-confirmed"]']} />,
}
/** Undo after Allow: the host is blocked again. */
export const LinkAllowUndone: StoryObj<typeof Outcome> = {
  render: () => <Outcome steps={[INSPECT, '[data-testid="blocked-link-allow"]', '[data-testid="blocked-link-allow-confirmed"]', '[data-testid="blocked-link-undo"]']} />,
}

// ── Settings -> Security -> Redaction ────────────────────────────────────

function AllowedHosts({ workspaces }: { workspaces: Record<string, string[]> }) {
  const [client] = useState(() => {
    const stub = api as unknown as Record<string, unknown>
    stub.redactionAllowedHosts = async () => ({ workspaces })
    stub.credentialRedaction = async () => ({ enabled: true, changed_at: '' })
    return new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })
  return (
    <QueryClientProvider client={client}>
      <div className="max-w-[720px] p-4 text-text"><RedactionSection /></div>
    </QueryClientProvider>
  )
}
export const SettingsAllowedHosts: StoryObj<typeof AllowedHosts> = {
  render: () => <AllowedHosts workspaces={{ default: ['reviews.corp.example', 'dashboards.corp.example'], research: ['wiki.corp.example'] }} />,
}
export const SettingsNoAllowedHosts: StoryObj<typeof AllowedHosts> = {
  render: () => <AllowedHosts workspaces={{}} />,
}
