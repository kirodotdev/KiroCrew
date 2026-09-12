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

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

// The submit gates on the consent query (shared with AwsConsentGate); vend a
// granted status so the form's ready-state is exercisable. The ungranted
// branch is pinned by the component logic itself (query disabled/absent =>
// submit stays disabled, as the first test's pre-consent assertions show).
vi.mock('../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      awsConsent: vi.fn(async () => ({
        service: 'bedrock-kb', serviceLabel: 'Amazon Bedrock KB', profile: '',
        credentialSource: 'default chain', identityResolved: true, granted: true,
        reason: '', revokedOnAccountChange: false,
        grant: { account: '000000000000', region: 'us-east-1', profile: '', granted_at: '2026-01-01T00:00:00Z' },
      })),
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

    fireEvent.change(screen.getByLabelText('AWS region'), {
      target: { value: 'us-east-1' },
    })
    // Valid shape mounts the consent card and fires the (mocked, granted)
    // consent query; the submit frees only once that resolves.
    await waitFor(() => expect(submit.disabled).toBe(false))
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
    fireEvent.change(screen.getByLabelText('AWS profile'), {
      target: { value: 'team-profile' },
    })
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
    expect((screen.getByLabelText('AWS profile') as HTMLInputElement).value).toBe('')
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
  })
})
