/**
 * Behavioural coverage for the shared dashboard API client: URL and body
 * construction — the query-string builders, path encoding, conditionally-omitted
 * body keys, and the theme-consent wire token on a chat send.
 *
 * Part 2 of the suite `ApiClient.coverage.test.tsx` lays out; the shared
 * fixtures live in `ApiClient.coverageTestUtils.tsx`, and the `vi.mock` block
 * and hooks are the suite's own, word for word.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  api,
  checkSessionExpired,
  __resetAuthRecoveryStateForTests,
} from '../api/client'
import { STALE_OWNER_SESSION_CODE, __resetStaleOwnerHandlerForTests, installStaleOwnerHandler } from '../api/staleOwnerSignal'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { copyToClipboard } from '../utils/clipboard'
import { resizeImageForModel } from '../utils/resizeImage'
import { __resetArtifactWrites } from '../lib/artifactWrites'
import { CONSENT_PREFIX } from '../utils/themeConsent'
import {
  res,
  okJson,
  fetchMock,
  call,
} from './ApiClient.coverageTestUtils'

// `revealPath` funnels a headless host's `copy` fallback into the clipboard, and
// `uploadFiles` downscales through the canvas helper — neither of which exists
// under happy-dom. Both are stubbed at their module boundary so the client's own
// wiring is what gets asserted.
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(async () => {}),
  copyCode: vi.fn(async () => {}),
}))
vi.mock('../utils/resizeImage', () => ({
  MODEL_IMAGE_LIMITS: { maxEdge: 8000, maxBytes: 3_750_000 },
  resizeImageForModel: vi.fn(async (file: File) => ({ file, info: null })),
}))

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockResolvedValue(okJson())
  vi.stubGlobal('fetch', fetchMock)
  __resetAuthRecoveryStateForTests()
  __resetErrorJournalForTests()
  __resetArtifactWrites()
  vi.mocked(copyToClipboard).mockClear()
  vi.mocked(resizeImageForModel).mockClear()
  vi.mocked(resizeImageForModel).mockImplementation(async (file: File) => ({ file, info: null }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  __resetAuthRecoveryStateForTests()
})

/* ──────────────────── 2. URL and body construction ──────────────────── */

describe('query-string builders', () => {
  it('memory records preserve the chosen store, filter and page without leaking query delimiters', async () => {
    await api.memoryRecords('member-reviewer', { q: 'owner+team@example.com & release', kind: 'fact' }, 50, 25)
    const query = new URL(call().url, 'http://localhost').searchParams
    expect(Object.fromEntries(query)).toEqual({ store: 'member-reviewer', q: 'owner+team@example.com & release', kind: 'fact', offset: '50', limit: '25' })
    await api.memoryRecords('', { q: '', kind: 'all' })
    expect(call(1).url).toBe('/api/memory/records?store=default&q=&kind=all&offset=0&limit=50')
  })

  it('memory history encodes record identity and keeps later pages in the same store', async () => {
    await api.memoryRecordHistory('member-reviewer', { kind: 'fact', id: 'user.contact+team&release' }, 25, 50)
    const query = new URL(call().url, 'http://localhost').searchParams
    expect(Object.fromEntries(query)).toEqual({ store: 'member-reviewer', kind: 'fact', id: 'user.contact+team&release', limit: '25', offset: '50' })
    await api.memoryRecordHistory('', { kind: 'fact', id: 'user.contact' })
    expect(call(1).url).toBe('/api/memory/records/history?store=default&kind=fact&id=user.contact&limit=25')
  })

  it('wakatimeExportUrl builds the export href with encoded dates and the format', () => {
    expect(api.wakatimeExportUrl('2026-09-01', '2026-09-07', 'csv')).toBe(
      '/api/wakatime/export?start=2026-09-01&end=2026-09-07&format=csv',
    )
    expect(api.wakatimeExportUrl('2026-09-01', '2026-09-07', 'json')).toBe(
      '/api/wakatime/export?start=2026-09-01&end=2026-09-07&format=json',
    )
  })

  it('kiroPrerequisite distinguishes latched read, coalesced poll and explicit probe', async () => {
    await api.kiroPrerequisite()
    expect(call().url).toBe('/api/kiro-prerequisite')
    await api.kiroPrerequisite('auto')
    expect(call(1).url).toBe('/api/kiro-prerequisite?refresh=auto')
    await api.kiroPrerequisite('explicit')
    expect(call(2).url).toBe('/api/kiro-prerequisite?refresh=explicit')
  })

  it('cloud lifecycle calls carry only the coordinates they were given', async () => {
    await api.cloudStop('kc-1')
    expect(call().url).toBe('/api/cloud/kc-1/stop')
    await api.cloudStart('kc-1', { region: 'us-west-2' })
    expect(call(1).url).toBe('/api/cloud/kc-1/start?region=us-west-2')
    await api.cloudDestroy('kc-1', { profile: 'dev', region: 'us-east-1', instanceId: 'i-0abc' })
    expect(call(2).url).toBe('/api/cloud/kc-1?profile=dev&region=us-east-1&instance_id=i-0abc')
    expect(call(2).method).toBe('DELETE')
  })

  it('cloudPreflight omits absent profile/region', async () => {
    await api.cloudPreflight()
    expect(call().url).toBe('/api/cloud/preflight')
    await api.cloudPreflight('dev')
    expect(call(1).url).toBe('/api/cloud/preflight?profile=dev')
    await api.cloudPreflight('dev', 'eu-west-1')
    expect(call(2).url).toBe('/api/cloud/preflight?profile=dev&region=eu-west-1')
  })

  it('artifacts() serialises every filter, and sends pinned=0 for an explicit false', async () => {
    await api.artifacts()
    expect(call().url).toBe('/api/artifacts')
    await api.artifacts({
      tag: 'cr', kind: 'widget', q: 'queue', source_path: '/n/a.md',
      snippet: true, contentMatch: true, session: 'chat-1', touchedBy: 'chat-2', pinned: true,
    })
    expect(call(1).url).toBe(
      '/api/artifacts?tag=cr&kind=widget&q=queue&source_path=%2Fn%2Fa.md' +
      '&snippet=1&content=1&session=chat-1&touched_by=chat-2&pinned=1',
    )
    // `pinned: false` must survive as an explicit "unpinned only", not be dropped.
    await api.artifacts({ pinned: false })
    expect(call(2).url).toBe('/api/artifacts?pinned=0')
    await api.artifacts({ snippet: false, contentMatch: false })
    expect(call(3).url).toBe('/api/artifacts')
  })

  it('cronHistory sends offset/limit only when supplied, including zero', async () => {
    await api.cronHistory('job-1')
    expect(call().url).toBe('/api/crons/job-1/history')
    await api.cronHistory('job-1', 0, 25)
    expect(call(1).url).toBe('/api/crons/job-1/history?offset=0&limit=25')
    expect(call(1).headers['X-Session-Key']).toBe('dashboard:ui')
  })

  it('cronHistoryAll can filter by job', async () => {
    await api.cronHistoryAll()
    expect(call().url).toBe('/api/crons/history')
    await api.cronHistoryAll({ offset: 10, limit: 5, jobId: 'job-2' })
    expect(call(1).url).toBe('/api/crons/history?offset=10&limit=5&job_id=job-2')
  })

  it('chatSlotDetail paginates with limit/before', async () => {
    await api.chatSlotDetail('chat 1')
    expect(call().url).toBe('/api/chat/slots/chat%201?')
    await api.chatSlotDetail('chat-1', 50, 0)
    expect(call(1).url).toBe('/api/chat/slots/chat-1?limit=50&before=0')
  })

  it('suggestions can force regeneration', async () => {
    await api.suggestions()
    expect(call().url).toBe('/api/suggestions')
    await api.suggestions(true)
    expect(call(1).url).toBe('/api/suggestions?force=1')
  })

  it('instanceStatus opts into the diagnosis probe', async () => {
    await api.instanceStatus('cd-1', true)
    expect(call().url).toBe('/api/instances/cd-1/status?diagnose=1')
  })

  it('sessions/search/episodic pass their paging and tag filters through', async () => {
    await api.sessions()
    expect(call().url).toBe('/api/sessions?limit=30&offset=0')
    await api.sessions(10, 20, true)
    expect(call(1).url).toBe('/api/sessions?limit=10&offset=20&preview=1')
    await api.sessions(30, 0, false, true)
    expect(call(2).url).toBe('/api/sessions?limit=30&offset=0&exclude_open=1')
    await api.sessions(30, 0, false, true, true)
    expect(call(3).url).toBe('/api/sessions?limit=30&offset=0&exclude_open=1&user_only=1')
    await api.sessionsSearch('a b', 5)
    expect(call(4).url).toBe('/api/sessions/search?q=a%20b&limit=5')
    await api.vectorEpisodic(10, 5, 'promo,l6')
    expect(call(5).url).toBe('/api/memory/episodic?limit=10&offset=5&tags=promo%2Cl6')
    await api.vectorEpisodic()
    expect(call(6).url).toBe('/api/memory/episodic?limit=50&offset=0')
    await api.vectorEpisodicSearch('q', 'tag')
    expect(call(7).url).toBe('/api/memory/episodic/search?q=q&tags=tag')
  })

  it('discovery endpoints append provider and limit only when set', async () => {
    await api.discoverSkills('grill')
    expect(call().url).toBe('/api/skills/-/discover?q=grill')
    await api.discoverSkills('grill', { provider: 'skills.sh', limit: 5 })
    expect(call(1).url).toBe('/api/skills/-/discover?q=grill&provider=skills.sh&limit=5')
    await api.mcpDiscover('fs')
    expect(call(2).url).toBe('/api/mcp/discover?q=fs')
    await api.mcpDiscover('fs', { provider: 'official', limit: 3 })
    expect(call(3).url).toBe('/api/mcp/discover?q=fs&provider=official&limit=3')
  })

  it('browseRemoteArtifacts defaults to the caller-owned scope', async () => {
    await api.browseRemoteArtifacts('quip')
    expect(call().url).toBe('/api/remote-artifacts/quip/browse?scope=mine')
    await api.browseRemoteArtifacts('quip', { scope: 'shared', q: 'a b', pageToken: 't/1' })
    expect(call(1).url).toBe('/api/remote-artifacts/quip/browse?scope=shared&q=a%20b&pageToken=t%2F1')
  })

  it('effortLevels and mcpActive scope to a slot/agent when given one', async () => {
    await api.effortLevels()
    expect(call().url).toBe('/api/effort-levels')
    await api.effortLevels('chat-1')
    expect(call(1).url).toBe('/api/effort-levels?slot=chat-1')
    await api.mcpActive()
    expect(call(2).url).toBe('/api/mcp/active')
    await api.mcpActive('kirocrew')
    expect(call(3).url).toBe('/api/mcp/active?agent=kirocrew')
  })

  it('browse/project helpers encode the path they were handed', async () => {
    await api.browseDirs()
    expect(call().url).toBe('/api/browse-dirs')
    await api.browseDirs('/a b/c')
    expect(call(1).url).toBe('/api/browse-dirs?path=%2Fa%20b%2Fc')
    await api.browseFiles('/a b')
    expect(call(2).url).toBe('/api/browse-files?path=%2Fa%20b')
    await api.projectGit('/repo x')
    expect(call(3).url).toBe('/api/project/git?path=%2Frepo%20x')
    await api.fileDiff('/repo/a b.ts')
    expect(call(4).url).toBe('/api/file-diff?path=%2Frepo%2Fa%20b.ts')
  })

  it('scopes fileSearch to a project and relays the abort signal under its deadline', async () => {
    const ctl = new AbortController()
    await api.fileSearch('cli', 'kirocrew', ctl.signal)
    expect(call().url).toBe('/api/file-search?q=cli&project=kirocrew')
    // The fetch gets the DEADLINE's signal, not the caller's: the bound lives in
    // the client, so a caller cannot opt out of it by handing over its own.
    const relayed = call().init?.signal as AbortSignal
    expect(relayed).toBeInstanceOf(AbortSignal)
    expect(relayed).not.toBe(ctl.signal)
    // Relay is asserted where observable -- on a request still in flight; this one
    // has settled, so its timer and listener are already released.

    // A caller that passes no signal is bounded all the same.
    await api.fileSearch('cli')
    expect(call(1).url).toBe('/api/file-search?q=cli')
    expect(call(1).init?.signal).toBeInstanceOf(AbortSignal)
  })

  it('artifactSessionDocs can scope to one session', async () => {
    await api.artifactSessionDocs()
    expect(call().url).toBe('/api/artifacts/session-docs')
    await api.artifactSessionDocs('chat-1')
    expect(call(1).url).toBe('/api/artifacts/session-docs?session=chat-1')
  })

  it('vectorContextPreview and telemetryContextTrace encode their query', async () => {
    await api.vectorContextPreview()
    expect(call().url).toBe('/api/memory/context-preview')
    await api.vectorContextPreview('a b')
    expect(call(1).url).toBe('/api/memory/context-preview?q=a%20b')
    await api.telemetryContextTrace('chat 1')
    expect(call(2).url).toBe('/api/telemetry/context-trace?slot=chat%201')
  })

  it('deleteArtifactFolder states the cascade explicitly in the query', async () => {
    await api.deleteArtifactFolder('f1', false)
    expect(call().url).toBe('/api/artifact-folders/f1?delete_contents=false')
    await api.deleteArtifactFolder('f1', true)
    expect(call(1).url).toBe('/api/artifact-folders/f1?delete_contents=true')
  })

  it('getArtifactPublishProviders and weixinQrStatus encode their single param', async () => {
    await api.getArtifactPublishProviders('markdown')
    expect(call().url).toBe('/api/artifacts/publish-providers?kind=markdown')
    await api.weixinQrStatus('s/1')
    expect(call(1).url).toBe('/api/channels/weixin/qr/status?session_id=s%2F1')
  })

  it('knowledgeSearch and autocomplete encode the query', async () => {
    await api.knowledgeSearch('a&b')
    expect(call().url).toBe('/api/knowledge/search-for-context?q=a%26b')
    await api.autocomplete('a&b')
    expect(call(1).url).toBe('/api/autocomplete?q=a%26b')
  })

  it('agentResolvedModel resolves the configured default for an empty agent', async () => {
    await api.agentResolvedModel('')
    expect(call().url).toBe('/api/agents/resolved-model?agent=')
  })
})

describe('path encoding', () => {
  it('uses the legacy slot route for one session automation snapshot', async () => {
    await api.autonudgeForSlot('chat/1')
    expect(call().url).toBe('/api/autonudge/slot/chat%2F1')
  })

  it('percent-encodes single-segment ids', async () => {
    await api.artifact('a/b')
    expect(call().url).toBe('/api/artifacts/a%2Fb')
    await api.deleteChatSlot('chat/1')
    expect(call(1).url).toBe('/api/chat/slots/chat%2F1')
    await api.untrustApp('my app')
    expect(call(2).url).toBe('/api/security/trusted-apps/my%20app')
    await api.deleteWorkspace('w s')
    expect(call(3).url).toBe('/api/workspaces/w%20s')
    await api.deleteTheme('a b')
    expect(call(4).url).toBe('/api/themes/a%20b')
  })

  it('keeps the separators of nested skill/prompt/steering keys while encoding each segment', async () => {
    // These names are genuinely hierarchical (`auto/resolve-i18n/...`), so the
    // slashes are structure and must NOT be encoded — only the segments are.
    await api.skill('auto/my skill')
    expect(call().url).toBe('/api/skills/auto/my%20skill')
    await api.skillTree('auto/my skill')
    expect(call(1).url).toBe('/api/skills/auto/my%20skill/-/tree')
    await api.skillFile('auto/s', 'rules/a b.md')
    expect(call(2).url).toBe('/api/skills/auto/s/-/file?path=rules%2Fa%20b.md')
    await api.promptDetail('team/sop x')
    expect(call(3).url).toBe('/api/prompts/team/sop%20x')
    await api.steeringFile('project/a b.md')
    expect(call(4).url).toBe('/api/steering/project/a%20b.md')
    await api.updateSkill('auto/s', '# hi', 'abc123')
    expect(call(5).url).toBe('/api/skills/auto/s')
    expect(call(5).body).toEqual({ content: '# hi', base_hash: 'abc123' })
  })

  it('builds the versioned artifact paths', async () => {
    await api.artifactVersion('cr-queue', 3)
    expect(call().url).toBe('/api/artifacts/cr-queue/versions/3')
    await api.artifactVersions('cr-queue')
    expect(call(1).url).toBe('/api/artifacts/cr-queue/versions')
    await api.artifactEvents('cr-queue')
    expect(call(2).url).toBe('/api/artifacts/cr-queue/events')
  })

  it('encodes both ids in two-segment comment paths', async () => {
    await api.replyArtifactComment('a/b', 'c/d', { text: 'ok' })
    expect(call().url).toBe('/api/artifacts/a%2Fb/comments/c%2Fd/reply')
    await api.deleteArtifactComment('a/b', 'c/d')
    expect(call(1).url).toBe('/api/artifacts/a%2Fb/comments/c%2Fd')
    await api.markCommentReview('s', 'c')
    expect(call(2).url).toBe('/api/artifacts/s/comments/c/review')
    await api.resolveComment('s', 'c')
    expect(call(3).url).toBe('/api/artifacts/s/comments/c/resolve')
    await api.reopenComment('s', 'c')
    expect(call(4).url).toBe('/api/artifacts/s/comments/c/reopen')
    await api.editArtifactComment('s', 'c', { text: 'x' })
    expect(call(5).url).toBe('/api/artifacts/s/comments/c')
    expect(call(5).method).toBe('PATCH')
  })

  it('percent-encodes provider-native remote ids, which may contain slashes', async () => {
    await api.remoteArtifactDetail('quip', 'folder/doc')
    expect(call().url).toBe('/api/remote-artifacts/quip/folder%2Fdoc')
    await api.remoteArtifactComments('quip', 'folder/doc')
    expect(call(1).url).toBe('/api/remote-artifacts/quip/folder%2Fdoc/comments')
    await api.deleteRemoteComment('quip', 'folder/doc', 'c/1')
    expect(call(2).url).toBe('/api/remote-artifacts/quip/folder%2Fdoc/comments/c%2F1')
    // clone/fork keep the external id in the BODY — a path segment cannot carry it.
    await api.cloneRemoteArtifact('quip', 'folder/doc')
    expect(call(3).url).toBe('/api/remote-artifacts/quip/clone')
    expect(call(3).body).toEqual({ external_id: 'folder/doc' })
    await api.forkRemoteArtifact('quip', 'folder/doc')
    expect(call(4).body).toEqual({ external_id: 'folder/doc' })
  })
})

describe('request bodies with conditionally-omitted keys', () => {
  it('createChatSlot resolves an omitted mode and preserves explicit fields', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ default_memory_mode: 'temporary' }))
    await api.createChatSlot()
    expect(call().url).toBe('/api/dashboard/config')
    expect(call(1).body).toEqual({ memory_mode: 'temporary' })

    await api.createChatSlot('n', 'a', 'm', 'mode', 'mem', 't', 'slug', 'f1')
    expect(call(2).body).toEqual({
      name: 'n', agent: 'a', model: 'm', mode: 'mode', memory_mode: 'mem',
      title: 't', artifact: 'slug', folder_id: 'f1',
    })
  })

  it('createChatSlot sends agent_kind beside an agent and never a crew binding', async () => {
    await api.createChatSlot(
      undefined, 'a', undefined, undefined, 'persistent', undefined,
      undefined, undefined, 'member',
    )
    expect(call().url).toBe('/api/chat/slots')
    expect(call().body).toEqual({ agent: 'a', agent_kind: 'member', memory_mode: 'persistent' })
  })

  it('sessionStorageRestore adds the uid list only for a partial restore', async () => {
    await api.sessionStorageRestore('b1')
    expect(call().body).toEqual({ batch_id: 'b1' })
    await api.sessionStorageRestore('b1', ['u1'])
    expect(call(1).body).toEqual({ batch_id: 'b1', uids: ['u1'] })
  })

  it('forkChatSlot omits an unspecified fork point', async () => {
    await api.forkChatSlot('chat-1')
    expect(call().body).toEqual({})
    // index 0 is a legitimate fork point and must not be dropped as falsy.
    await api.forkChatSlot('chat-1', 0, 'why', 'plan', 'down')
    expect(call(1).body).toEqual({ at_message_index: 0, prompt: 'why', mode: 'plan', direction: 'down' })
    await api.forkChatSlot('chat-1', 7, undefined, undefined, 'head', 'row-42')
    expect(call(2).body).toEqual({ at_message_index: 7, at_message_id: 'row-42', direction: 'head' })
  })

  it('slackLink sends no body at all when it is only asking for the existing link', async () => {
    await api.slackLink('chat-1')
    expect(call().init?.body).toBeUndefined()
    await api.slackLink('chat-1', 'C123', '1699.1')
    expect(call(1).body).toEqual({ channel: 'C123', thread_ts: '1699.1' })
  })

  it('channelClearContext narrows to one agent only in agent scope', async () => {
    await api.channelClearContext('ch1', 'all')
    expect(call().body).toEqual({ scope: 'all' })
    await api.channelClearContext('ch1', 'agent', 'a1')
    expect(call(1).body).toEqual({ scope: 'agent', agent_id: 'a1' })
  })

  it('answerQuestion distinguishes an answer from a dismissal', async () => {
    await api.answerQuestion('ask-1', { q1: 'yes' })
    expect(call().body).toEqual({ answers: { q1: 'yes' } })
    await api.answerQuestion('ask-1')
    expect(call(1).body).toEqual({ dismissed: true })
  })

  it('interruptSlot targets a specific queued message when asked to', async () => {
    await api.interruptSlot('chat-1')
    expect(call().body).toEqual({})
    await api.interruptSlot('chat-1', 'q1')
    expect(call(1).body).toEqual({ queue_id: 'q1' })
  })

  it('uninstallApp only names the retention exceptions it needs', async () => {
    await api.uninstallApp('demo')
    expect(call().body).toEqual({})
    await api.uninstallApp('demo', false, true, ['dep-a'])
    expect(call(1).body).toEqual({ purge_data: true, keep_dependencies: true, keep_specific: ['dep-a'] })
    await api.uninstallApp('demo', true, false, [])
    expect(call(2).body).toEqual({})
  })

  it('createChatFolder folds the modal settings into the create body', async () => {
    await api.createChatFolder('Work')
    expect(call().body).toEqual({ name: 'Work', parent_id: '' })
    await api.createChatFolder('Work', 'p1', { project_dir: '/r', default_agent: 'kirocrew', color: 'blue' })
    expect(call(1).body).toEqual({
      name: 'Work', parent_id: 'p1', project_dir: '/r', default_agent: 'kirocrew', color: 'blue',
    })
  })

  it('chatSlotContext marks an ephemeral injection and names its source', async () => {
    await api.chatSlotContext('chat-1', 'ctx')
    expect(call().body).toEqual({ content: 'ctx' })
    await api.chatSlotContext('chat-1', 'ctx', { source: 'artifact', ephemeral: false })
    expect(call(1).body).toEqual({ content: 'ctx', source: 'artifact', ephemeral: false })
  })

  it('sideTurn flags a steer, and sideQueueCancel names the owning tab', async () => {
    await api.sideTurn('chat-1', 'why?')
    expect(call().body).toEqual({ question: 'why?' })
    await api.sideTurn('chat-1', 'why?', { steer: true })
    expect(call(1).body).toEqual({ question: 'why?', steer: true })
    await api.sideQueueCancel('chat-1', 'q1')
    expect(call(2).method).toBe('DELETE')
    expect(call(2).body).toHaveProperty('client')
  })

  it('cancelTaskRunner and installDiscoveredSkill keep their optional fields optional', async () => {
    await api.cancelTaskRunner()
    expect(call().init?.body).toBeUndefined()
    await api.cancelTaskRunner('t1')
    expect(call(1).body).toEqual({ task_id: 't1' })
    await api.installDiscoveredSkill('skills.sh', 'grill', { name: 'grill2', overwrite: true })
    expect(call(2).body).toEqual({ provider: 'skills.sh', skill_id: 'grill', name: 'grill2', overwrite: true })
  })

  it('cleanupSessions coerces its flags and defaults the active slot', async () => {
    await api.cleanupSessions(30)
    expect(call().body).toEqual({ max_inactive_days: 30, active_slot: '', dry_run: false })
    await api.cleanupSessions(7, 'chat-1', true)
    expect(call(1).body).toEqual({ max_inactive_days: 7, active_slot: 'chat-1', dry_run: true })
  })

  it('planTask and startTaskRunner blank out absent optional strings', async () => {
    await api.startTaskRunner('spec')
    expect(call().body).toEqual({ spec: 'spec', agent: '', workspace_dir: '' })
    await api.planTask('do it', 'chat')
    expect(call(1).body).toEqual({ input: 'do it', source: 'chat', spec: '', agent: '', workspace_dir: '' })
    await api.executePlan('t1')
    expect(call(2).body).toEqual({ agent: '', auto_approve: false })
    await api.planFromChat([{ title: 'a' }])
    expect(call(3).body).toEqual({ steps: [{ title: 'a' }], task_id: '', original_input: '' })
  })

  it('createWebhookToken requires a signature by default and carries the destination', async () => {
    await api.createWebhookToken('ci', undefined, 'code-reviewer')
    expect(call().body).toEqual({ agent: 'code-reviewer', label: 'ci', require_signature: true })
    await api.createWebhookToken('legacy', false)
    expect(call(1).body).toEqual({ agent: '', label: 'legacy', require_signature: false })
  })

  it('addUserDeniedCommand defaults the operator note to empty', async () => {
    await api.addUserDeniedCommand('rm -rf /')
    expect(call().body).toEqual({ pattern: 'rm -rf /', note: '' })
  })

  it('setSlotFolder and setSlotColor send a clearing value rather than dropping the key', async () => {
    await api.setSlotFolder('chat-1', null)
    expect(call().body).toEqual({ folder_id: '' })
    await api.setSlotColor('chat-1', null)
    expect(call(1).body).toEqual({ color_index: null })
    await api.setSlotColor('chat-1', 0)
    expect(call(2).body).toEqual({ color_index: 0 })
  })

  it('mcpGatewaySetStub has a single and a batch form', async () => {
    await api.mcpGatewaySetStub('fs', true)
    expect(call().body).toEqual({ name: 'fs', stub: true })
    await api.mcpGatewaySetStub('fs', true, 'command-hash:env-hash')
    expect(call(1).body).toEqual({ name: 'fs', stub: true, expected_launch: 'command-hash:env-hash' })
    await api.mcpGatewaySetStubMany(['fs', 'git'], false)
    expect(call(2).body).toEqual({ names: ['fs', 'git'], stub: false })
  })

  it('createTagColumn/updateTagColumn pass the filter mode straight through', async () => {
    await api.createTagColumn({ name: 'Doing', tag_ids: ['t1'], mode: 'any', include_untagged: true })
    expect(call().body).toEqual({ name: 'Doing', tag_ids: ['t1'], mode: 'any', include_untagged: true })
    await api.updateTagColumn('c1', { mode: 'none', order: 2 })
    expect(call(1).method).toBe('PATCH')
    expect(call(1).body).toEqual({ mode: 'none', order: 2 })
  })

  it('vectorSemanticWrite stamps the write as an explicit user edit', async () => {
    await api.vectorSemanticWrite('k', 'v')
    expect(call().body).toEqual({ key: 'k', value: 'v', source: 'user_explicit' })
  })

  it('enablePullRequestAutoMerge requires opting in to an immediate merge', async () => {
    await api.enablePullRequestAutoMerge('https://gh/pr/1')
    expect(call().body).toEqual({ url: 'https://gh/pr/1', confirmImmediateMerge: false })
    await api.enablePullRequestAutoMerge('https://gh/pr/1', true)
    expect(call(1).body).toEqual({ url: 'https://gh/pr/1', confirmImmediateMerge: true })
  })

  it('pull-request source calls carry the refresh flag and the thread ids', async () => {
    await api.pullRequestSource('u')
    expect(call().body).toEqual({ url: 'u', refresh: false })
    await api.fetchIssueSource('u', true)
    expect(call(1).body).toEqual({ url: 'u', refresh: true })
    await api.resolvePullRequestThread('u', 't1')
    expect(call(2).body).toEqual({ url: 'u', threadId: 't1' })
    await api.unresolvePullRequestThread('u', 't1')
    expect(call(3).url).toBe('/api/source/pull-request/unresolve')
    await api.submitPullRequestReview('u', 'r1', 'APPROVE', 'digest')
    expect(call(4).body).toEqual({ url: 'u', reviewId: 'r1', event: 'APPROVE', contentDigest: 'digest' })
  })

  it('resumeChatSlot repeats the key as name and falls back to it for the title', async () => {
    await api.resumeChatSlot('k1')
    expect(call().body).toEqual({ name: 'k1', key: 'k1', title: 'k1' })
    await api.resumeChatSlot('k1', 'Real title')
    expect(call(1).body).toEqual({ name: 'k1', key: 'k1', title: 'Real title' })
  })

  it('approveChatSlot merges caller-supplied extras into the action body', async () => {
    await api.approveChatSlot('chat-1', 'allow', { tool: 'fs_write' })
    expect(call().body).toEqual({ action: 'allow', tool: 'fs_write' })
  })
})

/* ─────────── theme-consent wire token (two-tier consent) ─────────── */

describe('sendChat theme consent', () => {
  const slug = 'neon'

  beforeEach(() => { localStorage.clear() })
  afterEach(() => { localStorage.clear() })

  it('sends no theme fields at all when no colour theme is active', async () => {
    await api.sendChat('hi', 'chat-1')
    expect(call().url).toBe('/api/chat?ws=1')
    expect(call().body).toEqual({ message: 'hi', slot: 'chat-1' })
  })

  it('sends the theme but no consent token for a built-in theme', async () => {
    localStorage.setItem(CONSENT_PREFIX + slug, 'sha-abc')
    await api.sendChat('hi', 'chat-1', 'ocean')
    expect(call().body).toMatchObject({ color_theme: 'ocean' })
    expect(call().body).not.toHaveProperty('theme_consent_sha')
  })

  it('transmits the RAW stored grant for an installed pack', async () => {
    localStorage.setItem(CONSENT_PREFIX + slug, 'sha-abc')
    await api.sendChat('hi', 'chat-1', `custom-${slug}`)
    expect(call().body).toMatchObject({ color_theme: 'custom-neon', theme_consent_sha: 'sha-abc' })
    // The legacy boolean is deliberately not sent — gating is content-bound server-side.
    expect(call().body).not.toHaveProperty('theme_consent')
  })

  it('omits the token when the pack has no stored grant', async () => {
    await api.sendChat('hi', 'chat-1', 'custom-unknown')
    expect(call().body).not.toHaveProperty('theme_consent_sha')
  })

  it('treats a legacy "1" grant as no grant so the user re-prompts exactly once', async () => {
    localStorage.setItem(CONSENT_PREFIX + slug, '1')
    await api.sendChat('hi', 'chat-1', `custom-${slug}`)
    expect(call().body).not.toHaveProperty('theme_consent_sha')
  })

  it('treats an empty stored token as no grant', async () => {
    localStorage.setItem(CONSENT_PREFIX + slug, '')
    await api.sendChat('hi', 'chat-1', `custom-${slug}`)
    expect(call().body).not.toHaveProperty('theme_consent_sha')
  })

  it('carries meta, the steer intent and an abort signal', async () => {
    const ctl = new AbortController()
    await api.sendChat('hi', 'chat-1', undefined, ctl.signal, { attachments: ['a.png'] }, true)
    expect(call().body).toEqual({
      message: 'hi', slot: 'chat-1', meta: { attachments: ['a.png'] }, steer: true,
    })
    expect(call().init?.signal).toBe(ctl.signal)
  })

  it('hands the raw response back but still runs session-expiry recovery on a 403 auth challenge', async () => {
    // The transport reads the receipt itself, so a 4xx must RESOLVE -- but an
    // expired session must not degrade into a bare "refused" send: the same
    // silent-refresh path every `j`-parsed call takes runs first. (The steer
    // helper this wire replaced went through `j` and had it.)
    __resetAuthRecoveryStateForTests()
    fetchMock.mockResolvedValueOnce(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
    fetchMock.mockResolvedValue(okJson({ ok: true }))
    const r = await api.sendChat('hi', 'chat-1')
    expect(r.status).toBe(403)
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/auth/refresh', expect.anything()))
  })

  it('clears a stale session-expired banner when the send itself succeeds', async () => {
    // `j` dismisses the banner on any 2xx; the raw-response wire must too, or a
    // steer that succeeds after auth was restored in another tab leaves the
    // banner up until some unrelated parsed request happens to land.
    __resetAuthRecoveryStateForTests()
    fetchMock.mockResolvedValue(res(401, 'revoked'))
    checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
    await vi.waitFor(() => expect(document.getElementById('mc-session-expired')).not.toBeNull())
    try {
      fetchMock.mockResolvedValue(okJson({ ok: true }))
      const r = await api.sendChat('hi', 'chat-1')
      expect(r.ok).toBe(true)
      expect(document.getElementById('mc-session-expired')).toBeNull()
    } finally {
      document.getElementById('mc-session-expired')?.remove()
    }
  })

  it('raises the stale-owner prompt on a 401 stale-session body, and the receipt stays readable', async () => {
    // The stale-owner code travels in the BODY, which the pre-body hook cannot
    // read; the wire reads it off a clone so the transport's own `json()` still
    // works. (`steerChat` reached this through `j`; the transport must too.)
    const onStale = vi.fn()
    installStaleOwnerHandler(onStale)
    try {
      fetchMock.mockResolvedValueOnce(res(401, { error: 'sign in again', code: STALE_OWNER_SESSION_CODE }))
      const r = await api.sendChat('hi', 'chat-1')
      expect(r.status).toBe(401)
      await expect(r.json()).resolves.toMatchObject({ code: STALE_OWNER_SESSION_CODE })
      await vi.waitFor(() => expect(onStale).toHaveBeenCalled())
    } finally {
      __resetStaleOwnerHandlerForTests()
    }
  })
})
