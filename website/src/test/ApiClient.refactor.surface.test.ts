/**
 * Two properties of the `api` surface that a domain split can change while every
 * type and behavior test stays green: the order the members enumerate in, and
 * which members leave `X-Session-Key` off their request.
 *
 * Both lists were generated from the `client.ts` the split was cut from. The
 * order is the one the facade's spreads must reproduce; the header list is every
 * member whose request carried no session key there, raw `fetch` reads and writes
 * alike, so moving one onto the shared helpers (which add the key) fails here.
 * A new member joins `API_KEY_ORDER` where the facade spreads it, and joins
 * `NO_SESSION_KEY` only when it deliberately bypasses the helpers.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, __resetAuthRecoveryStateForTests } from '../api/client'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { __resetArtifactWrites } from '../lib/artifactWrites'

// `uploadFiles` downscales through a canvas helper that happy-dom lacks.
vi.mock('../utils/resizeImage', () => ({
  MODEL_IMAGE_LIMITS: { maxEdge: 8000, maxBytes: 3_750_000 },
  resizeImageForModel: vi.fn(async (file: File) => ({ file, info: null })),
}))

const API_KEY_ORDER = [
  'status', 'tunnelStatus', 'system', 'leakedRuntimes', 'reclaimLeakedRuntimes', 'sessionStorage',
  'sessionStorageCleanup', 'sessionStorageRestore', 'sessionStorageEmpty', 'sessionStorageEmptyStatus',
  'sessionInventory', 'sessionInventoryDetail', 'sessionInventoryTrash', 'sessionCrewLogProjections', 'sessionWorkProjection',
  'telemetryStartup', 'telemetryContextTrace', 'usageTurns', 'wakatimeStats',
  'wakatimeExportUrl', 'crewBoard', 'crewBoardAction', 'wakatimeExportDownload',
  'sessionSummary', 'dashboardCard', 'generateSessionSummary',
  'beaconStatus', 'collectionStatus', 'kiroPrerequisite', 'repairKiroPrerequisiteSpecs',
  'updateKiroPrerequisiteCli', 'kasLoginStatus', 'kasLoginBeginDevice', 'kasLoginPoll',
  'kasLoginBeginLoopback', 'kasLoginCancel', 'kasLoginLogout', 'onboardingImportScan',
  'onboardingImportApply', 'onboardingImportState', 'securityStats', 'securityPosture',
  'mobileLoginLink', 'mobileConnectMethods', 'tailnetStatus', 'tailnetMobile',
  'tailnetMobileConfigure', 'tailnetMobilePublish', 'tailnetMobileUnpublish', 'tailnetMobileQr',
  'deniedCommands', 'toggleBuiltinDeniedCommand', 'setDeniedCommandsDisableAll', 'addUserDeniedCommand',
  'toggleUserDeniedCommand', 'deleteUserDeniedCommand', 'redactionAllowedHosts', 'redactionAllowHost',
  'redactionRevokeHost', 'listTrustedApps', 'trustApp', 'untrustApp',
  'setTrustAllApps', 'listTrustedRegistries', 'grantTrustedRegistry', 'revokeTrustedRegistry',
  'governancePolicy', 'suggestions', 'branding',
  'listInstances', 'addInstance', 'updateInstance', 'removeInstance',
  'instanceStatus', 'connectInstance', 'refreshInstanceToken', 'disconnectInstance',
  'restartInstance', 'exportSession', 'importSessionFromFile', 'sendSessionToInstance',
  'cloudPreflight', 'cloudIamPolicy', 'cloudProvisioners', 'cloudLaunches',
  'cloudIdentity', 'cloudLaunch', 'cloudLaunchStatus', 'cloudLaunchTask',
  'cloudLaunchCancel', 'cloudLaunchSignin', 'cloudLaunchSigninRestart', 'cloudStop',
  'cloudStart', 'cloudDestroy', 'memoryPreferences', 'saveMemoryPreferences',
  'memoryProjects', 'saveMemoryProjects', 'memoryHistory', 'saveMemoryHistory',
  'memorySettings', 'saveMemorySettings', 'memoryStores', 'memoryRetired',
  'memoryRestoreRetired', 'memoryBackups', 'memoryBackupNow', 'memoryRestoreBackup',
  'cancelMemberMemoryRestore', 'memoryCarve', 'memoryRecall', 'memoryRecords',
  'memoryEditPreview', 'memoryEditPreviewPage', 'memoryRecordsRefresh', 'memoryQuerySelectionRefresh',
  'memoryRecordHistory', 'memoryEditApply', 'memberMemoryPage', 'memorySeed',
  'vectorSemantic', 'vectorSemanticWrite', 'vectorSemanticDelete', 'vectorEpisodic',
  'vectorEpisodicSearch', 'vectorEpisodicDelete', 'vectorStats', 'vectorEvents',
  'vectorEmbeddingStatus', 'vectorEnableEmbeddings', 'vectorValidateEmbedModel', 'vectorApplyEmbedModel',
  'vectorDisableEmbeddings', 'vectorImport', 'vectorContextPreview', 'memoryGraph',
  'consolidateMemory', 'restartSessions', 'sessionsMemory', 'sessionsUsage',
  'sessionsUsageRefresh', 'providerUsage', 'mcpProbeCache', 'agentsInstalled',
  'agentTemplates', 'agentTemplateCreate', 'agentTemplateDelete', 'agentDetail',
  'agentPatch', 'agentFork', 'agentPublish', 'agentReset',
  'kirocrewAgents', 'agentResolvedModel', 'agentCatalog', 'createKirocrewAgent',
  'members', 'memberThread', 'memberActivity', 'memberProjections',
  'memberPanel', 'memberBriefing', 'teams', 'updateKirocrewAgent',
  'deleteKirocrewAgent', 'appearances', 'uploadCrewAvatar', 'models',
  'chatSlotSelectionCapabilities', 'effortLevels', 'slashCommands', 'chatSlotAgent',
  'chatSlotModel', 'chatSlotAutocompact', 'setChatSlotAutocompact', 'chatSlotsModel',
  'chatSlotReasoningEffort', 'chatSlotWorkspace', 'chatSlotReload', 'chatSlotProject',
  'createWorktree', 'recentProjects', 'browseDirs', 'browseDrives',
  'browseFiles', 'projectGit', 'projectGitStatus', 'projectGitLog',
  'projectTree', 'workspaces', 'createWorkspace', 'updateWorkspace',
  'deleteWorkspace', 'crons', 'createCron', 'deleteCron',
  'batchDeleteCron', 'updateCron', 'runCron', 'cronSecretsGrant',
  'cancelCron', 'cronToChat', 'toggleCron', 'cronHistory',
  'cronRunDetail', 'cronScript', 'secretsList', 'secretsSave',
  'secretsDelete', 'ackCron', 'cronHistoryAll', 'cronFolders',
  'createCronFolder', 'updateCronFolder', 'deleteCronFolder', 'lessons',
  'createLesson', 'deleteLesson', 'hooks', 'kiroHooks',
  'createHook', 'updateHook', 'deleteHook', 'toggleHook',
  'testHook', 'webhooks', 'createWebhookToken', 'updateWebhookToken',
  'deleteWebhookToken', 'deleteWebhookContext', 'testWebhook', 'setWebhooksEnabled',
  'prompts', 'promptDetail', 'createPrompt', 'updatePrompt',
  'deletePrompt', 'skills', 'skillTrust', 'grantSkillTrust',
  'revokeSkillTrust', 'skill', 'skillTree', 'skillFile',
  'createSkill', 'updateSkill', 'deleteSkill', 'steeringFiles',
  'steeringFile', 'createSteering', 'updateSteering', 'deleteSteering',
  'skillsPending', 'skillPendingDetail', 'approvePendingSkill', 'dismissPendingSkill',
  'dismissAllPendingSkills', 'pinSkill', 'setSkillInjectOnTrigger', 'skillsBudget',
  'discoverSkills', 'previewDiscoveredSkill', 'installDiscoveredSkill', 'mcpServers',
  'mcpGlobalScopes', 'mcpDiscover', 'mcpDiscoverDetail', 'mcpDiscoverInstall',
  'mcpCustomAdd', 'mcpCustomGet', 'mcpCustomUpdate', 'mcpActive',
  'mcpProbe', 'mcpResetProbeFailures', 'mcpSync', 'mcpApply',
  'mcpToggle', 'mcpToggleTool', 'mcpToggleAll', 'mcpRemove',
  'mcpOAuthRelay', 'connectionsMint', 'connectionsMintState', 'connectionsPremint',
  'connectionsStatus', 'connectionsTest', 'connectionsCancel', 'connectionsDisconnect',
  'connectionsOAuthClients', 'connectionsOAuthClientSave', 'connectionsOAuthClientDelete', 'mcpGatewayStatus',
  'mcpGatewayEnable', 'mcpGatewayMetrics', 'mcpGatewayServers', 'mcpGatewayLaunchPreview',
  'mcpGatewaySetStub', 'mcpResolveRefresh', 'mcpMeasureStart', 'mcpMeasureProgress',
  'mcpGatewaySetStubMany', 'agentConfig', 'saveAgentConfig', 'defaultAgent',
  'setDefaultAgent', 'kirocrewConfig', 'saveKirocrewConfig', 'patchConfig',
  'acpBackends', 'acpBackendRecheck', 'kiroUsage', 'capabilityMcpList',
  'capabilityMcpInstall', 'capabilityMcpUninstall', 'capabilitySkillsList', 'capabilitySkillsInstall',
  'capabilitySkillsUninstall', 'capabilityAgentsList', 'capabilityAgentsInstall', 'capabilityAgentsUninstall',
  'capabilityPluginsList', 'capabilityPluginsSync', 'capabilityMcpRegistry', 'sttConfig',
  'saveSttConfig', 'sttStatus', 'sttVocabularies', 'sttPrepare', 'sttFfmpegDownload',
  'sttPrewarm', 'sttPolish', 'sttTranscribe', 'pullRequestSource',
  'pullRequestChecks', 'pullRequestStatuses', 'resolvePullRequestThread', 'unresolvePullRequestThread',
  'replyToPullRequestThread', 'commentOnPullRequest', 'enablePullRequestAutoMerge', 'markPullRequestReady',
  'pullRequestPendingReview', 'submitPullRequestReview', 'fetchIssueSource', 'appContributors',
  'chatSlots', 'autonudgeList', 'autonudgeForSlot', 'autonudgeResume', 'autonudgeDismiss', 'monitorsList',
  'monitorForSlot', 'monitorCreate', 'monitorUpdate', 'monitorStop',
  'monitorClear', 'monitorRestart', 'chatSlotSourceLinks', 'unlinkSourceLink', 'chatSlotDetail',
  'createChatSlot', 'chatSlotContext', 'deleteChatSlot', 'cleanupSessions',
  'stopChatSlot', 'stopChatSlotForce', 'cancelQueuedMessage', 'editQueuedMessage',
  'reorderQueuedMessages', 'interruptSlot', 'endWait', 'approveChatSlot',
  'resumeChatSlot', 'forkChatSlot', 'sideOpen',
  'sideTurn', 'sideQueueCancel', 'sideQueueEdit', 'sideClose', 'sideStop',
  'chatMode', 'generateTitle', 'resolveNavLinks', 'renameSlot', 'setTodoTask',
  'regenerateSlot', 'continueSlot', 'switchVariant', 'editResend',
  'rewind', 'slackLink', 'unlinkSlack', 'pauseSlack',
  'pauseMirror', 'channelTargets', 'linkMirror', 'remindMirror',
  'unlinkMirror', 'slackChannels', 'chatFolders', 'createChatFolder',
  'updateChatFolder', 'reorderChatFolders', 'deleteChatFolder', 'cleanupChatFolders', 'backfillChannelFolder',
  'setSlotFolder', 'setSlotColor', 'setSlotColorHex', 'clearSlotColor',
  'setSlotPin', 'chatTags', 'createChatTag',
  'adoptChatTag', 'updateChatTag', 'deleteChatTag', 'setSlotTags',
  'dropSlotToColumn', 'tagColumns', 'createTagColumn', 'updateTagColumn',
  'deleteTagColumn', 'reorderTagColumns', 'sendChat', 'sessionsHealth',
  'tasksSummary', 'tasksList', 'taskDetail', 'taskCancel',
  'knowledgeSearch', 'notifications', 'deleteNotification', 'clearNotifications',
  'ackNotification', 'unackNotification', 'ackAllNotifications', 'notificationChannels',
  'updateNotificationChannelSettings', 'sessions', 'sessionsSearch', 'instancesSearchSessions',
  'instancesCapabilities', 'instanceChatSlots', 'crewPeerGet', 'crewPeerPost', 'sessionDetail', 'deleteSession',
  'clearSessions', 'autocomplete', 'spawnList', 'spawn',
  'spawnStatus', 'spawnDelete', 'spawnStopAll', 'spawnRetry',
  'approvals', 'resolveApproval', 'pendingQuestions', 'answerQuestion',
  'dismissQuestionCard', 'logLevel', 'setLogLevel', 'taskRunnerStatus',
  'startTaskRunner', 'cancelTaskRunner', 'pauseTaskRun', 'deleteTaskRun',
  'retryTaskRun', 'renameTaskRun', 'updateTask', 'taskRunToChat',
  'revealPath', 'collectDiagnostics', 'workflowRuns', 'workflowDefinitions',
  'authorWorkflow', 'saveWorkflowDefinition', 'promoteWorkflowRun', 'updateWorkflowDefinition',
  'runWorkflowDefinition', 'refineTaskInput', 'refineStatus', 'refineCancel',
  'planTask', 'cancelPlan', 'updatePlan', 'executePlan',
  'planFromChat', 'planContext', 'exportPlanYaml', 'checkUpdate',
  'changelog', 'releases', 'applyUpdate', 'setAutoUpdate',
  'setUpdateChannel', 'restartGateway', 'armUpdate', 'armStatus',
  'dismissUpdateArm', 'cancelUpdate', 'simulateUpdate', 'pickFiles',
  'fileDiff', 'fileSearch', 'pathComplete', 'uploadFiles',
  'screenshot', 'themes', 'dashboardConfig', 'featureVideoNext',
  'featureVideoFeedback', 'featureVideoProbe', 'featureVideoStatus', 'featureVideoFetchAll',
  'updateDashboardConfig', 'createTheme', 'installTheme', 'updateTheme',
  'deleteTheme', 'themeDetail', 'themeBoot', 'updateThemeConfig',
  'voiceConfig', 'updateVoiceConfig', 'voiceVoices', 'voiceSystemVoices',
  'awsConsent', 'grantAwsConsent', 'revokeAwsConsent', 'fileDeliveryConsent',
  'armFileDeliveryConsent', 'fileDeliveryConsentArmStatus', 'revokeFileDeliveryConsent', 'credentialRedaction',
  'setCredentialRedaction', 'voiceSynthesize', 'voiceCancel', 'channelsList',
  'channelPresets', 'channelGet', 'channelCreate', 'channelClose',
  'channelPost', 'channelAddAgent', 'channelUpdateAgent', 'channelDismissAgent',
  'channelWakeAgent', 'channelApproveAgent', 'channelClearContext', 'listApps',
  'getApp', 'getAppManifest', 'installApp', 'enableApp',
  'disableApp', 'openApp', 'uninstallApp', 'uninstallPreview',
  'updateApp', 'migrateCleanup', 'listRegistry', 'listRegistries',
  'updateRegistries', 'refreshAppStore', 'refreshRegistries', 'installFromRegistry',
  'installFromRegistryStream', 'registerApp', 'artifacts', 'artifact',
  'artifactVersion', 'artifactVersions', 'artifactEvents', 'recordArtifactReference',
  'createArtifact', 'settleBlankArtifact', 'updateArtifact', 'deleteArtifact',
  'artifactFolders', 'createArtifactFolder', 'updateArtifactFolder', 'deleteArtifactFolder',
  'setArtifactFolder', 'setArtifactPinned', 'artifactSessionDocs', 'materializeArtifact',
  'publishArtifact', 'getArtifactPublishProviders', 'cloneRemoteArtifact', 'forkRemoteArtifact',
  'browseRemoteArtifacts', 'remoteArtifactDetail', 'remoteArtifactComments', 'postRemoteArtifactComment',
  'replyRemoteArtifactComment', 'markReviewRemoteComment', 'deleteRemoteComment', 'updateArtifactSharing',
  'unpublishArtifact', 'reprobeArtifactNotice', 'sandboxDocUrl', 'refreshArtifactSharing',
  'pullLatest', 'upstreamStatus', 'overwriteRemote', 'artifactComments',
  'postArtifactComment', 'replyArtifactComment', 'markCommentReview', 'resolveComment',
  'reopenComment', 'deleteArtifactComment', 'editArtifactComment', 'getBrowserInstall',
  'setBrowserToken', 'installBrowserCli', 'installBrowserEngine', 'getBrowserView',
  'startBrowserView', 'openInBrowser', 'getComputerUseConfig', 'saveComputerUseConfig',
  'getDecisionsConsent', 'getDecisionsProvider', 'getDecisionsLocalRuntime', 'saveDecisionsConsent', 'saveDecisionsScope',
  'saveDecisionsHistoryBudget', 'sendDecisionsFeedback', 'saveDecisionsProvider', 'removeDecisionsLocalModel', 'getSlackConfig', 'getSlackManifest', 'saveSlackConfig',
  'getDiscordConfig', 'saveDiscordConfig', 'getTelegramConfig', 'saveTelegramConfig',
  'getWeComConfig', 'getFeishuConfig', 'saveFeishuConfig', 'saveWeComConfig',
  'getWebexConfig', 'saveWebexConfig', 'getIMessageConfig', 'saveIMessageConfig',
  'getGovernanceChannels', 'getTeamsConfig', 'saveTeamsConfig', 'getWeixinConfig',
  'saveWeixinConfig', 'weixinQrStart', 'weixinQrStatus', 'getWhatsAppConfig',
  'saveWhatsAppConfig', 'whatsAppQrStart', 'whatsAppQrStatus', 'whatsAppUnlink',
  'getWhatsAppGroups', 'researchValidate', 'researchGrillExpand', 'appSessionStatus',
  'researchCampaigns', 'researchCampaign', 'researchCreate', 'researchAction',
  'researchGrillTree', 'researchNudge', 'researchAddQuestion', 'researchToKnowledge',
  'researchKnowledgeStatus', 'researchToArtifact', 'researchReportStatus', 'researchReport',
  'researchDelete', 'invokeFileMenuItem', 'artifactTeardown', 'publishProviders',
  'publishArtifactToCoreProvider', 'publishToProvider', 'tipsNext', 'tipsStatus',
  'tipsFeedback',
]

const NESTED_KEY_ORDER: Record<string, string[]> = {
  teams: ['list', 'create', 'update', 'remove'],
  appearances: ['list', 'detail', 'importBundle', 'remove'],
}

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
  'agentPublish', 'agentReset', 'agentResolvedModel', 'members',
  'memberActivity', 'memberProjections', 'memberPanel', 'memberBriefing',
  'teams.list', 'appearances.list', 'appearances.detail', 'uploadCrewAvatar',
  'models', 'chatSlotSelectionCapabilities', 'effortLevels', 'slashCommands',
  'chatSlotAutocompact', 'recentProjects', 'browseDirs', 'browseDrives',
  'browseFiles', 'projectGit', 'projectGitStatus', 'projectGitLog',
  'projectTree', 'workspaces', 'crons', 'updateCron',
  'cronFolders', 'updateCronFolder', 'lessons', 'hooks',
  'kiroHooks', 'webhooks', 'updateWebhookToken', 'prompts',
  'promptDetail', 'skill', 'skillTree', 'skillFile',
  'skillsPending', 'skillPendingDetail', 'mcpServers', 'mcpGlobalScopes',
  'mcpActive', 'connectionsMintState', 'connectionsStatus', 'connectionsOAuthClients',
  'mcpGatewayStatus', 'mcpGatewayMetrics', 'mcpGatewayServers', 'mcpGatewayLaunchPreview',
  'mcpMeasureProgress', 'agentConfig', 'defaultAgent', 'kirocrewConfig',
  'patchConfig', 'acpBackends', 'kiroUsage', 'capabilityMcpList',
  'capabilitySkillsList', 'capabilityAgentsList', 'capabilityPluginsList', 'capabilityMcpRegistry',
  'sttConfig', 'sttStatus', 'sttVocabularies', 'sttTranscribe', 'chatSlots',
  'autonudgeList', 'autonudgeForSlot', 'monitorsList', 'monitorForSlot',
  'chatSlotSourceLinks', 'chatSlotDetail', 'channelTargets', 'slackChannels',
  'sessionsHealth', 'tasksSummary', 'tasksList', 'taskDetail',
  'notifications', 'notificationChannels', 'sessions', 'sessionsSearch',
  'instancesSearchSessions', 'instancesCapabilities', 'instanceChatSlots', 'sessionDetail',
  'autocomplete', 'spawnList', 'spawnStatus', 'approvals',
  'pendingQuestions', 'logLevel', 'taskRunnerStatus', 'renameTaskRun',
  'updateTask', 'refineStatus', 'planContext', 'checkUpdate',
  'changelog', 'releases', 'armStatus', 'fileDiff',
  'fileSearch', 'pathComplete', 'uploadFiles', 'themes',
  'dashboardConfig', 'themeDetail', 'themeBoot', 'voiceConfig',
  'voiceVoices', 'voiceSystemVoices', 'awsConsent', 'fileDeliveryConsent',
  'fileDeliveryConsentArmStatus', 'credentialRedaction', 'channelsList', 'channelPresets',
  'channelGet', 'listApps', 'getApp', 'getAppManifest',
  'uninstallPreview', 'listRegistry', 'listRegistries',
]

// Members whose request is built from an argument's fields or bytes; every other
// member is called with the generic positional strings.
const ARGS: Record<string, unknown[]> = {
  memoryRecords: ['member-reviewer', { q: 'contact', kind: 'fact' }, 0, 50],
  memoryRecordHistory: ['member-reviewer', { kind: 'fact', id: 'user.contact' }, 25, 0],
  uploadFiles: [[new File(['abc'], 'a.txt', { type: 'text/plain' })]],
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

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockImplementation(async () => okJson())
  vi.stubGlobal('fetch', fetchMock)
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

describe('api key enumeration order', () => {
  it('is the order the split was cut from', () => {
    expect(Object.keys(api)).toEqual(API_KEY_ORDER)
  })

  it('keeps the nested namespaces in their order', () => {
    for (const [ns, order] of Object.entries(NESTED_KEY_ORDER)) {
      expect(Object.keys(api[ns as keyof typeof api] as object), ns).toEqual(order)
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
