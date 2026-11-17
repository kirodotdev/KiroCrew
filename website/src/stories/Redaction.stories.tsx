import type { Meta, StoryObj } from '@storybook/react-vite'
import { useEffect, useRef, useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { RedactionSection } from '../pages/settings/SecurityPanel'

/**
 * The redaction UI, reproducing the RFC prototype's conversation
 * (docs/request-for-change/assets/redaction-explain-reveal-prototype.html,
 * variants C then A): a credentials file with three removed values and the
 * first-run coach. Stories that show a card open it by clicking its marker,
 * the same click a reader makes.
 */

const CRED = '[REDACTED: credential]'

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

/** Renders the reply, then clicks `selector` (the n-th match) so a card is open. */
function Reply({ click, nth = 0, then, coach = false }: {
  click?: string
  nth?: number
  then?: string
  coach?: boolean
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
      <MarkdownRenderer content={REPLY} redactions={REDACTIONS} slotKey="story" redactionCoach={coach} />
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
/** Variant A: later replies carry only the lock tags. */
export const InlineMarkers: Story = { args: {} }
export const CredentialCard: Story = { args: { click: 'credential-tag', nth: 0 } }
export const SessionTokenCard: Story = { args: { click: 'credential-tag', nth: 2 } }
export const CredentialMoreWays: Story = { args: { click: 'credential-tag', nth: 0, then: '[data-testid="credential-more"] > button' } }

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
      <MarkdownRenderer content={REPLY} redactions={redactions} slotKey="story" />
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

// ── Settings -> Security -> Redaction ────────────────────────────────────

function RedactionSettings({ state }: { state: { enabled: boolean; changed_at: string; locked_by_policy: boolean } }) {
  const [client] = useState(() => {
    const stub = api as unknown as Record<string, unknown>
    stub.credentialRedaction = async () => state
    return new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })
  return (
    <QueryClientProvider client={client}>
      <div className="max-w-[720px] p-4 text-text"><RedactionSection /></div>
    </QueryClientProvider>
  )
}
export const SettingsRedactionOff: StoryObj<typeof RedactionSettings> = {
  render: () => <RedactionSettings state={{ enabled: false, changed_at: '', locked_by_policy: false }} />,
}
export const SettingsRedactionLocked: StoryObj<typeof RedactionSettings> = {
  render: () => <RedactionSettings state={{ enabled: true, changed_at: '', locked_by_policy: true }} />,
}
