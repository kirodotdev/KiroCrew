import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'

// Mock data for various endpoints
export const mockMemorySettings = {
  history_idle_hours: 24,
  history_max_days: 30,
  auto_consolidate: true,
}

export const mockMemoryPreferences = {
  content: '# My Preferences\n\n- Use TypeScript\n- Follow Amazon style guide',
  last_updated: '2026-03-20T10:00:00Z',
}

export const mockMemoryProjects = {
  content: '# Projects\n\n## KiroCrew\n\nAI-powered development assistant.',
  last_updated: '2026-03-20T10:00:00Z',
}

export const mockMemoryHistory = {
  content: '# Today\n\n- Worked on integration tests\n- Fixed memory consolidation',
  last_updated: '2026-03-20T10:00:00Z',
}

export const mockCrons = [
  {
    id: 'cron-1',
    name: 'Check system status',
    rule: 'Check system status',
    message: 'Check system health',
    schedule: '*/5 * * * *',
    enabled: true,
    last_run: '2026-03-20T10:00:00Z',
    next_run: '2026-03-20T10:05:00Z',
    last_status: '',
  },
  {
    id: 'cron-2',
    name: 'Backup database',
    rule: 'Backup database',
    message: 'Run backup script',
    schedule: '0 2 * * *',
    enabled: false,
    last_run: '2026-03-19T02:00:00Z',
    next_run: null,
    last_status: '',
  },
]

export const mockSkills = [
  {
    name: 'amazon-writing',
    key: 'amazon-writing',
    description: 'Amazon writing guidelines for documents',
    always: true,
    source: 'kirocrew',
    dir: '/path/to/skills/amazon-writing',
  },
  {
    name: 'code-search',
    key: 'code-search',
    description: 'Search code in Amazon repositories',
    always: false,
    source: 'kirocrew',
    dir: '/path/to/skills/code-search',
  },
]

export const mockSkillDetail = {
  name: 'amazon-writing',
  key: 'amazon-writing',
  content: '---\nname: amazon-writing\ndescription: Amazon writing guidelines for documents\nalways: true\ntriggers: writing, docs, narrative\ntags: [skill, writing, amazon]\n---\n# Amazon Writing\n\nGuidelines for clear technical writing.',
  description: 'Amazon writing guidelines for documents',
  always: true,
  source: 'kirocrew',
  dir: '/path/to/skills/amazon-writing',
}

export const mockCodeSearchDetail = {
  name: 'code-search',
  key: 'code-search',
  content: '---\nname: code-search\ndescription: Search code in Amazon repositories\ntags: [skill, code, search]\n---\n# Code Search\n\nSearch Amazon code repositories.',
  description: 'Search code in Amazon repositories',
  always: false,
  source: 'kirocrew',
  dir: '/path/to/skills/code-search',
}

export const mockMcpServers = [
  {
    name: 'builder-mcp',
    status: 'ok',
    enabled: true,
    tools: ['ReadInternalWebsites', 'TaskeiGetTask'],
    disabledTools: [],
    command: 'node /path/to/builder-mcp',
    presence: { kirocrew: true, kiroGlobal: true, ccGlobal: false },
  },
  {
    name: 'ai-community-slack-mcp',
    status: 'ok',
    enabled: false,
    tools: ['search', 'post_message'],
    disabledTools: ['post_message'],
    command: 'node /path/to/slack-mcp',
    presence: { kirocrew: false, kiroGlobal: false, ccGlobal: false },
  },
]

export const mockHooks = [
  {
    id: 'hook-1',
    name: 'Deploy Notifier',
    event: 'UserPromptSubmit',
    matcher: '*deploy*',
    command: 'echo "Deploy started"',
    timeout: 30,
    enabled: true,
    last_run: Math.floor(Date.now() / 1000) - 3600,
    last_status: 'ok',
    run_count: 15,
  },
]

export const mockMemoryGraph = {
  nodes: [
    { id: 'p1', label: 'Use TypeScript', group: 'preference', title: 'Use TypeScript' },
    { id: 'p2', label: 'Follow Amazon style guide', group: 'preference', title: 'Follow Amazon style guide' },
    { id: 'pr1', label: 'KiroCrew', group: 'project', title: 'KiroCrew' },
    { id: 'pr2', label: 'KiroCrew: AI-powered assistant', group: 'project', title: 'AI-powered development assistant' },
    { id: 's1', label: 'pref.editor', group: 'semantic', title: 'pref.editor = vim' },
    { id: 'l1', label: 'Check CRs before building', group: 'lesson', title: 'Always check for existing open CRs' },
    { id: 'h1', label: '2026-03-25', group: 'history', title: '2026-03-25 work session' },
  ],
  edges: [
    { from: 'pr1', to: 'pr2' },
    { from: 'l1', to: 'pr1' },
  ],
}

// Request handlers
export const handlers = [
  // Memory endpoints
  http.get('/api/memory/settings', () => {
    return HttpResponse.json(mockMemorySettings)
  }),
  
  http.put('/api/memory/settings', async ({ request }) => {
    const body = await request.json()
    return HttpResponse.json({ ...mockMemorySettings, ...body })
  }),
  
  http.get('/api/memory/preferences', () => {
    return HttpResponse.json(mockMemoryPreferences)
  }),
  
  http.put('/api/memory/preferences', async ({ request }) => {
    const body = await request.json() as { content: string }
    return HttpResponse.json({ ...mockMemoryPreferences, content: body.content })
  }),
  
  http.get('/api/memory/projects', () => {
    return HttpResponse.json(mockMemoryProjects)
  }),
  
  http.put('/api/memory/projects', async ({ request }) => {
    const body = await request.json() as { content: string }
    return HttpResponse.json({ ...mockMemoryProjects, content: body.content })
  }),
  
  http.get('/api/memory/history', () => {
    return HttpResponse.json(mockMemoryHistory)
  }),
  
  http.put('/api/memory/history', async ({ request }) => {
    const body = await request.json() as { content: string }
    return HttpResponse.json({ ...mockMemoryHistory, content: body.content })
  }),
  
  http.post('/api/memory/consolidate', async ({ request }) => {
    const body = await request.json() as { key: string; include_history: boolean }
    return HttpResponse.json({ success: true, key: body.key })
  }),
  
  http.get('/api/memory/stats', () => {
    return HttpResponse.json({
      total_entries: 42,
      total_size_bytes: 102400,
      oldest_entry: '2026-01-01T00:00:00Z',
      newest_entry: '2026-03-20T10:00:00Z',
    })
  }),
  
  http.get('/api/memory/embedding-status', () => {
    return HttpResponse.json({
      enabled: false,
      model: null,
      total_embeddings: 0,
    })
  }),
  
  http.get('/api/memory/semantic', () => {
    return HttpResponse.json({
      entries: [],
      total: 0,
    })
  }),

  http.get('/api/memory/graph', () => {
    return HttpResponse.json(mockMemoryGraph)
  }),
  
  // Cron endpoints
  http.get('/api/cron-folders', () => {
    return HttpResponse.json([])
  }),

  http.get('/api/crons', () => {
    return HttpResponse.json({ jobs: mockCrons })
  }),
  
  http.post('/api/crons', async ({ request }) => {
    const body = await request.json() as { name: string; message: string; every?: number; cron?: string; agent?: string }
    const newCron = {
      id: `cron-${Date.now()}`,
      name: body.name,
      rule: body.name,
      message: body.message,
      schedule: body.cron || `every ${body.every}s`,
      enabled: true,
      last_run: null,
      next_run: null,
      last_status: '',
      agent: body.agent || '',
    }
    return HttpResponse.json(newCron, { status: 201 })
  }),
  
  http.delete('/api/crons/:id', () => {
    return HttpResponse.json({ success: true })
  }),
  
  http.post('/api/crons/:id/enable', async ({ request }) => {
    const body = await request.json() as { enabled: boolean }
    return HttpResponse.json({ success: true, enabled: body.enabled })
  }),
  
  // Skills endpoints
  http.get('/api/skills', () => {
    return HttpResponse.json(mockSkills)
  }),
  
  http.get('/api/skills/:name', ({ params }) => {
    const name = params.name as string
    if (name === 'code-search') return HttpResponse.json(mockCodeSearchDetail)
    return HttpResponse.json(mockSkillDetail)
  }),

  // Directory browser: SKILL.md is the only file in each fixture skill,
  // mirroring the typical case for a flat-layout skill.  Routes use the
  // ``/-/`` separator (GitLab-style) so a skill named e.g. ``utils/tree``
  // can't collide with the browser endpoint.
  http.get('/api/skills/:name/-/tree', ({ params }) => {
    const name = params.name as string
    return HttpResponse.json({
      name,
      root: `/path/to/skills/${name}`,
      entries: [{ path: 'SKILL.md', type: 'file', size: 256 }],
    })
  }),

  http.get('/api/skills/:name/-/file', ({ params, request }) => {
    const name = params.name as string
    const url = new URL(request.url)
    const path = url.searchParams.get('path')
    if (path !== 'SKILL.md') return new HttpResponse(null, { status: 404 })
    const detail = name === 'code-search' ? mockCodeSearchDetail : mockSkillDetail
    return HttpResponse.json({ name, path, content: detail.content })
  }),

  http.post('/api/skills', async ({ request }) => {
    const body = await request.json() as { name: string; content: string }
    return HttpResponse.json({
      name: body.name,
      key: body.name,
      content: body.content,
      always: false,
      source: 'kirocrew',
      dir: `/path/to/skills/${body.name}`,
    }, { status: 201 })
  }),
  
  http.put('/api/skills/:name', async ({ request }) => {
    const body = await request.json() as { content: string }
    return HttpResponse.json({
      ...mockSkillDetail,
      content: body.content,
    })
  }),
  
  http.delete('/api/skills/:name', () => {
    return HttpResponse.json({ success: true })
  }),
  
  // MCP endpoints
  http.get('/api/mcp', () => {
    return HttpResponse.json(mockMcpServers)
  }),
  
  http.get('/api/mcp/probe', () => {
    return HttpResponse.json([])
  }),
  
  http.post('/api/mcp/toggle', async ({ request }) => {
    const body = await request.json() as { name: string; enabled: boolean }
    return HttpResponse.json({ success: true, name: body.name, enabled: body.enabled })
  }),
  
  http.post('/api/mcp/toggle-tool', async ({ request }) => {
    const body = await request.json() as { server: string; tool: string; enabled: boolean }
    return HttpResponse.json({ success: true, tool: body.tool, enabled: body.enabled })
  }),
  
  http.post('/api/mcp/toggle-all', async ({ request }) => {
    const body = await request.json() as { enabled: boolean }
    return HttpResponse.json({ success: true, enabled: body.enabled })
  }),
  
  http.post('/api/mcp/probe', () => {
    return HttpResponse.json(mockMcpServers)
  }),
  
  http.post('/api/mcp/sync', () => {
    return HttpResponse.json({ success: true })
  }),
  
  http.post('/api/mcp/remove', async ({ request }) => {
    const body = await request.json() as { name: string }
    return HttpResponse.json({ success: true, name: body.name })
  }),
  
  // Sessions endpoint for consolidation
  http.get('/api/sessions', () => {
    return HttpResponse.json({
      sessions: [],
    })
  }),
  
  // Lessons endpoint
  http.get('/api/lessons', () => {
    return HttpResponse.json({
      lessons: [],
    })
  }),
  
  http.post('/api/lessons', async ({ request }) => {
    const body = await request.json() as { rule: string; category: string }
    return HttpResponse.json(
      {
        rule: body.rule,
        category: body.category,
        ts: new Date().toISOString(),
      },
      { status: 201 }
    )
  }),
  
  http.delete('/api/lessons', () => {
    return HttpResponse.json({ success: true })
  }),
  
  // Agents endpoint
  http.get('/api/agents/installed', () => {
    return HttpResponse.json([
      { name: 'default', source: 'builtin' },
      { name: 'kirocrew', source: 'builtin' },
    ])
  }),
  
  // Hooks endpoints
  http.get('/api/hooks', () => {
    return HttpResponse.json({ hooks: mockHooks })
  }),

  http.post('/api/hooks', async ({ request }) => {
    const body = await request.json() as any
    return HttpResponse.json(
      {
        id: `hook-${Date.now()}`,
        ...body,
        last_run: 0,
        last_status: '',
        run_count: 0,
        enabled: true,
      },
      { status: 201 }
    )
  }),

  http.put('/api/hooks/:id', async ({ request }) => {
    const body = await request.json()
    return HttpResponse.json({ success: true, ...body })
  }),

  http.delete('/api/hooks/:id', () => {
    return HttpResponse.json({ success: true })
  }),

  http.post('/api/hooks/:id/toggle', () => {
    return HttpResponse.json({ success: true })
  }),

  http.post('/api/hooks/:id/test', () => {
    return HttpResponse.json({
      result: {
        exit_code: 0,
        duration_ms: 100,
        stdout: 'Test output',
        stderr: '',
      },
    })
  }),

  // Fork slot endpoint
  http.post('/api/chat/slots/:slot/fork', ({ params }) => {
    return HttpResponse.json({ ok: true, key: `${params.slot}-fork` })
  }),

  // Dashboard config endpoints
  http.get('/api/dashboard/config', () => {
    return HttpResponse.json({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false })
  }),

  http.put('/api/dashboard/config', async ({ request }) => {
    await request.json()
    return HttpResponse.json({ ok: true })
  }),

  // Chat config endpoint
  http.get('/api/chat/config', () => {
    return HttpResponse.json({ historyExpanded: false, notifLimit: 50, collapseAllSteps: true })
  }),

  // TaskKeeper endpoints
  http.get('/api/taskkeeper/candidates', () => {
    return HttpResponse.json([
      {
        id: 'C1', source: 'slack', source_ref: 'http://slack/p1',
        sender: 'alice', sender_id: 'U1', summary: 'Please review the PR',
        raw_context: 'review this', suggested_title: 'Review PR #42',
        confidence: 'high', resolution_status: 'open', status: 'pending',
        skip_reason: '', s_number: 'S1', duplicate_group: null,
        duplicate_of_task: null, duplicate_reason: null,
        related_candidates: [], related_tasks: [], created_at: Date.now() / 1000 - 3600,
      },
      {
        id: 'C2', source: 'email', source_ref: 'conv-1',
        sender: 'bob', sender_id: '', summary: 'Approve access request',
        raw_context: 'Subject: Access\nPlease approve', suggested_title: 'Approve access for UK testers',
        confidence: 'medium', resolution_status: 'open', status: 'pending',
        skip_reason: '', s_number: 'E1', duplicate_group: 'llm-S1',
        duplicate_of_task: null, duplicate_reason: 'Both about access',
        related_candidates: [], related_tasks: [], created_at: Date.now() / 1000 - 7200,
      },
      {
        id: 'C3', source: 'slack', source_ref: 'http://slack/p3',
        sender: 'carol', sender_id: 'U3', summary: 'Also about access',
        raw_context: 'need access too', suggested_title: 'Grant access for JP testers',
        confidence: 'high', resolution_status: 'open', status: 'pending',
        skip_reason: '', s_number: 'S2', duplicate_group: 'llm-S1',
        duplicate_of_task: null, duplicate_reason: 'Both about access',
        related_candidates: [], related_tasks: [], created_at: Date.now() / 1000 - 600,
      },
      {
        id: 'C4', source: 'slack', source_ref: 'http://slack/p4',
        sender: 'dave', sender_id: 'U4', summary: 'FYI: deployed',
        raw_context: 'deployed to prod', suggested_title: 'Deployment notification',
        confidence: 'low', resolution_status: 'resolved', status: 'pending',
        skip_reason: 'Already deployed', s_number: 'S3', duplicate_group: null,
        duplicate_of_task: null, duplicate_reason: null,
        related_candidates: [], related_tasks: [], created_at: Date.now() / 1000 - 300,
      },
      {
        id: 'C5', source: 'slack', source_ref: 'http://slack/p5',
        sender: 'eve', sender_id: 'U5', summary: 'Same as existing PLU task',
        raw_context: 'PLU lookup issue', suggested_title: 'Fix PLU lookup gap',
        confidence: 'high', resolution_status: 'open', status: 'pending',
        skip_reason: '', s_number: 'S4', duplicate_group: null,
        duplicate_of_task: 1, duplicate_reason: 'Same as TM#1',
        related_candidates: [], related_tasks: [], created_at: Date.now() / 1000 - 1800,
      },
    ])
  }),
  http.get('/api/taskkeeper/tasks', () => {
    return HttpResponse.json({ tasks: [{ id: 1, title: 'Existing task', status: 'open', details: '' }] })
  }),
  http.post('/api/taskkeeper/scan/all', () => {
    return HttpResponse.json({ ok: true, candidates: [], skipped: [], warnings: [] })
  }),
  http.post('/api/taskkeeper/scan/slack', () => {
    return HttpResponse.json({ ok: true, candidates: [], skipped: [] })
  }),
  http.post('/api/taskkeeper/scan/email', () => {
    return HttpResponse.json({ ok: true, candidates: [], skipped: [] })
  }),
  http.post('/api/taskkeeper/candidates/merge/preview', () => {
    return HttpResponse.json({ ok: true, title: 'Merged title', parts: [] })
  }),
  http.post('/api/taskkeeper/candidates/ungroup', () => {
    return HttpResponse.json({ ok: true, changed: 2 })
  }),
  http.post('/api/taskkeeper/candidates/:id/task_merge/preview', () => {
    return HttpResponse.json({
      ok: true,
      task: { id: 1, title: 'Existing task', status: 'open', details: 'old details' },
      candidate: { id: 'C5', suggested_title: 'Fix PLU lookup gap' },
      draft_title: 'Existing task + PLU fix',
      draft_note: 'eve flagged the PLU lookup gap',
      attribution_header: '**Update from eve via Slack on 2026-05-18:**',
    })
  }),
  http.post('/api/taskkeeper/candidates/:id/task_merge', () => {
    return HttpResponse.json({ ok: true, task: { id: 1, title: 'Existing task + PLU fix' } })
  }),
  http.post('/api/taskkeeper/triage/batch', () => {
    return HttpResponse.json({ ok: true, results: [{ action: 'add', refs: ['S1'], ok: true }] })
  }),
  http.post('/api/taskkeeper/candidates/:id/accept', () => {
    return HttpResponse.json({ ok: true })
  }),
  http.post('/api/taskkeeper/candidates/:id/skip', () => {
    return HttpResponse.json({ ok: true })
  }),
  http.post('/api/taskkeeper/candidates/merge', () => {
    return HttpResponse.json({ ok: true, candidate: { id: 'M1', suggested_title: 'Merged' } })
  }),
  http.get('/api/taskkeeper/rundown', () => {
    return HttpResponse.json({
      open: [{ id: 1, title: 'Review PR', status: 'open', due: '2026-05-18', details: '', delegate: null }],
      wip: [{ id: 2, title: 'Write doc', status: 'wip', due: '2026-05-20', details: '', delegate: null }],
    })
  }),
  http.post('/api/taskkeeper/sync', () => {
    return HttpResponse.json({ ok: true })
  }),
  http.get('/api/taskkeeper/status', () => {
    return HttpResponse.json({ ok: true, tasks_count: 5 })
  }),
]

// Fallback (LAST — specific handlers above always win): happy-dom performs REAL
// network I/O for DOM-driven loads (widget `<script src>`, blob: `<iframe>`
// navigation) that jsdom never did; left alone they dial localhost — ECONNREFUSED
// spam plus a fork-worker socket-teardown crash. This fallback answers any
// otherwise-unmatched request so nothing dials.
//
// LOUD-BY-DEFAULT (fail-closed): rather than empty-200 everything-except-`/api`
// (which would silently pass a test whose component fetched a mistyped
// non-`/api` resource, an app-backend `/apps/{name}/api/*` route, or an external
// URL), we 501 EVERYTHING except a NARROW allowlist of the same-origin
// DOM-driven loads this fallback actually exists for. So a missing/typo'd mock —
// API or otherwise — surfaces loudly instead of masquerading as success.
//
// Allowlisted (empty-200, no dial): `blob:`/`data:` URLs (live-iframe srcdoc /
// blob navigation) and SAME-ORIGIN static asset paths happy-dom eager-loads for
// a widget iframe (`/vendor/*` runtime scripts, `/assets/*`, `/static/*`, and
// bare root files like `/logo.png`). Everything else — `/api/**`,
// `/apps/*/api/*`, cross-origin — gets 501.
const TEST_ORIGIN = 'http://localhost:3000' // vitest happy-dom default document origin
const STATIC_ASSET_RE = /^\/(vendor|assets|static)\/|^\/[^/]+\.[a-z0-9]+$/i

// Path-kind probe (`usePathKind`): markdown inline-code chips HEAD this endpoint
// to learn whether a path-shaped string is a file, a directory, or absent, and
// only become clickable for the first two. Any transcript-rendering test would
// otherwise hit the 501 fallback below purely as a side effect of rendering
// prose that mentions a path.
//
// The default answer is "missing" — chips stay inert, which is what a test that
// is not about chips wants. A test that needs `file`/`dir` stubs
// `globalThis.fetch` directly (see MarkdownRenderer.test.tsx), matching the
// sibling HEAD probe in DiffBlock.
handlers.push(
  http.head('/api/file-read', () =>
    new HttpResponse(null, { status: 404, headers: { 'X-Path-Kind': 'missing' } })),
)

handlers.push(
  http.all('*', ({ request }) => {
    const scheme = request.url.slice(0, 5)
    if (scheme === 'blob:' || scheme === 'data:') {
      return new HttpResponse('', { status: 200 })
    }
    const url = new URL(request.url)
    const sameOrigin = url.origin === TEST_ORIGIN
    if (sameOrigin && STATIC_ASSET_RE.test(url.pathname)) {
      return new HttpResponse('', { status: 200 })
    }
    return new HttpResponse(
      `unmocked ${sameOrigin ? url.pathname : request.url} — add a handler in server.ts`,
      { status: 501 },
    )
  }),
)

export const server = setupServer(...handlers)
