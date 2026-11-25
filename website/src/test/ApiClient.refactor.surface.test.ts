/**
 * Two properties of the `api` surface that a domain split can change while every
 * type and behavior test stays green: which domain group supplies each member,
 * and which members leave `X-Session-Key` off their request.
 *
 * The facade in `client.ts` spreads domain groups into one object, so a member
 * two groups both define, or that an inline member re-declares, is silently
 * replaced by whichever comes last. The groups are rebuilt here from the same
 * factories and checked against the facade member by member.
 *
 * The header list is every member whose request carries no session key, raw
 * `fetch` reads and writes alike, so moving one onto the shared helpers (which add
 * the key) fails here. A member joins `NO_SESSION_KEY` only when it deliberately
 * bypasses the helpers, so each entry is a reviewed exception.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, __resetAuthRecoveryStateForTests } from '../api/client'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { __resetArtifactWrites } from '../lib/artifactWrites'
import { FakeXhr } from './fakeXhr'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// `uploadFiles` downscales through a canvas helper that happy-dom lacks.
vi.mock('../utils/resizeImage', () => ({
  MODEL_IMAGE_LIMITS: { maxEdge: 8000, maxBytes: 3_750_000 },
  resizeImageForModel: vi.fn(async (file: File) => ({ file, info: null })),
}))

const NO_SESSION_KEY = [
  'status', 'tunnelStatus', 'system', 'sessionCrewLogProjections',
  'telemetryStartup', 'telemetryContextTrace', 'usageTurns', 'wakatimeStats',
  'sessionSummary', 'dashboardCard', 'generateSessionSummary', 'beaconStatus', 'collectionStatus',
  'suggestions', 'branding', 'memoryPreferences', 'memoryProjects',
  'memoryHistory', 'memorySettings', 'memoryStores', 'memoryRetired',
  'memoryBackups', 'memoryCarve', 'memoryRecall', 'memoryRecords',
  'memoryRecordHistory', 'memberMemoryPage', 'vectorSemantic', 'vectorEpisodic',
  'vectorEpisodicSearch', 'vectorStats', 'vectorEvents', 'vectorEmbeddingStatus',
  'vectorContextPreview', 'memoryGraph', 'sessionsMemory', 'sessionsUsage',
  'providerUsage', 'mcpProbeCache', 'agentsInstalled', 'agentTemplates',
  'agentTemplateDelete', 'agentDetail', 'agentPatch', 'agentFork',
  'agentPublish', 'agentReset', 'defaultMcpSetAdd', 'defaultMcpSetRemove',
  'agentResolvedModel', 'members',
  'memberActivity', 'memberProjections', 'memberBriefing',
  'teams.list', 'appearances.list', 'appearances.detail', 'uploadCrewAvatar',
  'models', 'chatSlotSelectionCapabilities', 'effortLevels', 'slashCommands',
  'chatSlotAutocompact', 'chatSlotTurnTimeout', 'chatSlotsFollowDefaultCount', 'recentProjects', 'favoriteProjects', 'browseDirs', 'browseDrives',
  'browseFiles', 'projectGit', 'projectGitStatus', 'projectGitLog', 'projectGitBranchDiff', 'projectGitRepos',
  'projectTree', 'workspaces', 'crons', 'cron', 'updateCron',
  'cronFolders', 'updateCronFolder', 'lessons', 'hooks',
  'kiroHooks', 'webhooks', 'updateWebhookToken', 'prompts',
  'promptDetail', 'skill', 'skillTree', 'skillFile',
  'skillsPending', 'skillPendingDetail', 'mcpServers', 'mcpGlobalScopes',
  'mcpActive', 'connectionsMintState', 'connectionsMintServerState', 'connectionsStatus', 'connectionsOAuthClients',
  'mcpGatewayStatus', 'mcpGatewayFootprint', 'mcpGatewayMetrics', 'mcpGatewayServers', 'mcpGatewayLaunchPreview',
  'mcpMeasureProgress', 'agentConfig', 'defaultAgent', 'kirocrewConfig',
  'patchConfig', 'acpBackends', 'kiroUsage', 'capabilityMcpList',
  'capabilitySkillsList', 'capabilityAgentsList', 'capabilityPluginsList', 'capabilityMcpRegistry',
  'sttConfig', 'sttStatus', 'sttVocabularies', 'sttTranscribe', 'chatSlots',
  'chatSlotsUnrestored', 'autonudgeList', 'autonudgeForSlot', 'monitorsList', 'monitorForSlot',
  'monitorContract',
  'chatSlotSourceLinks', 'chatSlotDetail', 'channelTargets', 'channelTopics', 'slackChannels',
  'sessionsHealth', 'tasksSummary', 'spawnAdaptive', 'tasksList', 'taskDetail',
  'notifications', 'notificationChannels', 'sessions', 'sessionsSearch', 'sessionsSearchMessages', 'sessionsNamed',
  'sessionArchiveList', 'sessionArchiveRead',
  'instancesSearchSessions', 'instancesCapabilities', 'instanceChatSlots', 'sessionDetail', 'sessionMeta',
  'autocomplete', 'spawnList', 'spawnStatus', 'approvals',
  'pendingQuestions', 'logLevel', 'taskRunnerStatus', 'renameTaskRun',
  'updateTask', 'refineStatus', 'planContext', 'checkUpdate',
  'changelog', 'releases', 'armStatus', 'fileDiff',
  'fileSearch', 'pathComplete', 'uploadFiles', 'uploadToDirectory', 'themes',
  'dashboardConfig', 'themeDetail', 'themeBoot', 'voiceConfig',
  'voiceVoices', 'voiceSystemVoices', 'awsConsent', 'credentialRedaction', 'channelsList', 'channelPresets',
  'channelGet', 'listApps', 'getApp', 'getAppManifest',
  'uninstallPreview', 'listRegistry', 'listRegistries',
]

// Members whose request is built from an argument's fields or bytes; every other
// member is called with the generic positional strings.
const ARGS: Record<string, unknown[]> = {
  memoryRecords: ['member-reviewer', { q: 'contact', kind: 'fact' }, 0, 50],
  memoryRecordHistory: ['member-reviewer', { kind: 'fact', id: 'user.contact' }, 25, 0],
  uploadFiles: [[new File(['abc'], 'a.txt', { type: 'text/plain' })]],
  uploadToDirectory: ['/tmp/proj', new File(['abc'], 'a.txt', { type: 'text/plain' })],
  uploadCrewAvatar: ['crew-a', new File(['png'], 'a.png', { type: 'image/png' })],
  sttTranscribe: [new Blob(['wav'], { type: 'audio/wav' })],
  // Takes only an AbortSignal; its deadline helper throws on a string before it fetches.
  slashCommands: [],
  // Same deadline helper: the second / third argument is an AbortSignal, not a string.
  browseFiles: ['sw-1'],
  fileSearch: ['sw-1', 'sw-2'],
}

function okJson(): Response {
  const body = { ok: true, items: [], paths: ['/p/a'] }
  return {
    ok: true,
    status: 200,
    url: 'http://localhost:6776/api/probe',
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
    blob: async () => new Blob([JSON.stringify(body)]),
    clone: () => okJson(),
    body: null,
  } as unknown as Response
}

const fetchMock = vi.fn()

/** `uploadFiles` posts over XMLHttpRequest (upload progress); this one hands
 *  each request to `fetchMock` and answers at once, so the probe reads it the
 *  same way as every fetch. */
class XhrViaFetchMock extends FakeXhr {
  send(body: unknown) {
    fetchMock(this.url, { method: this.method, headers: this.requestHeaders, body })
    this.respond(200, { paths: ['/p/a'] })
  }
}

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockImplementation(async () => okJson())
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('XMLHttpRequest', XhrViaFetchMock)
  __resetAuthRecoveryStateForTests()
  __resetErrorJournalForTests()
  __resetArtifactWrites()
})

afterEach(() => {
  vi.unstubAllGlobals()
  __resetAuthRecoveryStateForTests()
})

function headerNames(init: RequestInit | undefined): string[] {
  const h = init?.headers
  if (!h) return []
  if (h instanceof Headers) return [...h.keys()]
  if (Array.isArray(h)) return h.map(([k]) => k.toLowerCase())
  return Object.keys(h).map((k) => k.toLowerCase())
}

type Group = Record<string, unknown>
const FACTORY_MODULES = import.meta.glob<Record<string, unknown>>('../api/client/*.ts', { eager: true })

/** `client.ts`'s facade, rebuilt as the list of groups it spreads, in order. */
function spreadGroups(): { name: string; group: Group }[] {
  const source = readFileSync(resolve(process.cwd(), 'src/api/client.ts'), 'utf-8')
  const factories = new Map<string, (t: unknown) => Record<string, Group>>()
  for (const mod of Object.values(FACTORY_MODULES)) {
    for (const [name, value] of Object.entries(mod)) {
      if (/^create\w+Endpoints$/.test(name) && typeof value === 'function') {
        factories.set(name, value as (t: unknown) => Record<string, Group>)
      }
    }
  }
  // The factories close over the transport; nothing here calls an endpoint.
  const transport = new Proxy({}, { get: () => () => { throw new Error('transport called while building groups') } })
  const domains = new Map<string, Record<string, Group>>()
  for (const [, local, factory] of source.matchAll(/^const (\w+) = (create\w+Endpoints)\(transport\)/gm)) {
    const create = factories.get(factory)
    expect(create, `${factory} is not exported from src/api/client/*.ts`).toBeTypeOf('function')
    domains.set(local, create!(transport))
  }
  const facade = source.slice(source.indexOf('export const api = {'))
  return [...facade.matchAll(/^ {2}\.\.\.(\w+)\.(\w+),$/gm)].map(([, domain, group]) => {
    const value = domains.get(domain)?.[group]
    expect(value, `api spreads ${domain}.${group}, which its factory does not return`).toBeTypeOf('object')
    return { name: `${domain}.${group}`, group: value! }
  })
}

/** Same member: same function source, or the same shape for a nested namespace. */
function sameMember(a: unknown, b: unknown): boolean {
  if (typeof a === 'function' && typeof b === 'function') return a.toString() === b.toString()
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const ak = Object.keys(a)
    return ak.join() === Object.keys(b).join()
      && ak.every((k) => sameMember((a as Group)[k], (b as Group)[k]))
  }
  return a === b
}

describe('api facade composition', () => {
  const groups = spreadGroups()

  it('reads the spread groups it checks', () => {
    expect(groups.length, 'no `...domain.group,` spread found in client.ts').toBeGreaterThan(0)
    for (const { name, group } of groups) expect(Object.keys(group).length, name).toBeGreaterThan(0)
  })

  it('takes each member from exactly one group', () => {
    const owner = new Map<string, string>()
    for (const { name, group } of groups) {
      for (const key of Object.keys(group)) {
        expect(owner.get(key), `${key} is defined by both ${owner.get(key)} and ${name}; the later spread wins`)
          .toBeUndefined()
        owner.set(key, name)
      }
    }
  })

  it('serves every group member unchanged, so no inline member shadows one', () => {
    const facade = api as unknown as Group
    for (const { name, group } of groups) {
      for (const [key, member] of Object.entries(group)) {
        expect(key in facade, `${name}.${key} is missing from api`).toBe(true)
        expect(sameMember(facade[key], member), `api.${key} is not ${name}.${key}: a later member replaced it`)
          .toBe(true)
      }
    }
  })
})

describe('members that send no X-Session-Key', () => {
  it.each(NO_SESSION_KEY)('%s', async (name) => {
    // A dotted name is a member of a nested namespace (`teams.list`).
    const [head, member] = name.split('.')
    const owner = (member ? (api as unknown as Record<string, object>)[head] : api) as Record<string, (...args: unknown[]) => unknown>
    const fn = owner[member ?? head].bind(owner)
    await Promise.resolve(fn(...(ARGS[name] ?? ['sw-1', 'sw-2', 'sw-3', 'sw-4']))).catch(() => {})
    expect(fetchMock, `${name} issued no request`).toHaveBeenCalled()
    for (const [, init] of fetchMock.mock.calls as [string, RequestInit | undefined][]) {
      expect(headerNames(init), name).not.toContain('x-session-key')
    }
  })
})
