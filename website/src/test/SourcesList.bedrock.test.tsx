/**
 * Add-source dialog: the Bedrock KB branch.
 *
 * The sibling SourcesList suites cover the file and folder branches; this one
 * pins the bedrock_kb branch added with the remote-KB connector: the type
 * button, the required-field gating on the submit, the POST body shape (the
 * derived bedrock-kb:// uri and the kb_ids/region/profile properties -- no
 * credentials), and the field reset after a successful add.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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

  it('POSTs the derived uri and kb properties, then resets the fields', async () => {
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
      uri: 'bedrock-kb://us-east-1/KBTEST1234',
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

  it('keeps the file and folder branches reachable beside the new type', async () => {
    renderList()
    fireEvent.click(await screen.findByText('+ Add Source'))
    expect(await screen.findByText('Local File')).toBeInTheDocument()
    expect(screen.getByText('Local Folder')).toBeInTheDocument()
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
    expect(screen.queryByText('pending')).not.toBeInTheDocument()
    expect(screen.queryByText(/0 items/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Sync source')).not.toBeInTheDocument()
    // What "Live" means is visible text on the row, not a hover title: the
    // row has no items, badge or Sync, and the word alone does not explain it.
    const meaning = screen.getByTestId('bedrock-kb-live-meaning')
    expect(meaning.textContent).toMatch(/queried live from your aws account/i)
    expect(meaning.className).not.toMatch(/sr-only/)
  })

  it('labels the folder branch the same way as the Bedrock branch', async () => {
    renderList()
    fireEvent.click(await screen.findByText('+ Add Source'))
    fireEvent.click(await screen.findByText('Local Folder'))
    // Visible <label htmlFor> on every text input, so switching type does
    // not flip the dialog between labelled and placeholder-only fields.
    for (const name of ['Source name (optional)', 'Folder path', 'Ignore patterns']) {
      const input = screen.getByLabelText(name)
      const label = document.querySelector(`label[for="${input.id}"]`)
      expect(label, `${name} has a visible label`).not.toBeNull()
      expect(label?.textContent).toBe(name)
    }
    // The name input's label is its only hint (no placeholder repeating it).
    expect((screen.getByLabelText('Source name (optional)') as HTMLInputElement).placeholder).toBe('')
  })
})
