/**
 * Add-source dialog: the Bedrock KB branch.
 *
 * The sibling SourcesList suites cover the file and folder branches; this one
 * pins the bedrock_kb branch added with the remote-KB connector: the type
 * button, the required-field gating on the submit, the POST body shape (the
 * kb_ids/region/profile properties and no uri -- the server derives the
 * bedrock-kb:// handle from the validated config -- and nothing
 * credential-shaped), and the field reset after a successful add.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ComponentProps } from 'react'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import { api as clientApi } from '../api/client'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

// The submit gates on the consent query (shared with AwsConsentGate); vend a
// granted status so the form's ready-state is exercisable. The ungranted
// branch is pinned by the component logic itself (query disabled/absent =>
// submit stays disabled, as the first test's pre-consent assertions show).
const { grantedStatus } = vi.hoisted(() => ({
  grantedStatus: {
    service: 'bedrock-kb', serviceLabel: 'Amazon Bedrock KB', profile: '',
    credentialSource: 'default chain', identityResolved: true, granted: true,
    reason: '', revokedOnAccountChange: false,
    grant: { account: '000000000000', region: 'us-east-1', profile: '', granted_at: '2026-01-01T00:00:00Z' },
  },
}))
vi.mock('../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      awsConsent: vi.fn(async () => grantedStatus),
    },
  }
})

type ListProps = ComponentProps<typeof SourcesList>
type Handler = (path: string, opts?: RequestInit) => unknown

let handler: Handler = () => ({ ok: true })

function renderList(props: Partial<ListProps> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <SourcesList
        onIngest={vi.fn()}
        uploadNamespace=""
        setUploadNamespace={vi.fn()}
        namespaces={[]}
        ingestionJobs={[]}
        supportedFormatsDisplay=".md, .txt"
        {...props}
      />
    </QueryClientProvider>,
  )
  return { ...utils, queryClient }
}

async function openBedrockForm() {
  fireEvent.click(await screen.findByText('+ Add Source'))
  fireEvent.click(await screen.findByText('Bedrock Knowledge Base'))
}

const postCalls = () =>
  vi.mocked(api.knowledgeApi).mock.calls.filter(([p, opts]) =>
    p === '/sources' && (opts as RequestInit | undefined)?.method === 'POST')

beforeEach(() => {
  // Default routing: the list query needs an ARRAY (the component calls
  // .some on it); POST and everything else answer generically per test.
  handler = (path, opts) => {
    if (path === '/sources' && !(opts as RequestInit | undefined)?.method) return []
    return { ok: true }
  }
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(api.knowledgeApi).mockImplementation(async (path: string, opts?: RequestInit) =>
    handler(path, opts) as never)
  // A test may retarget the consent mock; every test starts from the
  // granted default.
  vi.mocked(clientApi.awsConsent).mockImplementation(async () => grantedStatus as never)
})

describe('SourcesList bedrock_kb branch', () => {
  it('offers the Bedrock KB type and gates submit on kb ids + region', async () => {
    renderList()
    await openBedrockForm()

    const submit = screen.getByText('Add Knowledge Base') as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    for (const label of [
      'Source name (optional)',
      'Knowledge Base IDs',
      'AWS region',
      'AWS profile (optional)',
    ]) {
      expect(screen.getByLabelText(label).className).toContain('focus-ring')
    }
    expect(screen.getByText('Cancel').className).toContain('font-body')
    expect(submit.className).toContain('font-body')

    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), {
      target: { value: 'KBTEST1234' },
    })
    expect(submit.disabled).toBe(true)
    // A filled form with a dead Add always says why: with the region still
    // empty, the reason is the region (neither the malformed-region hint nor
    // the consent reason applies yet).
    expect(screen.getByText('Enter the AWS region to add this source.')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('AWS region'), {
      target: { value: 'us-east-1' },
    })
    expect(screen.queryByText('Enter the AWS region to add this source.')).not.toBeInTheDocument()
    // Valid shape mounts the consent card and fires the (mocked, granted)
    // consent query; the submit frees only once that resolves.
    await waitFor(() => expect(submit.disabled).toBe(false))
    // The profile field says what blank means.
    expect(screen.getByText('Leave blank to use the default AWS credentials.')).toBeInTheDocument()
  })

  it('POSTs the kb properties without a uri, then resets the fields', async () => {
    handler = (path, opts) => {
      if (path === '/sources' && opts?.method === 'POST') return { id: 'new1' }
      if (path === '/sources') return []
      return { ok: true }
    }
    renderList()
    await openBedrockForm()

    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), {
      target: { value: 'KBTEST1234, arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KBPEER5678' },
    })
    fireEvent.change(screen.getByLabelText('AWS region'), {
      target: { value: 'us-east-1' },
    })
    fireEvent.change(screen.getByLabelText('AWS profile (optional)'), {
      target: { value: 'team-profile' },
    })
    // In a browser the click on Add blurs the profile field first; the blur
    // is what commits the profile the consent card and the POST both use.
    fireEvent.blur(screen.getByLabelText('AWS profile (optional)'))
    await waitFor(() =>
      expect((screen.getByText('Add Knowledge Base') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByText('Add Knowledge Base'))

    await waitFor(() => expect(postCalls()).toHaveLength(1))
    const body = JSON.parse(String((postCalls()[0][1] as RequestInit).body))
    expect(body).toEqual({
      name: 'Bedrock KB KBTEST1234',
      source_type: 'bedrock_kb',
      properties: {
        kb_ids: 'KBTEST1234,arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KBPEER5678',
        region: 'us-east-1',
        profile: 'team-profile',
      },
    })
    // No credential-shaped fields anywhere in the payload.
    expect(JSON.stringify(body)).not.toMatch(/secret|token|password/i)

    // The dialog closes and a reopened form starts blank.
    await waitFor(() =>
      expect(screen.queryByText('Add Knowledge Base')).not.toBeInTheDocument())
    await openBedrockForm()
    expect((screen.getByLabelText('Knowledge Base IDs') as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText('AWS region') as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText('AWS profile (optional)') as HTMLInputElement).value).toBe('')
  })

  it('keys a reopened form on its blank profile, so the first Add after an add submits', async () => {
    // The consent gate keys on the COMMITTED profile. A reset that clears the
    // field but keeps the committed value reopens the form gated on the
    // profile just added while the field reads blank: the Add reads ready,
    // and its first click is spent re-targeting the gate instead of saving.
    handler = (path, opts) => {
      if (path === '/sources' && opts?.method === 'POST') return { id: 'new1' }
      if (path === '/sources') return []
      return { ok: true }
    }
    renderList()
    await openBedrockForm()
    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), { target: { value: 'KBTEST1234' } })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
    fireEvent.change(screen.getByLabelText('AWS profile (optional)'), { target: { value: 'team-profile' } })
    fireEvent.blur(screen.getByLabelText('AWS profile (optional)'))
    await waitFor(() =>
      expect((screen.getByText('Add Knowledge Base') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByText('Add Knowledge Base'))
    await waitFor(() => expect(postCalls()).toHaveLength(1))
    await waitFor(() =>
      expect(screen.queryByText('Add Knowledge Base')).not.toBeInTheDocument())

    await openBedrockForm()
    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), { target: { value: 'KBPEER5678' } })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
    await waitFor(() =>
      expect(vi.mocked(clientApi.awsConsent).mock.lastCall?.[1]).toMatchObject({ profile: '' }))
    const submit = screen.getByText('Add Knowledge Base') as HTMLButtonElement
    await waitFor(() => expect(submit.disabled).toBe(false))
    fireEvent.click(submit)

    await waitFor(() => expect(postCalls()).toHaveLength(2))
    const body = JSON.parse(String((postCalls()[1][1] as RequestInit).body))
    expect(body.properties).toEqual({ kb_ids: 'KBPEER5678', region: 'us-east-1', profile: '' })
  })

  it('defaults the name to the bare KB id even when an ARN is pasted first', async () => {
    // A pasted ARN carries the account id. The name is a column every
    // authenticated caller can read off the sources list, so the default must
    // use the trailing id (the server's own ARN reduction), never the ARN.
    handler = (path, opts) => {
      if (path === '/sources' && opts?.method === 'POST') return { id: 'new2' }
      if (path === '/sources') return []
      return { ok: true }
    }
    renderList()
    await openBedrockForm()
    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), {
      target: { value: 'arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KBPEER5678, KBTEST1234' },
    })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
    await waitFor(() =>
      expect((screen.getByText('Add Knowledge Base') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByText('Add Knowledge Base'))

    await waitFor(() => expect(postCalls()).toHaveLength(1))
    const body = JSON.parse(String((postCalls()[0][1] as RequestInit).body))
    expect(body.name).toBe('Bedrock KB KBPEER5678')
    expect(body.name).not.toContain('123456789012')
    // The ids themselves still travel as typed; the server canonicalizes them.
    expect(body.properties.kb_ids).toBe(
      'arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KBPEER5678,KBTEST1234')
  })

  it('never POSTs a profile the consent gate did not probe', async () => {
    // Only the default chain (profile '') holds a grant. A profile typed and
    // not blurred leaves the live value diverged from the committed one the
    // gate was checked against; Add must re-target the gate to the typed
    // profile instead of submitting a target no grant covers.
    vi.mocked(clientApi.awsConsent).mockImplementation(async (_service, target) => ({
      service: 'bedrock-kb', serviceLabel: 'Amazon Bedrock KB', profile: target?.profile ?? '',
      credentialSource: 'default chain', region: 'us-east-1', account: '000000000000', arn: '',
      identityResolved: true, identityDetail: '', granted: (target?.profile ?? '') === '',
      reason: '', revokedOnAccountChange: false,
      grant: { account: '000000000000', region: 'us-east-1', profile: '', granted_at: '2026-01-01T00:00:00Z' },
    }) as never)
    renderList()
    await openBedrockForm()

    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), { target: { value: 'KBTEST1234' } })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
    const submit = screen.getByText('Add Knowledge Base') as HTMLButtonElement
    await waitFor(() => expect(submit.disabled).toBe(false))

    fireEvent.change(screen.getByLabelText('AWS profile (optional)'), { target: { value: 'team-profile' } })
    const scrolled = vi.fn()
    const originalScroll = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrolled
    try {
      fireEvent.click(submit)

      // The typed profile is now the gate's target: it holds no grant, so the
      // submit locks again behind its reason, and nothing was POSTed.
      await waitFor(() => expect(submit.disabled).toBe(true))
      const reason = screen.getByText('Confirm the AWS account above to add this source.')
      expect(reason).toBeInTheDocument()
      expect(postCalls()).toHaveLength(0)
      // The click added nothing, so it must answer loudly: focus lands on the
      // consent card, the card carries the ring, and the reason is a live
      // region (announced) in the warn tone rather than the muted hint tone.
      const card = screen.getByTestId('bedrock-kb-consent-card')
      expect(document.activeElement).toBe(card)
      expect(card.className).toContain('ring-2')
      expect(scrolled).toHaveBeenCalled()
      expect(reason).toHaveAttribute('role', 'status')
      expect(reason.className).toContain('text-warn')
    } finally {
      Element.prototype.scrollIntoView = originalScroll
    }
  })

  it('answers a pointer submit at pointerdown, before the blur can disable the button under the click', async () => {
    // A real pointer submit arrives as pointerdown -> blur -> click. The blur
    // commits the typed profile and disables the button, so a click-only
    // nudge would never fire; the intent has to be caught at pointerdown.
    vi.mocked(clientApi.awsConsent).mockImplementation(async (_service, target) => ({
      service: 'bedrock-kb', serviceLabel: 'Amazon Bedrock KB', profile: target?.profile ?? '',
      credentialSource: 'default chain', region: 'us-east-1', account: '000000000000', arn: '',
      identityResolved: true, identityDetail: '', granted: (target?.profile ?? '') === '',
      reason: '', revokedOnAccountChange: false,
      grant: { account: '000000000000', region: 'us-east-1', profile: '', granted_at: '2026-01-01T00:00:00Z' },
    }) as never)
    renderList()
    await openBedrockForm()
    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), { target: { value: 'KBTEST1234' } })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
    const submit = screen.getByText('Add Knowledge Base') as HTMLButtonElement
    await waitFor(() => expect(submit.disabled).toBe(false))

    const profileInput = screen.getByLabelText('AWS profile (optional)')
    profileInput.focus()
    fireEvent.change(profileInput, { target: { value: 'team-profile' } })
    const originalScroll = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = vi.fn()
    try {
      // pointerdown alone (no click yet): the typed profile becomes the gate's
      // target and the card rings; the default (focus move -> blur) is
      // prevented so the input keeps focus.
      const pointerDown = fireEvent.pointerDown(submit)
      expect(pointerDown).toBe(false) // default prevented
      await waitFor(() => expect(submit.disabled).toBe(true))
      expect(screen.getByTestId('bedrock-kb-consent-card').className).toContain('ring-2')
      expect(screen.getByText('Confirm the AWS account above to add this source.').className).toContain('text-warn')
      expect(postCalls()).toHaveLength(0)
      // A second pointerdown with nothing new typed is an ordinary press.
      expect(fireEvent.pointerDown(submit)).toBe(true)
    } finally {
      Element.prototype.scrollIntoView = originalScroll
    }
  })

  it('keeps an inert consent card while the typed profile differs from its committed target', async () => {
    // The card keys on the BLUR-committed profile. Between a keystroke and the
    // blur the field reads one profile and the card target names another. The
    // card stays mounted for continuity, but its pending row must expose no
    // old profile and no action that could confirm it.
    vi.mocked(clientApi.awsConsent).mockImplementation(async (_service, target) => ({
      service: 'bedrock-kb', serviceLabel: 'Amazon Bedrock KB', profile: target?.profile ?? '',
      credentialSource: target?.profile ? `profile ${target.profile}` : 'default chain',
      region: 'us-east-1', account: '000000000000', arn: '',
      identityResolved: true, identityDetail: '', granted: false,
      reason: '', revokedOnAccountChange: false, grant: null,
    }) as never)
    renderList()
    await openBedrockForm()
    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), { target: { value: 'KBTEST1234' } })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
    const profileInput = screen.getByLabelText('AWS profile (optional)')
    fireEvent.change(profileInput, { target: { value: 'team-a' } })
    fireEvent.blur(profileInput)
    // The committed profile's card names team-a and offers its Confirm.
    await screen.findByTestId('aws-consent-bedrock-kb-confirm')
    expect(screen.getByTestId('aws-consent-source').textContent).toBe('profile team-a')
    const callsBeforeEdit = vi.mocked(clientApi.awsConsent).mock.calls.length

    // Type on without blurring: the same card remains, but it is inert and
    // renders no data or action from the old target.
    fireEvent.change(profileInput, { target: { value: 'team-b' } })
    const pendingCard = screen.getByTestId('aws-consent-bedrock-kb')
    expect(pendingCard).toHaveAttribute('aria-disabled', 'true')
    // Same card, same title: the service label survives the edit.
    expect(screen.getByTestId('aws-consent-bedrock-kb-title').textContent).toBe('Amazon Bedrock KB')
    expect(screen.queryByTestId('aws-consent-bedrock-kb-confirm')).toBeNull()
    expect(screen.queryByText('profile team-a')).toBeNull()
    expect(vi.mocked(clientApi.awsConsent)).toHaveBeenCalledTimes(callsBeforeEdit)
    // The pending row says what brings Confirm back.
    expect(screen.getByTestId('aws-consent-bedrock-kb-pending-hint').textContent).toBe(
      'Finish editing the profile to confirm.',
    )

    // The commit re-targets the card to the profile the field reads.
    fireEvent.blur(profileInput)
    await waitFor(() => expect(screen.getByTestId('aws-consent-source').textContent).toBe('profile team-b'))
    expect(screen.getByTestId('aws-consent-bedrock-kb-confirm')).toBeInTheDocument()
    expect(screen.getByTestId('aws-consent-bedrock-kb')).not.toHaveAttribute('aria-disabled')
    expect(screen.queryByTestId('aws-consent-bedrock-kb-pending-hint')).toBeNull()
  })

  it('says what the confirmation bills and links the AWS price list', async () => {
    vi.mocked(clientApi.awsConsent).mockImplementation(async () => ({
      ...grantedStatus, region: 'us-east-1', account: '000000000000', arn: '',
      identityDetail: '', granted: false, grant: null,
    }) as never)
    renderList()
    await openBedrockForm()
    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), { target: { value: 'KBTEST1234' } })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
    // The cost sits in the confirm row itself, beside a labelled account.
    const short = await screen.findByTestId('aws-consent-billing-short')
    expect(short.textContent).toBe('Billed per search: one Bedrock Retrieve call per Knowledge Base')
    const accountLabel = screen.getByTestId('aws-consent-account').parentElement
    expect(accountLabel?.textContent?.startsWith('AWS account ')).toBe(true)
    expect(short.parentElement).toContainElement(accountLabel as HTMLElement)
    expect(screen.getByTestId('aws-consent-billing-detail')).toBeInTheDocument()
    const link = screen.getByText('AWS pricing') as HTMLAnchorElement
    expect(link.href).toBe('https://aws.amazon.com/bedrock/pricing/')
    expect(link.rel).toBe('noopener noreferrer')
  })

  it('keeps Add off after a one-account conflict until the target changes', async () => {
    handler = (path, opts) => {
      if (path === '/sources' && opts?.method === 'POST') {
        throw Object.assign(new Error('Team design KB already uses another profile; remove that source first'),
          { code: 'bedrock_kb_target_conflict' })
      }
      if (path === '/sources') return []
      return { ok: true }
    }
    renderList()
    await openBedrockForm()
    fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), { target: { value: 'KBTEST1234' } })
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
    const submit = () => screen.getByText('Add Knowledge Base') as HTMLButtonElement
    await waitFor(() => expect(submit().disabled).toBe(false))
    fireEvent.click(submit())
    await screen.findByText(/remove that source first/)
    // Same target: a second click would fail the same way.
    expect(submit().disabled).toBe(true)
    // Another region is another target, so Add comes back.
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-west-2' } })
    await waitFor(() => expect(submit().disabled).toBe(false))
  })

  it('turns Add back on for the same target once the conflicting source is removed', async () => {
    let otherSource = true
    handler = (path, opts) => {
      const method = (opts as RequestInit | undefined)?.method
      if (path === '/sources' && method === 'POST') {
        throw Object.assign(new Error('Team design KB already uses another profile; remove that source first'),
          { code: 'bedrock_kb_target_conflict' })
      }
      if (path === '/sources') {
        return otherSource
          ? [{ id: 'kb1', name: 'Team design KB', source_type: 'bedrock_kb', uri: 'bedrock-kb://us-east-1/KBOTHER123', sync_status: 'pending', item_count: 0 }]
          : []
      }
      if (path === '/sources/kb1' && method === 'DELETE') { otherSource = false; return { ok: true } }
      return { ok: true }
    }
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderList()
      await openBedrockForm()
      fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), { target: { value: 'KBTEST1234' } })
      fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } })
      const submit = () => screen.getByText('Add Knowledge Base') as HTMLButtonElement
      await waitFor(() => expect(submit().disabled).toBe(false))
      fireEvent.click(submit())
      await screen.findByText(/remove that source first/)
      expect(submit().disabled).toBe(true)

      // Same target, but the source it conflicted with is gone: the stale
      // refusal no longer holds Add off, and the backend rechecks on retry.
      fireEvent.click(screen.getByLabelText('Remove source'))
      await waitFor(() => expect(submit().disabled).toBe(false))
      expect(screen.queryByText(/remove that source first/)).toBeNull()
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('confirms a Live row removal without promising to delete ingested items', async () => {
    handler = (path, opts) => {
      if (path === '/sources' && !(opts as RequestInit | undefined)?.method) {
        return [
          { id: 'kb1', name: 'Team KB', source_type: 'bedrock_kb', uri: 'bedrock-kb://us-east-1/KBTEST1234', sync_status: 'pending', item_count: 0 },
          { id: 'f1', name: 'Notes', source_type: 'local_folder', uri: '/notes', sync_status: 'synced', item_count: 3 },
        ]
      }
      return { ok: true }
    }
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderList()
    const removes = await screen.findAllByLabelText('Remove source')
    expect(removes).toHaveLength(2)
    fireEvent.click(removes[0])
    fireEvent.click(removes[1])
    // A Live KB ingests nothing, so its confirm must not claim otherwise;
    // the folder row keeps the shared wording.
    expect(confirmSpy).toHaveBeenNthCalledWith(1,
      'Remove this knowledge base source? Nothing is stored locally; the knowledge base itself is not touched.')
    expect(confirmSpy).toHaveBeenNthCalledWith(2, 'Remove this source and all its ingested items?')
    confirmSpy.mockRestore()
  })

  it('restores a Live row and surfaces a failed removal', async () => {
    let failDelete = true
    handler = (path, opts) => {
      if (path === '/sources' && !opts?.method) {
        return [{
          id: 'kb1', name: 'Team KB', source_type: 'bedrock_kb',
          uri: 'bedrock-kb://us-east-1/KBTEST1234', sync_status: 'pending', item_count: 0,
        }]
      }
      if (path === '/sources/kb1' && opts?.method === 'DELETE') {
        if (failDelete) throw new Error('Unreadable attestation store')
        return new Promise(() => {})
      }
      return { ok: true }
    }
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderList()
      await screen.findByText('Team KB')

      fireEvent.click(screen.getByLabelText('Remove source'))

      const notice = await screen.findByTestId('source-delete-error')
      expect(screen.getByText('Team KB')).toBeInTheDocument()
      expect(notice).toHaveTextContent('Unreadable attestation store')
      expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()

      await openBedrockForm()
      fireEvent.change(screen.getByLabelText('Knowledge Base IDs'), {
        target: { value: 'UNSAVEDKB123' },
      })
      // Navigating to chat here would discard the add form's unsaved KB id.
      expect(screen.getByTestId('source-delete-error')).toHaveTextContent('Unreadable attestation store')
      expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()

      fireEvent.click(screen.getByText('Cancel'))
      failDelete = false
      fireEvent.click(screen.getByLabelText('Remove source'))
      await waitFor(() => expect(screen.queryByTestId('source-delete-error')).not.toBeInTheDocument())
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('keeps the file and folder branches reachable beside the new type', async () => {
    renderList()
    fireEvent.click(await screen.findByText('+ Add Source'))
    expect(await screen.findByText('Files and folders')).toBeInTheDocument()
    expect(screen.getByText('Web Page')).toBeInTheDocument()
    expect(screen.getByText('Bedrock Knowledge Base')).toBeInTheDocument()
  })

  it('renders a connected KB row as Live — no sync affordances', async () => {
    handler = (path, opts) => {
      if (path === '/sources' && !(opts as RequestInit | undefined)?.method) {
        return [{
          id: 'kb1', name: 'Team KB', source_type: 'bedrock_kb',
          uri: 'bedrock-kb://us-east-1/KBTEST1234', sync_status: 'pending', item_count: 0,
        }]
      }
      return { ok: true }
    }
    renderList()
    // The meta chip labels the mode Live (the status badge is deliberately
    // absent: it asserted health nothing checks); dead-row signals are absent.
    expect((await screen.findAllByText(/live/i)).length).toBeGreaterThanOrEqual(1)
    // Plain "Live", no dot: a grey status-style dot read as "off" (UX review).
    const chip = screen.getByText('Live')
    expect(chip.className).not.toMatch(/(^|\s)(inline-)?flex(\s|$)/)
    expect(chip.querySelector('svg')).toBeNull()
    const sourceIcon = screen.getByTestId('bedrock-kb-source-icon')
    expect(sourceIcon.classList.contains('lucide-database')).toBe(true)
    expect(document.querySelector('.lucide-folder-sync')).toBeNull()
    expect(screen.queryByText('pending')).not.toBeInTheDocument()
    expect(screen.queryByText(/0 items/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Sync source')).not.toBeInTheDocument()
    // What "Live" means is visible text on the row, not a hover title: the
    // row has no items, badge or Sync, and the word alone does not explain it.
    const meaning = screen.getByTestId('bedrock-kb-live-meaning')
    expect(meaning.textContent).toMatch(/queried live from your aws account/i)
    expect(meaning.className).not.toMatch(/sr-only/)
  })

  // Every wait below sits behind a chained query: the sources list loads,
  // each Bedrock KB row's consent query answers, and that answer re-reads
  // the sources list before the chip settles. A loaded worker can take
  // longer than Testing Library's 1000 ms default for that chain.
  const CHAIN = { timeout: 5000 }

  // The chip says "Live" only while the consent query (the one the
  // add form polls) reports the row's own (profile, region) as granted.
  const savedKbRow = {
    id: 'kb1', name: 'Team KB', source_type: 'bedrock_kb',
    uri: 'bedrock-kb://us-west-2/KBTEST1234', sync_status: 'pending', item_count: 0,
    properties: JSON.stringify({ kb_ids: 'KBTEST1234', region: 'us-west-2', profile: 'team' }),
  }
  const listOnly = (row: unknown): Handler => (path, opts) =>
    path === '/sources' && !(opts as RequestInit | undefined)?.method ? [row] : { ok: true }

  it('keeps Live for a granted row, asking about that row\u2019s own target', async () => {
    handler = listOnly(savedKbRow)
    renderList()
    await waitFor(() => expect(clientApi.awsConsent).toHaveBeenCalledWith(
      'bedrock-kb', { profile: 'team', region: 'us-west-2' }), CHAIN)
    expect(await screen.findByText('Live', undefined, CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('bedrock-kb-grant-revoked')).not.toBeInTheDocument()
  })

  it('shows Grant revoked when the grant is gone, and leads back to the consent card', async () => {
    vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
      ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted: false, grant: null }) as never)
    handler = listOnly(savedKbRow)
    renderList()
    const revoked = await screen.findByTestId('bedrock-kb-grant-revoked', undefined, CHAIN)
    expect(revoked).toHaveTextContent('Not enabled · Review AWS account')
    expect(revoked.tagName).toBe('BUTTON')
    expect(screen.queryByText('Live')).not.toBeInTheDocument()

    fireEvent.click(revoked)
    // The add form opens on the Bedrock KB branch, targeted at this row.
    expect((screen.getByLabelText('AWS region') as HTMLInputElement).value).toBe('us-west-2')
    expect((screen.getByLabelText('AWS profile (optional)') as HTMLInputElement).value).toBe('team')
    expect(await screen.findByTestId('bedrock-kb-consent-card', undefined, CHAIN)).toBeInTheDocument()
  })

  it('asks to remove and re-add a granted row the gateway reports unsearchable (profile moved to another account)', async () => {
    vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
      ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted: true }) as never)
    handler = listOnly({ ...savedKbRow, searchable: false, searchable_reason: 'other_account' })
    renderList()
    const chip = await screen.findByTestId('bedrock-kb-needs-readd', undefined, CHAIN)
    expect(chip).toHaveTextContent('Added under a different AWS account, so searches skip it. Remove it with ✕ and add it again.')
    // Confirming again cannot bring this row back, so it is not offered.
    expect(screen.queryByTestId('bedrock-kb-grant-revoked')).not.toBeInTheDocument()
    expect(screen.queryByText('Live')).not.toBeInTheDocument()
  })

  it('reads the sources list again when the row\u2019s grant changes, so searchable describes the same grant', async () => {
    let granted = false
    vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
      ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted,
        grant: granted ? { ...grantedStatus.grant, granted_at: '2026-11-05T00:00:00Z' } : null }) as never)
    // No grant at the first list fetch: the gateway computed searchable=false.
    // After the grant is restored it answers searchable=true.
    handler = (path, opts) => {
      if (path === '/sources' && !(opts as RequestInit | undefined)?.method) {
        return [{ ...savedKbRow, searchable: granted, searchable_reason: 'other_account' }]
      }
      return { ok: true }
    }
    const { queryClient } = renderList()
    await screen.findByTestId('bedrock-kb-grant-revoked', undefined, CHAIN)
    granted = true
    await queryClient.invalidateQueries({ queryKey: ['awsConsent', 'bedrock-kb'] })
    // Settled state: the list was read again for the restored grant.
    await waitFor(() => expect(vi.mocked(api.knowledgeApi).mock.calls.filter(([p, o]) =>
      p === '/sources' && !(o as RequestInit | undefined)?.method).length).toBeGreaterThanOrEqual(3), CHAIN)
    await waitFor(() => expect(queryClient.isFetching()).toBe(0), CHAIN)
    expect(await screen.findByText('Live', undefined, CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('bedrock-kb-needs-readd')).not.toBeInTheDocument()
  })

  it('reads the sources list again on the first consent answer, so a list older than the grant is not trusted', async () => {
    // The cached list predates a grant restored elsewhere: its first answer
    // says not searchable, every later one says searchable.
    let listFetches = 0
    handler = (path, opts) => {
      if (path === '/sources' && !(opts as RequestInit | undefined)?.method) {
        listFetches += 1
        return [{ ...savedKbRow, searchable: listFetches > 1, searchable_reason: 'other_account' }]
      }
      return { ok: true }
    }
    renderList()
    // The stale list must never surface its warning, not even for a frame
    // before the second read lands.
    const seenWarning = { value: false }
    const observer = new MutationObserver(() => {
      if (document.querySelector('[data-testid="bedrock-kb-needs-readd"]')) seenWarning.value = true
    })
    observer.observe(document.body, { childList: true, subtree: true })
    try {
      await waitFor(() => expect(listFetches).toBeGreaterThan(1), CHAIN)
      await waitFor(() => expect(screen.getByText('Live')).toBeInTheDocument(), CHAIN)
      expect(seenWarning.value).toBe(false)
      expect(screen.queryByTestId('bedrock-kb-needs-readd')).not.toBeInTheDocument()
    } finally {
      observer.disconnect()
    }
  })

  it('keeps Remove and add again on screen through a background refetch with the same grant', async () => {
    vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
      ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted: true }) as never)
    // Later reads hang until released, so the refetch is visibly in flight.
    let release: () => void = () => {}
    let hold = false
    handler = (path, opts) => {
      if (path === '/sources' && !(opts as RequestInit | undefined)?.method) {
        const rows = [{ ...savedKbRow, searchable: false, searchable_reason: 'other_account' }]
        return hold ? new Promise(r => { release = () => r(rows) }) : rows
      }
      return { ok: true }
    }
    const { queryClient } = renderList()
    await screen.findByTestId('bedrock-kb-needs-readd', undefined, CHAIN)
    await waitFor(() => expect(queryClient.isFetching()).toBe(0), CHAIN)
    hold = true
    let flippedToLive = false
    const observer = new MutationObserver(() => {
      if (!document.querySelector('[data-testid="bedrock-kb-needs-readd"]')) flippedToLive = true
    })
    observer.observe(document.body, { childList: true, subtree: true, characterData: true })
    try {
      const refetch = queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] })
      await waitFor(() => expect(queryClient.isFetching()).toBeGreaterThan(0), CHAIN)
      // Let React commit the in-flight render before releasing the read.
      await act(async () => { await Promise.resolve() })
      expect(screen.queryByTestId('bedrock-kb-needs-readd')).toBeInTheDocument()
      release()
      await refetch
      await waitFor(() => expect(queryClient.isFetching()).toBe(0), CHAIN)
      expect(flippedToLive).toBe(false)
      expect(screen.getByTestId('bedrock-kb-needs-readd')).toBeInTheDocument()
    } finally {
      observer.disconnect()
    }
  })

  it('does not trust a stale searchable flag when the re-read fails; it says the check failed', async () => {
    vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
      ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted: true }) as never)
    let listFetches = 0
    handler = (path, opts) => {
      if (path === '/sources' && !(opts as RequestInit | undefined)?.method) {
        listFetches += 1
        if (listFetches > 1) throw new Error('gateway down')
        return [{ ...savedKbRow, searchable: false, searchable_reason: 'other_account' }]
      }
      return { ok: true }
    }
    renderList()
    expect(await screen.findByTestId('bedrock-kb-grant-check-error', undefined, CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('bedrock-kb-needs-readd')).not.toBeInTheDocument()
    expect(screen.getByText('Live')).toBeInTheDocument()
  })

  it('does not trust a stale flag when the stale read and the grant share a millisecond', async () => {
    // Every clock read returns the same instant, so a timestamp cannot tell
    // the list read before the grant from one after it.
    const now = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000)
    try {
      vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
        ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted: true }) as never)
      let listFetches = 0
      handler = (path, opts) => {
        if (path === '/sources' && !(opts as RequestInit | undefined)?.method) {
          listFetches += 1
          if (listFetches > 1) throw new Error('gateway down')
          return [{ ...savedKbRow, searchable: false, searchable_reason: 'other_account' }]
        }
        return { ok: true }
      }
      renderList()
      expect(await screen.findByTestId('bedrock-kb-grant-check-error', undefined, CHAIN)).toBeInTheDocument()
      expect(screen.queryByTestId('bedrock-kb-needs-readd')).not.toBeInTheDocument()
    } finally {
      now.mockRestore()
    }
  })

  it('shows Remove and add again after an identical re-read in the same millisecond', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000)
    try {
      vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
        ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted: true }) as never)
      handler = listOnly({ ...savedKbRow, searchable: false, searchable_reason: 'other_account' })
      renderList()
      expect(await screen.findByTestId('bedrock-kb-needs-readd', undefined, CHAIN)).toBeInTheDocument()
    } finally {
      now.mockRestore()
    }
  })

  it('does not count a cache write (an optimistic removal or its rollback) as a fresh read', async () => {
    vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
      ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted: true }) as never)
    let listFetches = 0
    const rows = [{ ...savedKbRow, searchable: false, searchable_reason: 'other_account' }]
    handler = (path, opts) => {
      if (path === '/sources' && !(opts as RequestInit | undefined)?.method) {
        listFetches += 1
        if (listFetches > 1) throw new Error('gateway down')
        return rows
      }
      return { ok: true }
    }
    const { queryClient } = renderList()
    expect(await screen.findByTestId('bedrock-kb-grant-check-error', undefined, CHAIN)).toBeInTheDocument()
    act(() => { queryClient.setQueryData(['knowledge-sources'], [...rows]) })
    act(() => { queryClient.setQueryData(['knowledge-sources'], rows) })
    expect(screen.queryByTestId('bedrock-kb-needs-readd')).not.toBeInTheDocument()
    expect(screen.getByTestId('bedrock-kb-grant-check-error')).toBeInTheDocument()
  })

  it('offers no re-add for an unsearchable row the gateway never registered', async () => {
    vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
      ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted: true }) as never)
    handler = listOnly({ ...savedKbRow, searchable: false, searchable_reason: 'unknown' })
    const { queryClient } = renderList()
    await waitFor(() => expect(clientApi.awsConsent).toHaveBeenCalled(), CHAIN)
    await waitFor(() => expect(queryClient.isFetching()).toBe(0), CHAIN)
    expect(await screen.findByText('Live', undefined, CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('bedrock-kb-needs-readd')).not.toBeInTheDocument()
  })

  it('keeps Live for a granted row the gateway reports searchable', async () => {
    handler = listOnly({ ...savedKbRow, searchable: true })
    renderList()
    await waitFor(() => expect(clientApi.awsConsent).toHaveBeenCalled(), CHAIN)
    expect(await screen.findByText('Live', undefined, CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('bedrock-kb-needs-readd')).not.toBeInTheDocument()
  })

  it('keeps Live while the consent read fails: an unanswered read says nothing about the grant', async () => {
    vi.mocked(clientApi.awsConsent).mockImplementation(async () => { throw new Error('offline') })
    handler = listOnly(savedKbRow)
    renderList()
    await waitFor(() => expect(clientApi.awsConsent).toHaveBeenCalled(), CHAIN)
    expect(await screen.findByText('Live', undefined, CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('bedrock-kb-grant-revoked')).not.toBeInTheDocument()
    // The failure is said, not swallowed.
    expect(await screen.findByTestId('bedrock-kb-grant-check-error', undefined, CHAIN))
      .toHaveTextContent("Couldn't check the AWS grant for this knowledge base.")
  })

  it('focuses the consent card again on a second Grant revoked click', async () => {
    vi.mocked(clientApi.awsConsent).mockImplementation(async () =>
      ({ ...grantedStatus, profile: 'team', region: 'us-west-2', granted: false, grant: null }) as never)
    handler = listOnly(savedKbRow)
    renderList()
    fireEvent.click(await screen.findByTestId('bedrock-kb-grant-revoked', undefined, CHAIN))
    const card = await screen.findByTestId('bedrock-kb-consent-card', undefined, CHAIN)
    await waitFor(() => expect(document.activeElement).toBe(card), CHAIN)
    fireEvent.click(screen.getByText('Cancel'))
    fireEvent.click(screen.getByTestId('bedrock-kb-grant-revoked'))
    const again = await screen.findByTestId('bedrock-kb-consent-card', undefined, CHAIN)
    await waitFor(() => expect(document.activeElement).toBe(again), CHAIN)
  })
})
