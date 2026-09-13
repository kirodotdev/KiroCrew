/**
 * WebAppArtifactCard's "Copy URL" button must run the deployed public URL
 * through safeHttpUrl BEFORE copying (an unsafe URL never reaches the
 * clipboard) and must gate its check-icon confirmation on the shared
 * clipboard helper's returned boolean, never on the write having merely been
 * attempted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactElement } from 'react'
import WebAppArtifactCard from '../components/WebAppArtifactCard'
import type { Artifact } from '../types'

vi.mock('../api/client', () => ({
  api: {
    artifactTeardown: vi.fn(),
  },
}))

vi.mock('../utils/clipboard', () => ({
  // Resolves TRUE by default: the real signature is `Promise<boolean>`, and a
  // bare `vi.fn()` returning undefined is falsy, so it could not tell a
  // successful clipboard write from a failed one.
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

import { copyToClipboard } from '../utils/clipboard'

function makeArtifact(overrides?: Partial<Artifact>): Artifact {
  return {
    slug: 'kanban-demo',
    name: 'Kanban Demo',
    kind: 'webapp',
    source: 'chat',
    description: 'A kanban board deployed to AWS',
    tags: ['deploy'],
    version: 1,
    created_at: '2026-07-10T10:00:00Z',
    updated_at: '2026-07-10T10:00:00Z',
    webapp_metadata: {
      slug: 'kanban-demo',
      origin_session: 'abc123',
      deploy_target: {
        provider: 'aws',
        account: '123456789012',
        region: 'us-west-2',
        public_url: 'https://d2nzmpzyp0popu.cloudfront.net/kanban/',
        profile: 'my-deploy',
      },
      architecture: {
        tier: '3',
        frontend: 'CloudFront -> S3 (private, OAC)',
        backend: 'API Gateway HTTP API -> Lambda',
        state: 'DynamoDB (IAM scoped to table)',
        resources: [],
      },
      lifecycle: {
        created_at: new Date(Date.now() - 24 * 3600e3).toISOString(),
        expires_at: new Date(Date.now() + 48 * 3600e3).toISOString(),
        persistent: false,
        ttl_hours: 72,
        status: 'live',
      },
      cost: {
        model: 'ttl-window',
        window_hours: 72,
        estimates: [{ views: 100, usd: 0.0009 }],
        idle_usd: 0,
        note: 'estimate, not the AWS bill',
      },
      teardown: { method: 'reaper-lambda', handle: 'kanban-demo', reversible: false },
    },
    ...overrides,
  }
}

function renderWithClient(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(copyToClipboard).mockReset().mockResolvedValue(true)
})

describe('WebAppArtifactCard — Copy URL affordance', () => {
  it('copies the validated public URL and confirms only once it actually lands', async () => {
    renderWithClient(<WebAppArtifactCard artifact={makeArtifact()} />)
    const copyBtn = screen.getByRole('button', { name: 'Copy URL' })
    fireEvent.click(copyBtn)
    await waitFor(() =>
      expect(copyToClipboard).toHaveBeenCalledWith('https://d2nzmpzyp0popu.cloudfront.net/kanban/'),
    )
    await waitFor(() => expect(copyBtn.querySelector('.lucide-check')).not.toBeNull())
  })

  it('shows no confirmation when the clipboard write fails outright', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    renderWithClient(<WebAppArtifactCard artifact={makeArtifact()} />)
    const copyBtn = screen.getByRole('button', { name: 'Copy URL' })
    fireEvent.click(copyBtn)
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalled())
    expect(copyBtn.querySelector('.lucide-check')).toBeNull()
  })

  it('never reaches the clipboard for a public_url safeHttpUrl rejects', async () => {
    // javascript: is not an http(s) scheme — safeHttpUrl must return null,
    // and handleCopy must never call the clipboard helper with unsafe input.
    renderWithClient(
      <WebAppArtifactCard
        artifact={makeArtifact({
          webapp_metadata: {
            ...makeArtifact().webapp_metadata!,
            deploy_target: {
              ...makeArtifact().webapp_metadata!.deploy_target,
              public_url: 'javascript:alert(1)',
            },
          },
        })}
      />,
    )
    const copyBtn = screen.getByRole('button', { name: 'Copy URL' })
    fireEvent.click(copyBtn)
    // Give any microtask a chance to run before asserting the negative.
    await new Promise((r) => setTimeout(r, 0))
    expect(copyToClipboard).not.toHaveBeenCalled()
    expect(copyBtn.querySelector('.lucide-check')).toBeNull()
  })
})
