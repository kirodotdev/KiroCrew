/**
 * The dashboard's API client: the `api` singleton and the transport under it.
 *
 * This module owns the transport and its auth recovery: the `X-Session-Key`
 * default, the request helpers, the `j`/`jNullable` parsers that journal every
 * failure as an `ApiError`, the 403 `X-Auth-Required` silent refresh, the
 * embedded-pane hand-off, the re-auth banner and the stale-owner prompt. It
 * installs those helpers as the blessed `apiTransport` at load.
 *
 * The endpoints themselves live by domain under `./client/`, one module per
 * product area, each a `create*Endpoints` factory handed that transport. `api`
 * is assembled here from their segments, spread in the order the methods have
 * always had, so it stays one plain object: `vi.spyOn(api, name)` and the
 * `{ ...mod.api, name: vi.fn() }` mock factories keep working. The wire types
 * those modules own are re-exported below, so every import path is unchanged.
 */
import { installSessionExpiryHandler } from './sessionExpirySignal'
import { ApiError, friendlyErrText, toApiError } from './apiError'
import { refreshOnce, __resetRefreshOnceForTests } from './refreshOnce'
import {
  STALE_OWNER_SESSION_CODE,
  installStaleOwnerHandler,
  noteStaleOwnerResponse,
} from './staleOwnerSignal'
import { edgeChallengeMessage, noteEdgeAuthChallenge } from './edgeAuthChallenge'
import { beginArtifactWrite, endArtifactWrite } from '../lib/artifactWrites'
import { withDeadline } from '../lib/withDeadline'
import { installApiTransport } from './apiTransport'
import { isDeadlineError, queryClient, invalidateAcrossQueryClients } from './queryClient'
import { recordError, attachReport, parseErrorCode, requestPath } from '../utils/errorReport'
import { i18nT } from '../i18n/t'
import type { ClientTransport } from './client/transport'
import { createSystemEndpoints } from './client/system'
import { createTelemetryEndpoints } from './client/telemetry'
import { createChatEndpoints } from './client/chat'
import { createOnboardingEndpoints } from './client/onboarding'
import { createSecurityEndpoints } from './client/security'
import { createRemoteAccessEndpoints } from './client/remoteAccess'
import { createFeatureDiscoveryEndpoints } from './client/featureDiscovery'
import { createThemesEndpoints } from './client/themes'
import { createInstancesEndpoints } from './client/instances'
import { createCloudEndpoints } from './client/cloud'
import { createMemoryEndpoints } from './client/memory'
import { createSessionsEndpoints } from './client/sessions'
import { createMcpEndpoints } from './client/mcp'
import { createAgentsEndpoints } from './client/agents'
import { createChatSlotSettingsEndpoints } from './client/chatSlotSettings'
import { createFilesEndpoints } from './client/files'
import { createCronEndpoints } from './client/cron'
import { createHooksEndpoints } from './client/hooks'
import { createSkillsEndpoints } from './client/skills'
import { createSteeringEndpoints } from './client/steering'
import { createConnectionsEndpoints } from './client/connections'
import { createConfigEndpoints } from './client/config'
import { createCapabilityManagerEndpoints } from './client/capabilityManager'
import { createVoiceEndpoints } from './client/voice'
import { createSourceControlEndpoints } from './client/sourceControl'
import { createMonitorsEndpoints } from './client/monitors'
import { createChatOrganizationEndpoints } from './client/chatOrganization'
import { createNotificationsEndpoints } from './client/notifications'
import { createSubagentsEndpoints } from './client/subagents'
import { createApprovalsEndpoints } from './client/approvals'
import { createTaskRunnerEndpoints } from './client/taskRunner'
import { createWorkflowsEndpoints } from './client/workflows'
import { createUpdatesEndpoints } from './client/updates'
import { createAgentChannelsEndpoints } from './client/agentChannels'
import { createAppsEndpoints } from './client/apps'
import { createArtifactsEndpoints } from './client/artifacts'
import { createBrowserAndComputerUseEndpoints } from './client/browserAndComputerUse'
import { createDecisionsEndpoints } from './client/decisions'
import { createMessagingEndpoints } from './client/messaging'
import { createAutoResearchEndpoints } from './client/autoResearch'
import type { DecisionsConsentData } from './client/decisions'

export type { TunnelStatus } from './client/system'
export type { WakaTimeStatsEntry, WakaTimeStats } from './client/telemetry'
export type {
  KiroPrerequisiteStatus,
  KasLoginStatus,
  KasLoginDeviceSession,
  KasLoginPollResult,
  KasLoginLoopbackSession,
  AgentImportCategory,
  AgentImportSource,
  AgentImportSkipped,
  AgentImportScanResponse,
  AgentImportSelection,
  AgentImportConflictStrategy,
  AgentImportApplyRequest,
  AgentImportSummary,
  AgentImportApplyResponse,
} from './client/onboarding'
export type {
  DeniedCommandRule,
  DeniedUserRule,
  AwsConsentStatus,
  FileDeliveryGrant,
  FileDeliveryConsentStatus,
  CredentialRedactionState,
  ArmedFileDeliveryConsent,
  DeniedCommandsData,
  GovernanceScopeDetail,
  GovernanceScope,
  GovernanceDistributionData,
  GovernancePolicyData,
  PostureItem,
  PostureControl,
  SecurityPostureData,
  TrustedAppsData,
  TrustedAppsRevokeResult,
  ManagedSecret,
  SecretsListResponse,
} from './client/security'
export type {
  TailnetStatusData,
  TailnetMobileStep,
  TailnetMobileData,
  TailnetMobileMutation,
  MobileConnectMethodsData,
  TailnetMobileConfigure,
  TailnetMobileQr,
} from './client/remoteAccess'
export type {
  SuggestionKind,
  SuggestionItem,
  FeatureVideo,
  FeatureVideoNext,
  FeatureVideoProbe,
  FeatureVideoStatus,
} from './client/featureDiscovery'
export type {
  InstanceTunnelStatus,
  SsoStatus,
  InstanceView,
  AddInstanceBody,
} from './client/instances'
export { filenameFromDisposition } from './client/instances'
export type {
  CloudCoords,
  CloudPreflight,
  RemoteProvisioner,
  LaunchJobStatus,
  LaunchStepState,
  LaunchStep,
  CloudLaunchSignin,
  KiroLoginTarget,
  CloudIdentity,
  LaunchJob,
  LaunchTaskSighting,
  LaunchTaskReport,
} from './client/cloud'
export type { MemoryCarveQuery, MemoryCarveResult } from './client/memory'
export { SEARCH_MIN_CHARS } from './client/sessions'
export type {
  McpShareReason,
  McpShareRecommendation,
  McpMeasureProgress,
  McpManagedServer,
} from './client/mcp'
export type {
  MemberRosterRow,
  MemberActivityEntry,
  CrewTeam,
  CrewPanelData,
  CrewPanelMeta,
} from './client/agents'
export { SLASH_COMMANDS_TIMEOUT_MS } from './client/chatSlotSettings'
export type {
  WebhookFreshness,
  WebhookOutcome,
  WebhookTokenEntry,
  WebhookContextEntry,
  WebhookRunRecord,
  WebhooksView,
  WebhookTokenCreated,
  WebhookTestResult,
} from './client/hooks'
export type { SkillScriptValidation } from './client/skills'
export { SKILLS_TIMEOUT_MS } from './client/skills'
export { FILE_SEARCH_TIMEOUT_MS, BROWSE_FILES_TIMEOUT_MS } from './client/files'
export type {
  ConnectionMintState,
  ConnectionStatus,
  ConnectionOAuthClientSource,
  ConnectionOAuthClient,
  ConnectionOAuthClientSave,
  ConnectionTestResult,
} from './client/connections'
export type { AcpBackendInstalled, AcpBackendProbe } from './client/config'
export type { MonitorWrite, MonitorResponse } from './client/monitors'
export type {
  ChannelFolderBackfillMoved,
  ChannelFolderBackfillReport,
} from './client/chatOrganization'
export type { PlanStepInput } from './client/taskRunner'
export type {
  WorkflowLineage,
  WorkflowDefinitionRevision,
  WorkflowDefinition,
  WorkflowDefinitionWrite,
} from './client/workflows'
export type {
  InstallStreamResult,
  ExternalRegistryRow,
  FileMenuSurface,
  FileMenuContext,
} from './client/apps'
export type { AppPublishProvider } from './client/artifacts'
export { browserInstallConflictJob } from './client/browserAndComputerUse'
export type {
  BrowserInstallData,
  BrowserEngine,
  BrowserEngineStatus,
  BrowserInstallJob,
  BrowserViewData,
  BrowserOpenData,
  ComputerUsePermissions,
  ComputerUseConfigData,
  ComputerUseConfigSave,
} from './client/browserAndComputerUse'
export type {
  DecisionsConsentData,
  DecisionPointData,
  DecisionFeedbackSide,
  DecisionVerdictValue,
} from './client/decisions'
export type {
  SlackConfigData,
  SlackConfigSave,
  DiscordConfigData,
  TelegramConfigData,
  DiscordConfigSave,
  TelegramConfigSave,
  WeComConfigData,
  WeComConfigSave,
  FeishuConfigData,
  FeishuConfigSave,
  WebexConfigData,
  WebexConfigSave,
  IMessageConfigData,
  IMessageConfigSave,
  TeamsConfigData,
  WeixinConfigData,
  TeamsConfigSave,
  WeixinConfigSave,
  WhatsAppGroup,
  WhatsAppConfigData,
  WhatsAppConfigSave,
} from './client/messaging'

let _sessionExpiredShown = false

/**
 * True while the banner on screen is the stale-owner variant. A separate latch
 * because that session is still AUTHENTICATED: ordinary polls keep succeeding,
 * so the `j` wrapper's clear-banner-on-2xx self-dismissal would remove the one
 * instruction that recovers the owner-gated surfaces. It clears via the
 * banner's own X or a successful in-banner token exchange, never via a 2xx.
 */
let _staleOwnerBanner = false

/**
 * Synchronous getter so React components can read the auth-banner state on
 * mount (e.g. when the banner was already injected before the component
 * subscribed to the `mc-auth-required` / `mc-auth-cleared` events).
 */
export function isAuthBannerShown(): boolean {
  return _sessionExpiredShown
}

/**
 * Internal: fire a window-level CustomEvent so React components can react
 * to auth-banner state transitions. The banner itself is a vanilla DOM
 * element managed by this module; the events let consumers (e.g.
 * `ChatPage`) suppress redundant offline UI when auth is the real blocker.
 *
 * Two events, and the difference is load-bearing. `mc-auth-cleared` means
 * only THE BANNER IS GONE, which includes the reader dismissing it with its
 * X while the session is still broken. `mc-auth-recovered` means
 * AUTHENTICATION WORKS AGAIN, and is emitted from `removeAuthBanner` alone --
 * every one of whose callers is gated on a 2xx or on an accepted token
 * exchange. A consumer that acts on recovery (dropping a stale auth failure)
 * must read the second; a consumer that only mirrors banner presence reads
 * the first.
 */
function _emitAuthEvent(
  kind: 'mc-auth-required' | 'mc-auth-cleared' | 'mc-auth-recovered',
): void {
  if (typeof window === 'undefined') return
  try { window.dispatchEvent(new CustomEvent(kind)) } catch { /* ignore */ }
}

/**
 * Clear the session-expired banner if it is currently shown.
 * Called automatically from the `j` response wrapper on any 2xx response so
 * the banner self-dismisses once auth is restored (e.g. via a successful poll
 * after gateway restart wiped the session table). The in-banner token paste
 * calls it directly once its exchange succeeds, since that path deliberately
 * issues no request whose 2xx would reach `j`.
 *
 * Idempotent: safe to call on every response.
 */
export function removeAuthBanner(): void {
  // A 2xx means auth works again — clear the terminal-refresh latch so a later
  // lapse retries silently instead of going straight to the banner.
  _silentRefreshExhausted = false
  // The stale-owner banner is exempt from the 2xx self-dismissal: that session
  // still authenticates for everything the owner gate does not front, so a
  // success proves nothing about the stale-subject denial.
  if (_staleOwnerBanner) return
  // Auth works again, so a LATER lapse in this same document deserves its own
  // hand-off. Deliberately after the stale-owner return above: that denial is not
  // disproved by an unrelated success, and re-asking the hub for it loops forever.
  _embeddedHandoffPosted = false
  if (!_sessionExpiredShown) return
  _sessionExpiredShown = false
  const el = document.getElementById('mc-session-expired')
  if (el) el.remove()
  _emitAuthEvent('mc-auth-cleared')
  // Reaching here means a caller proved auth works: every call site is behind a
  // 2xx or an accepted token exchange. The banner's own X does NOT come through
  // here -- it tears the banner down inline -- so this event, unlike
  // `mc-auth-cleared`, is never fired by a reader simply dismissing the notice.
  _emitAuthEvent('mc-auth-recovered')
}

// Reactive warm-path recovery: background-poll 403s funnel here, through the
// shared single-flight refreshOnce(). True if the 30-day cookie rotated.
let _silentRefreshExhausted = false
// One hub hand-off per pane DOCUMENT. Without this every 403 from every
// background poll posts another `mc-auth-expired`, and the hub answers each one
// with an SSH token mint (rate-limited, never stopped) — a mint storm behind a
// loading spinner. A hub re-mint reloads this iframe, which resets this latch,
// so a pane that can recover still gets a fresh ask on every load.
let _embeddedHandoffPosted = false

/** Hand auth recovery to the hub, at most once per pane document.
 *
 * The wildcard target is deliberate and matches the two call sites' comments:
 * the hub's origin is not knowable from inside the pane (tunnel hosts vary), and
 * the message carries only a fixed type string — no secret — while the parent
 * validates `event.origin` before acting on it (see resolveTunnelOrigin).
 */
function postAuthExpiredToHub(): boolean {
  if (_embeddedHandoffPosted) return true
  try {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent.postMessage({ type: 'mc-auth-expired' }, '*')
  } catch {
    return false // cross-origin parent unreachable — caller falls back to the banner
  }
  _embeddedHandoffPosted = true
  return true
}

export function attemptSilentRefresh(): Promise<boolean> {
  return refreshOnce().then((res) => {
    if (res.ok) {
      // Keep the scheduler's ['auth-me'] cache from holding a stale
      // pre-rotation session_exp after a warm-path recovery.
      void queryClient.invalidateQueries({ queryKey: ['auth-me'] })
      return true
    }
    // 401 = terminal (chain revoked / no cookie) → latch to banner; 5xx is transient.
    if (res.status === 401) _silentRefreshExhausted = true
    return false
  })
}

/** Test-only: reset module auth-recovery state between cases. */
export function __resetAuthRecoveryStateForTests(): void {
  _silentRefreshExhausted = false
  _embeddedHandoffPosted = false
  _sessionExpiredShown = false
  _staleOwnerBanner = false
  __resetRefreshOnceForTests()
  if (typeof document !== 'undefined') {
    document.getElementById('mc-session-expired')?.remove()
  }
}

/**
 * Exchange a pasted token for a session cookie WITHOUT leaving the page.
 *
 * The auth middleware reads `?token=` AHEAD of the session cookie on every path,
 * and because such a token did not arrive from the cookie it writes the session
 * cookie onto that response once the handler returns (`dashboard/token_auth.py`,
 * the `if not from_cookie` branch). `GET /api/auth/me` is deliberately kept off
 * the bypass list so it runs the full auth path. One credentialed request on that
 * endpoint therefore establishes the session in place.
 *
 * This used to be `window.location.href = ...?token=...`, which authenticated by
 * exactly the same mechanism and threw away every piece of in-memory state on the
 * way. Whatever the user had typed into the panel that prompted the re-auth went
 * with it -- for Settings -> Secrets that was a credential they had already
 * pasted, which is the loss this replaces (#12240).
 *
 * Answers false for any non-2xx and for a transport failure, including the 404 an
 * older gateway gives for this endpoint. The caller keeps the banner up on false,
 * so a server that cannot exchange in place leaves the user exactly where they
 * were rather than half-signed-in.
 */
/**
 * What one in-banner exchange established.
 *
 * `reached` is false only when the request never got an answer -- an unreachable
 * gateway, a dropped connection. It is separate from `ok` because the two need
 * different words: a refused token asks the user to paste a better one, while an
 * unreachable gateway makes "not accepted" an assertion about a check that never
 * ran.
 *
 * `ok` means some credential is live: enough to drop a plain-expiry banner,
 * which was raised because nothing was. `tokenAccepted` is the gateway's answer
 * to the narrower question -- is the token in THIS request what authenticated it
 * -- and `ownerOk` to whether that caller also clears the owner gate. A token
 * minted before the owner was configured is valid, so it can be accepted and
 * still denied; resolving an owner denial needs both. A gateway that predates
 * either field leaves it false, which keeps the prompt up rather than dismissing
 * one it cannot vouch for.
 */
type PasteExchange = {
  reached: boolean
  ok: boolean
  tokenAccepted: boolean
  ownerOk: boolean
}

const EXCHANGE_UNREACHABLE: PasteExchange = {
  reached: false,
  ok: false,
  tokenAccepted: false,
  ownerOk: false,
}

async function exchangePastedToken(token: string): Promise<PasteExchange> {
  try {
    const r = await fetch('/api/auth/me?token=' + encodeURIComponent(token), {
      credentials: 'include',
    })
    if (!r.ok) return { reached: true, ok: false, tokenAccepted: false, ownerOk: false }
    try {
      const body = (await r.json()) as
        | { token_accepted?: unknown; owner_ok?: unknown }
        | null
      return {
        reached: true,
        ok: true,
        tokenAccepted: body?.token_accepted === true,
        ownerOk: body?.owner_ok === true,
      }
    } catch {
      // 2xx with an unreadable body: the session is live, but nothing vouches
      // for the pasted token, so it counts as the unproven case.
      return { reached: true, ok: true, tokenAccepted: false, ownerOk: false }
    }
  } catch {
    return EXCHANGE_UNREACHABLE
  }
}

/**
 * Render *sentence* into *el* with the command wrapped in a <code> element.
 *
 * The command comes from `api.client.reauth_command`, so the value split on is
 * the SAME value a translator sees, and no untranslated literal sits in this
 * module. Falls back to the plain sentence when the command is absent from it,
 * because a translation that moved or dropped it must still be readable --
 * losing the chip is a styling regression, whereas rendering nothing would be a
 * blank banner. `ApiClient.coverage.test.tsx` pins the relationship across every
 * catalog, so a translation that breaks it reddens CI rather than silently
 * costing the chip.
 */
function setInstructionWithCommandChip(el: HTMLElement, sentence: string): void {
  const command = i18nT('api.client.reauth_command')
  const at = command ? sentence.indexOf(command) : -1
  if (at < 0) {
    el.textContent = sentence
    return
  }
  el.append(document.createTextNode(sentence.slice(0, at)))
  const chip = document.createElement('code')
  chip.textContent = command
  el.append(chip, document.createTextNode(sentence.slice(at + command.length)))
}

function showSessionExpiredBanner(lead?: string): void {
  if (_sessionExpiredShown) return
  _sessionExpiredShown = true
  _emitAuthEvent('mc-auth-required')
  const el = document.createElement('div')
  el.id = 'mc-session-expired'
  el.style.cssText =
    'position:fixed;top:0;left:0;right:0;z-index:99999;background:#b91c1c;color:#fff;' +
    'padding:12px 20px;text-align:center;font:14px/1.5 system-ui;'
  const b = document.createElement('b')
  b.textContent = lead ?? i18nT('api.client.session_expired')
  const input = document.createElement('input')
  input.type = 'text'
  input.placeholder = i18nT('api.client.paste_token_url_or_raw_token')
  input.style.cssText =
    'margin-left:12px;padding:4px 8px;border-radius:4px;border:1px solid #fca5a5;' +
    'background:#7f1d1d;color:#fff;font-size:13px;width:280px;cursor:text;caret-color:#fff;' +
    'outline:2px solid transparent;outline-offset:2px;transition:border-color 0.2s,box-shadow 0.2s;'
  input.addEventListener('focus', () => { input.style.borderColor = '#fff'; input.style.boxShadow = '0 0 0 3px rgba(255,255,255,0.25),0 0 20px rgba(255,255,255,0.1)' })
  input.addEventListener('blur', () => { input.style.borderColor = '#fca5a5'; input.style.boxShadow = 'none' })
  // A refused paste has to say so. The exchange's only other cue is the field
  // re-enabling, which is indistinguishable from nothing having happened, so
  // without this the user presses Enter again and concludes the banner is
  // broken. `role="status"` announces the text when it appears, since a sighted
  // user sees it arrive and a screen-reader user otherwise would not.
  //
  // A `div`, and deliberately unstyled: block layout puts it on its own line
  // without an inline-spacing rule, and it inherits the banner's white-on-red
  // type, which clears contrast at this size where a lighter red would not.
  const failure = document.createElement('div')
  failure.setAttribute('role', 'status')
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const v = input.value.trim()
      if (!v) return
      let t: string | null = null
      try { t = new URL(v).searchParams.get('token') } catch { t = v }
      if (!t) return
      // Disabled for the round trip so a second Enter cannot start a second
      // exchange. The pasted text is left in the field: on a refusal the user
      // corrects it rather than re-pasting from scratch.
      input.disabled = true
      // Drop the previous attempt's refusal now, so the next one is visibly a
      // new answer rather than text that was already on screen.
      failure.textContent = ''
      void exchangePastedToken(t).then(({ reached, ok, tokenAccepted, ownerOk }) => {
        input.disabled = false
        if (!reached) {
          // The request never got an answer, so nothing judged the token. Saying
          // it was not accepted would assert a check that never ran and send the
          // user to re-run a command that cannot help.
          failure.textContent = i18nT('api.client.token_exchange_unreachable')
          input.focus()
          return
        }
        if (!ok) {
          failure.textContent = i18nT('api.client.token_not_accepted')
          input.focus()
          return
        }
        // An owner denial is resolved by ONE event: a credential that both
        // authenticates AND clears the owner gate. A 2xx here establishes
        // neither. `/api/auth/me` is not owner-gated, so this session -- still
        // authenticated, still owner-denied -- answers 200 on its cookie, and
        // the middleware quietly falls back to that cookie when the pasted token
        // is invalid. A token minted before the owner was configured is valid
        // too, so it is accepted and still denied everywhere the gate fronts.
        // Clearing the latch on the status alone, or on acceptance alone, would
        // hide the prompt while every owner-gated call kept failing, and the
        // user would learn that from their next refused save.
        //
        // So the latch drops only when the gateway reports both. Without them
        // the banner stays up and says the token was not accepted, which is the
        // accurate reading of a 200 that proves neither.
        if (_staleOwnerBanner && !(tokenAccepted && ownerOk)) {
          failure.textContent = i18nT('api.client.token_not_accepted')
          input.focus()
          return
        }
        // `removeAuthBanner` returns early while the latch is set --
        // deliberately, because that session keeps answering 2xx on everything
        // the owner gate does not front, so an unrelated success proves
        // nothing. Dropping it here is what lets the banner clear on its own
        // recovery, which the previous full-page reload hid by wiping the
        // document.
        _staleOwnerBanner = false
        // Auth works again. `removeAuthBanner` drops the banner and clears the
        // terminal-refresh latch, so a LATER lapse in this document retries the
        // silent path instead of going straight back to the banner.
        removeAuthBanner()
        // Only queries that failed AND are holding nothing get refetched.
        //
        // `status === 'error'` alone is not enough, and the earlier version of
        // this comment was wrong to say an error-state query has no data to sync
        // from. React Query KEEPS the last successful `data` when a later
        // refetch fails: measured against `@tanstack/query-core`, a query that
        // succeeds and then fails a refetch reports `status: 'error'` with its
        // `data` still defined and unchanged. Refetching one of those
        // re-delivers data to whatever watches it, and
        // `McpCustomServerModal`'s effect runs
        // `setText(JSON.stringify(specQuery.data.spec, ...))` on every change of
        // `specQuery.data` -- so an unsaved spec draft would be silently
        // overwritten by the server's copy. A no-argument
        // `invalidateQueries()` does that to every panel at once, which is the
        // wider version of the same bug.
        //
        // `data === undefined` narrows it to queries that never carried a
        // successful value: exactly the ones the lapse broke, and the only ones
        // with nothing to overwrite a draft with.
        invalidateAcrossQueryClients({
          predicate: (q) => q.state.status === 'error' && q.state.data === undefined,
        })
      })
    }
  })
  // The connective text around the command used to sit here as two bare English
  // It used to be two bare English fragments wrapped around a <code> element,
  // which left the banner untranslated everywhere while the panel's own error
  // card was translated. A key per fragment is not the fix: the i18n gate
  // rejects a value that ends mid-sentence, because the translator cannot
  // reorder around a sibling it never sees -- and several languages need the
  // command somewhere English does not put it. `api.client.
  // session_expired_sign_in_again` already carries this same command inline in a
  // whole sentence across every locale, so this follows that precedent. A `div`
  // so it needs no inline spacing rule.
  //
  // The command still gets its own <code> element, found by splitting the
  // rendered sentence on the command itself rather than by a placeholder: that
  // keeps one whole translatable sentence in one key AND keeps the command
  // visually separable, which is the whole reason a reader can tell where it
  // begins and ends.
  const instruction = document.createElement('div')
  setInstructionWithCommandChip(
    instruction,
    i18nT('api.client.run_kirocrew_token_then_paste_sign_in_url'),
  )
  el.append(b, instruction, input, failure)
  const dismiss = document.createElement('button')
  dismiss.textContent = '✕'
  dismiss.style.cssText =
    'margin-left:12px;background:none;border:none;color:#fca5a5;cursor:pointer;font-size:18px;vertical-align:middle;'
  dismiss.addEventListener('click', () => {
    el.remove()
    _sessionExpiredShown = false
    _staleOwnerBanner = false
    _emitAuthEvent('mc-auth-cleared')
  })
  el.append(dismiss)
  document.body.prepend(el)
  requestAnimationFrame(() => input.focus())
}

export function checkSessionExpired(r: Response): Response {
  if (r.status === 403 && r.headers.get('X-Auth-Required') === 'true' && !_sessionExpiredShown) {
    // When this dashboard is running embedded in the Instances pane stack
    // (an <iframe> inside the hub), don't show the paste-token banner here —
    // the user can't easily fetch the remote token from inside the pane, and
    // the hub owns recovery.
    //
    // But try OUR OWN recovery first. This pane holds the same 30-day
    // `mc_refresh_<port>` cookie the top-level path below uses, and the common
    // cause of a burst of 403s here is a lapsed access cookie (a laptop that
    // slept through the proactive refresh) — which one silent refresh fixes, with
    // no SSH mint, no iframe reload, and no lost pane state. Handing that to the
    // hub instead costs a remote mint and a full reload of this document.
    //
    // Only when the refresh cannot recover do we signal the parent, which
    // force-mints a fresh token and reloads this iframe. That hand-off is
    // latched to once per document (see postAuthExpiredToHub): the hub answers
    // every ask with a mint, so an unrepairable session used to produce one mint
    // per rate-limit window for as long as the window stayed open.
    if (window.parent && window.parent !== window) {
      if (!_silentRefreshExhausted) {
        void attemptSilentRefresh().then((ok) => {
          if (ok) removeAuthBanner()
          else postAuthExpiredToHub()
        })
        return r
      }
      if (postAuthExpiredToHub()) return r
      // Cross-origin parent unreachable — fall through to the banner below.
    }
    // Mid-session the access cookie can lapse (20h TTL, or laptop sleep
    // pausing the proactive refresh timer) while the tab stays open. The
    // background polls then 403 in a burst. Before showing the re-auth banner,
    // try a single-flight silent refresh with the still-valid 30-day cookie —
    // this recovers without ever showing the banner. Only banner if the
    // refresh can't recover (chain revoked / no refresh cookie).
    if (!_silentRefreshExhausted) {
      void attemptSilentRefresh().then((ok) => {
        if (ok) removeAuthBanner()
        else if (_silentRefreshExhausted) showSessionExpiredBanner()
      })
      return r
    }
    showSessionExpiredBanner()
  }
  return r
}

/**
 * Recovery prompt for the ONE denial the silent-refresh path can never clear:
 * a session whose token was minted before `KIROCREW_OWNER_ID` was configured.
 * `/api/auth/refresh` re-mints from the incoming subject, so a "successful"
 * refresh would rotate the cookie and keep the stale bootstrap subject — the
 * next owner-gated call is denied again, forever. Only a fresh sign-in (a new
 * token link, whose subject is derived from the now-configured owner) recovers,
 * so this goes straight to the banner instead of attempting a refresh.
 */
function handleStaleOwnerSession(): void {
  // Latch FIRST, even when a banner is already showing: a plain-expiry banner
  // raised moments earlier would otherwise keep its clear-on-2xx self-dismissal
  // and vanish on the next successful poll — this session still succeeds on
  // everything the owner gate does not front, so once the stale denial is seen
  // only the X or a successful in-banner token exchange may clear the prompt.
  _staleOwnerBanner = true
  if (_sessionExpiredShown) return
  // Embedded in the Instances pane stack: hand recovery to the hub, mirroring
  // checkSessionExpired — the hub force-mints a fresh token (whose subject is
  // derived from the current owner) and reloads this iframe.
  // No silent refresh attempt here, unlike checkSessionExpired: re-minting from
  // the incoming subject keeps the stale bootstrap subject, so a "successful"
  // refresh would rotate the cookie and be denied again. The hand-off is latched
  // to once per document, and `_staleOwnerBanner` above keeps a later 2xx from
  // re-opening it — a hub re-mint cannot fix this denial either, so asking twice
  // only buys another SSH mint.
  if (typeof window !== 'undefined' && window.parent && window.parent !== window) {
    if (postAuthExpiredToHub()) return
    // Cross-origin parent unreachable — fall through to the banner below.
  }
  showSessionExpiredBanner(i18nT('api.client.stale_owner_session'))
}

// The signal module is a leaf shared with the direct-fetch surfaces (app-sdk,
// the MCP-app relay, Mochi's approval bridge); this module owns the banner, so
// it supplies the prompt those detections raise. Re-exported so consumers of
// the blessed transport can reference the wire contract from one place.
installStaleOwnerHandler(handleStaleOwnerSession)
installSessionExpiryHandler(checkSessionExpired)
export { STALE_OWNER_SESSION_CODE }

/**
 * `ApiError` and `friendlyErrText` now live in the side-effect-free
 * `api/apiError` module so app API clients can import them without pulling this
 * file's graph (queryClient, transport install, the error journal) into their
 * bundles. Re-exported here because this has always been their import path —
 * every existing consumer, and every test that mocks `../api/client`, is
 * unchanged by the move.
 */
export { ApiError, friendlyErrText, toApiError }

/**
 * Whether *e* is a failure the user can only clear by signing back in.
 *
 * The gateway's auth denial names the cryptographic reason it rejected the
 * token (`invalid signature`, `session revoked`), which is accurate and
 * useless to a user: it neither says the session is what broke nor points at
 * the re-auth banner. Call sites use this to swap a futile retry for the one
 * action that recovers.
 *
 * An interposed proxy's challenge is deliberately NOT one of these, though it
 * also sets `authRequired`: the gateway never saw that request, so its sign-in
 * banner and token flow cannot clear it, and offering them names the wrong
 * system. Those failures carry their own remedy in the message instead. Call
 * sites that only want the retry withdrawal read `authRequired` directly.
 */
export const isAuthExpiredError = (e: unknown): boolean =>
  e instanceof ApiError && e.authRequired && !e.edgeChallenge

/**
 * Build the ApiError AND journal it.
 *
 * `j`/`jNullable` are the single chokepoint every dashboard API failure passes
 * through, which makes this the one place that can capture the full context
 * (status, path, backend `code`, raw body) before call sites collapse it to
 * `e.message`. `utils/errorReport` then lets a shared error banner recover that
 * context from the message alone — see AskAgentButton / ErrorNotice.
 */
const apiFailure = (r: Response, errText: string, benign?: BenignDenial): ApiError => {
  // An auth denial's own reason text ("invalid signature") describes HMAC
  // verification, not anything the user can act on, and every card that renders
  // it hides the fact that one re-auth clears all of them at once. Substitute
  // the recovery instruction for display; the raw reason still travels in
  // `body` and in the error report's `detail` for diagnostics.
  const authRequired = r.status === 403 && r.headers.get('X-Auth-Required') === 'true'
  // The stale-owner signal is matched on status AND the backend's code — a
  // generic 401 (or any 403) keeps its current handling untouched. Detection
  // lives HERE rather than in checkSessionExpired because the code travels in
  // the BODY, which checkSessionExpired (a pre-body Response hook) cannot read;
  // the prompt itself is idempotent, so the factory raising it cannot spam.
  const staleOwnerSession = noteStaleOwnerResponse(r.status, errText)
  // A third denial neither of the above can see: a proxy in front of the gateway
  // answered with its own sign-in page, so the signals are status + type + body.
  // Skipped when the gateway's own header is present: that header proves the request
  // reached the gateway, so nothing interposed answered it.
  const edgeOutcome = authRequired || staleOwnerSession
    ? null
    : noteEdgeAuthChallenge(r.status, r.headers.get('content-type'), errText)
  // Every one of these needs a person: the gateway never saw the request, so a silent
  // retry a second later reproduces it whether a session lapsed or a firewall refused.
  const edgeAuthExpired = edgeOutcome !== null
  const message = staleOwnerSession
    ? i18nT('api.client.stale_owner_session_sign_in_again')
    : authRequired
      ? i18nT('api.client.session_expired_sign_in_again')
      : edgeChallengeMessage(edgeOutcome)
        || friendlyErrText(r.status, errText)
        || `HTTP ${r.status}`
  const code = parseErrorCode(errText)
  // One specific denial on one endpoint is a DESIGNED, benign signal rather than a
  // failure worth showing the user — a disabled optional feature answering its own
  // probe (instances is deny-by-default; see listInstances). The caller opts that
  // one out via `benign`: the ApiError is still THROWN so the caller's catch runs,
  // but it is not journaled, so it cannot surface as a spurious error report on an
  // unrelated route (e.g. /chat/new-session mounting the sidebar).
  //
  // The match is on status AND the gateway's own `code`, never status alone. The
  // same endpoint answers 403 to a non-owner caller and to a Slack-origin request,
  // and both are real authorization failures a reader needs; keyed on status they
  // would be silently swallowed along with the routine one.
  //
  // The three auth-recovery denials above are additionally excluded, including the
  // edge challenge: a proxy answering with its own sign-in page carries no code of
  // ours, so it cannot match `benign`, but the term is kept explicit because each of
  // the three needs a person and none may ever be opted out by a call site.
  const expectedBenign = !!benign
    && r.status === benign.status
    && code === benign.code
    && !authRequired
    && !staleOwnerSession
    && !edgeAuthExpired
  const report = expectedBenign
    ? undefined
    : recordError({
      source: 'api',
      message,
      status: r.status,
      code,
      endpoint: requestPath(r.url),
      detail: errText,
    })
  // A stale-owner denial is authRequired in the sense call sites care about:
  // no retry can succeed until the user signs in again.
  //
  // The journal entry rides on the error itself (`attachReport`): the journal is
  // resolved by exact message, newest first, so two reads refused with the same
  // server line ("Access denied") would otherwise both resolve to whichever failed
  // LAST and hand the agent the wrong endpoint. `reportForError` reads this first.
  // A benign denial is not journaled, so it has no report to pin.
  const error = new ApiError(
    r.status, message, errText,
    authRequired || staleOwnerSession || edgeAuthExpired,
    edgeAuthExpired,
  )
  return report ? attachReport(error, report) : error
}

/**
 * The auth-recovery half of `j` for a response that is handed back RAW instead
 * of parsed (the chat-core transport's wire): the pre-body 403 `X-Auth-Required`
 * hook, and the body-borne stale-owner code a 401 carries -- read off a CLONE so
 * the caller's own `json()` still works. Fire-and-forget: the recovery prompts
 * are idempotent and the receipt read must not wait on them. A 2xx also clears
 * a stale session-expired banner, exactly as `j` does -- a send that succeeds
 * after auth was restored elsewhere must not leave the banner up.
 */
function sendResponseAuthRecovery(r: Response): Response {
  checkSessionExpired(r)
  if (r.ok) removeAuthBanner()
  if (r.status === 401) {
    // Best-effort: a wire may hand back a Response-like without `clone`.
    try {
      void r.clone().text().then((body) => noteStaleOwnerResponse(r.status, body)).catch(() => {})
    } catch { /* not a real Response; nothing to read */ }
  }
  return r
}

/**
 * A non-2xx this endpoint's caller handles itself, identified by the denial it
 * IS rather than by the status it arrives with.
 *
 * Status alone is not enough to identify a denial, and on the motivating
 * endpoint it is actively wrong: `/api/instances` answers 403 for a disabled
 * feature, for a non-owner caller, and for a Slack-origin request, and only the
 * first is routine. `code` is the machine-readable discriminator the gateway
 * emits for exactly that case, so both must match before anything is opted out.
 */
type BenignDenial = { readonly status: number; readonly code: string }

/**
 * The one parser body behind `j`, `jNullable` and `jInstancesDisabled`.
 *
 * `benign` and `nullOn204` are the ONLY differences between the three, so they
 * share this rather than holding copies that drift as the auth-recovery steps
 * above change.
 */
const parseJson = async (r: Response, benign?: BenignDenial, nullOn204 = false) => {
  checkSessionExpired(r)
  if (r.ok) removeAuthBanner()
  // Before the !r.ok branch, because 204 IS ok: the banner clear above still runs,
  // exactly as it did when this was its own copy of the body.
  if (nullOn204 && r.status === 204) return null
  if (!r.ok) {
    const errText = await r.text()
    throw apiFailure(r, errText, benign)
  }
  return r.json()
}

const j = (r: Response) => parseJson(r)

/**
 * Nullable variant of j(): preserves auth recovery + ApiError semantics but
 * returns null on 204 (No Content). Used by tips endpoints.
 */
const jNullable = (r: Response) => parseJson(r, undefined, true)

/**
 * The one denial in the dashboard that is a DESIGNED, benign signal rather than
 * a failure worth journaling: `/api/instances` answering its own list probe on
 * an install where the control plane is simply off.
 *
 * Deliberately NOT a `(status, code)` parameter pair. A parser taking those
 * would hand every domain module a general "ignore this status everywhere"
 * opt-out, and there is exactly one denial that has earned it. A second one
 * would be a second constant here, reviewed on its own merits — which is the
 * point: each addition is a visible decision at the facade rather than a call
 * site quietly passing different arguments.
 */
const INSTANCES_DISABLED: BenignDenial = { status: 403, code: 'instances_disabled' }

/**
 * `j` for the one benign denial above — nothing else.
 *
 * `j`'s exact semantics (auth recovery, `ApiError` on non-2xx) EXCEPT that a 403
 * carrying `instances_disabled` is thrown but NOT recorded in the error journal.
 * Any other denial on that same endpoint, INCLUDING another 403, journals
 * normally: `/api/instances` also answers 403 to a non-owner caller and to a
 * Slack-origin request, and both are real authorization failures a reader needs.
 *
 * Why it exists: the instances control plane is deny-by-default
 * (`instances.enabled` off), so a 403 to its own list probe is expected on most
 * installs. Journaling it made the sidebar's routine `['instances']` query
 * publish a spurious "/api/instances -> 403" error report on whatever route
 * mounted the sidebar (e.g. /chat/new-session).
 *
 * Handed to the domain modules through `ClientTransport` rather than imported by
 * them, for the reason that interface's own docstring gives: a module must reach
 * the SAME parser objects the facade installs, or an edition's calls and core's
 * calls diverge.
 */
const jInstancesDisabled = (r: Response) => parseJson(r, INSTANCES_DISABLED)

/** Add transport failures that have no HTTP Response to the same journal as apiFailure.
 *
 *  The journaled report is also PINNED to the rejection (`attachReport`). Every deadline here
 *  rejects with the one contract message, so a notice resolving its report by message alone
 *  (`findReport`) lands on whichever bounded read timed out LAST -- and hands the agent another
 *  read's endpoint. The notice reads the pinned report first (`reportForError`). */
function withJournaledDeadline<T>(
  ms: number,
  outer: AbortSignal | undefined,
  endpoint: string,
  attempt: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  return withDeadline(ms, outer, attempt).catch((error: unknown) => {
    if (isDeadlineError(error)) {
      const report = recordError({
        source: 'api',
        message: error instanceof Error ? error.message : String(error),
        code: 'timeout',
        endpoint,
      })
      // `isDeadlineError` has already established this is a non-null object.
      attachReport(error as object, report)
    }
    throw error
  })
}

// X-Session-Key ensures the server-side ephemeral gate always runs.
// Without it, browser requests would skip the `if sk:` check — a fail-open
// path that an MCP subprocess could exploit by omitting its own header.
const _sk = { 'X-Session-Key': 'dashboard:ui' }

/**
 * Count a mutating request against the artifact it targets, so the leave-time
 * cleanup can tell an unacknowledged write from a document nobody touched.
 *
 * Hooked HERE, at the transport, rather than in each caller: the previous design
 * asked every write path to announce itself and repeatedly shipped one that did
 * not, letting a document be deleted with its own PATCH still in the air. A
 * request cannot be issued without passing through these five helpers, so this
 * cannot be forgotten by a new call site. `settle` itself is excluded — it is the
 * cleanup, not a user write, and counting it would have it guard against itself.
 */
const ARTIFACT_WRITE_RE = /\/api\/artifacts\/([^/?#]+)/
function trackArtifactWrite(url: string, res: Promise<Response>): Promise<Response> {
  const m = ARTIFACT_WRITE_RE.exec(url)
  if (!m || url.includes('/settle')) return res
  let slug: string
  try {
    slug = decodeURIComponent(m[1])
  } catch {
    slug = m[1]
  }
  beginArtifactWrite(slug)
  // `finally` on both paths: a FAILED write clears too, which is correct — the
  // server never applied it, so the record it re-reads is authoritative anyway.
  return res.finally(() => endArtifactWrite(slug))
}

const get = (url: string, sessionKey?: string, signal?: AbortSignal) =>
  fetch(url, { headers: { ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk) }, ...(signal ? { signal } : {}) })
const post = (
  url: string,
  body?: object,
  sessionKey?: string,
  extra?: HeadersInit,
  redirect?: RequestRedirect,
) =>
  trackArtifactWrite(url, fetch(url, {
    method: 'POST',
    // sessionKey overrides the shared `dashboard:ui` placeholder with the REAL
    // slot. The placeholder satisfies the server's `if sk:` gate but names no
    // actual session, so a restricted (incognito) slot was never recognised as
    // restricted and its writes were allowed through. Callers acting on behalf
    // of a specific chat slot must pass it.
    // `extra` carries a per-call precondition header (a view the server must
    // still agree with) without every caller re-implementing the header merge.
    // `redirect` is for a caller whose URL is not core's to choose: a validated
    // target that answers 3xx would otherwise be followed automatically, and the
    // check that approved the FIRST url never sees the second. Defaulted so no
    // existing caller changes behaviour.
    ...(redirect ? { redirect } : {}),
    headers: { 'Content-Type': 'application/json', ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk), ...extra },
    body: body ? JSON.stringify(body) : undefined,
  }))
const put = (url: string, body: object, sessionKey?: string, extra?: HeadersInit) =>
  trackArtifactWrite(url, fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json', ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk), ...extra }, body: JSON.stringify(body) }))
const del = (url: string, body?: object, sessionKey?: string, extra?: HeadersInit) =>
  trackArtifactWrite(url, fetch(url, { method: 'DELETE', headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk), ...extra }, body: body ? JSON.stringify(body) : undefined }))
const patch = (url: string, body: object, sessionKey?: string, signal?: AbortSignal) =>
  trackArtifactWrite(url, fetch(url, {
    method: 'PATCH',
    // Same override as post(): replace the shared `dashboard:ui` placeholder with
    // the REAL slot when the write belongs to a chat session, so the server's
    // restricted-session gate applies to it.
    headers: { 'Content-Type': 'application/json', ...(sessionKey ? { 'X-Session-Key': sessionKey } : _sk) },
    body: JSON.stringify(body),
    signal,
  }))

// Publish the blessed transport so a downstream edition can build its OWN typed
// API module on the SAME session-key-authenticated helpers as core methods
// (inheriting X-Session-Key + auth-recovery + ApiError), instead of forking this
// file or writing methods on raw fetch. See api/apiTransport.ts.
installApiTransport({ get, post, put, del, patch, j, jNullable })

// The Kiro credit-usage wire payload and the view model normalized from it stay
// defined here, side by side: `KiroAccountModal` and `api/kiroUsage.ts` read
// them from this module, and `./client/telemetry` imports them as types.
export interface KiroBonusCreditGrantPayload {
  name: string
  used: number
  total: number
  days_left?: number
}

export interface KiroUsagePayload {
  available?: boolean
  /**
   * Why usage is unavailable when `available` is false (`api_key_auth` or
   * `signin_required`); absent when the gateway simply holds no reading.
   */
  reason?: string
  credits_used?: number
  credits_covered?: number
  credits_overage?: number
  credits_plan?: number
  resets?: string
  plan?: string
  cost_usd?: number
  overage_rate?: number | string
  bonus_credits?: KiroBonusCreditGrantPayload[]
  stale?: boolean
  account?: string
  email?: string
  account_type?: string
  start_url?: string
}

/** `POST /api/sessions/usage/refresh` — the GET envelope plus the declined-scrape marker. */
export interface KiroUsageRefreshResponse {
  usage?: KiroUsagePayload
  skipped?: 'scrape_parked'
  retry_after?: number
}

export interface KiroBonusCreditGrant {
  name: string
  used: number
  total: number
  daysLeft?: number
}

export interface KiroCreditUsage {
  used: number
  limit: number
  overage: number
  resets?: string
  plan?: string
  costUsd?: number
  overageRate?: number
  bonusCredits: KiroBonusCreditGrant[]
  stale: boolean
  account?: string
  email?: string
  accountType?: string
  startUrl?: string
}

// Every endpoint owner under `./client/` is built on this one transport: the
// helper objects just installed as the blessed `apiTransport`, plus the recovery
// hooks the methods that read their own response call directly.
const transport: ClientTransport = {
  get,
  post,
  put,
  del,
  patch,
  j,
  jNullable,
  jInstancesDisabled,
  sessionKeyHeader: _sk,
  checkSessionExpired,
  removeAuthBanner,
  sendResponseAuthRecovery,
  withJournaledDeadline,
}

const system = createSystemEndpoints(transport)
const telemetry = createTelemetryEndpoints(transport)
const chat = createChatEndpoints(transport)
const onboarding = createOnboardingEndpoints(transport)
const security = createSecurityEndpoints(transport)
const remoteAccess = createRemoteAccessEndpoints(transport)
const featureDiscovery = createFeatureDiscoveryEndpoints(transport)
const themes = createThemesEndpoints(transport)
const instances = createInstancesEndpoints(transport)
const cloud = createCloudEndpoints(transport)
const memory = createMemoryEndpoints(transport)
const sessions = createSessionsEndpoints(transport)
const mcp = createMcpEndpoints(transport)
const agents = createAgentsEndpoints(transport)
const chatSlotSettings = createChatSlotSettingsEndpoints(transport)
const files = createFilesEndpoints(transport)
const cron = createCronEndpoints(transport)
const hooks = createHooksEndpoints(transport)
const skills = createSkillsEndpoints(transport)
const steering = createSteeringEndpoints(transport)
const connections = createConnectionsEndpoints(transport)
const config = createConfigEndpoints(transport)
const capabilityManager = createCapabilityManagerEndpoints(transport)
const voice = createVoiceEndpoints(transport)
const sourceControl = createSourceControlEndpoints(transport)
const monitors = createMonitorsEndpoints(transport)
const chatOrganization = createChatOrganizationEndpoints(transport)
const notifications = createNotificationsEndpoints(transport)
const subagents = createSubagentsEndpoints(transport)
const approvals = createApprovalsEndpoints(transport)
const taskRunner = createTaskRunnerEndpoints(transport)
const workflows = createWorkflowsEndpoints(transport)
const updates = createUpdatesEndpoints(transport)
const agentChannels = createAgentChannelsEndpoints(transport)
const apps = createAppsEndpoints(transport)
const artifacts = createArtifactsEndpoints(transport)
const browserAndComputerUse = createBrowserAndComputerUseEndpoints(transport)
const decisions = createDecisionsEndpoints(transport)
const messaging = createMessagingEndpoints(transport)
const autoResearch = createAutoResearchEndpoints(transport)

// Spread in the order the methods have always had, so the key order is
// unchanged. A key two segments both defined would silently take the later
// one; `ApiClient.refactor.facade.test.ts` holds every key to one owner.
export const api = {
  ...system.statusAndStorage,
  ...telemetry.usageReadouts,
  ...sessions.crewBoard,
  // Defined here rather than in ./client/telemetry: it reads
  // `api.wakatimeExportUrl` at call time, so replacing that member reroutes the
  // download.
  /** Download the export as a file. Fetches rather than navigating, so an
   *  upstream 502 raises here (surfaced through ErrorNotice) instead of
   *  replacing the dashboard with the raw error body. The saved filename comes
   *  from the endpoint's own sanitized Content-Disposition. */
  wakatimeExportDownload: async (start: string, end: string, format: 'csv' | 'json') => {
    const r = await get(api.wakatimeExportUrl(start, end, format))
    if (!r.ok) {
      throw await toApiError(r)
    }
    const blob = await r.blob()
    const cd = r.headers.get('Content-Disposition') || ''
    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/.exec(cd)
    const filename = (m && decodeURIComponent(m[1])) || `wakatime-hours-${start}-to-${end}.${format}`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  },
  ...chat.summaries,
  ...telemetry.privacyPosture,
  ...onboarding.readiness,
  ...security.posture,
  ...remoteAccess.mobileAccess,
  ...security.policies,
  ...featureDiscovery.suggestions,
  ...themes.branding,
  ...instances.registryAndTransfer,
  ...cloud.launcher,
  ...memory.memoryAndVectors,
  ...sessions.runtimes,
  ...telemetry.creditUsage,
  ...mcp.probeCache,
  ...agents.crew,
  ...chatSlotSettings.selection,
  ...files.projects,
  ...cron.jobs,
  ...security.secrets,
  ...cron.historyAndFolders,
  ...memory.lessons,
  // Defined here rather than in ./client/memory: learn-cron-dashboard.md names
  // this module as the home of `deleteLesson`.
  // The selector is sent only when it is a string: `""` names the global row
  // and a fragment names that scope's row, while an absent key deletes every
  // scope's same-rule row -- which is the only delete that can reach a row the
  // list reports as `null` (stored scope present but unusable). Passing `null`
  // through would be refused (400 repo_scope_not_string) rather than widened.
  // `selectors` are the row's own from the list: `scope` / `workspace` pick the
  // JSONL file (the route defaults to the global one, so a workspace row's delete
  // has to carry them back), and `exact` narrows the rule match to the whole
  // rule -- the route matches by SUBSTRING by default, which is right for a CLI
  // fragment and wrong for a table row that holds the full text ("use tabs"
  // would also take "always use tabs").
  deleteLesson: (
    rule: string,
    repoScope?: string | null,
    selectors?: { scope?: 'global' | 'workspace'; workspace?: string; exact?: boolean },
  ) =>
    del('/api/lessons', {
      rule,
      ...(typeof repoScope === 'string' ? { repo_scope: repoScope } : {}),
      ...(selectors?.scope ? { scope: selectors.scope } : {}),
      ...(selectors?.workspace ? { workspace: selectors.workspace } : {}),
      ...(selectors?.exact ? { exact: true } : {}),
    }).then(j) as Promise<{ ok: boolean }>,
  ...hooks.triggers,
  ...skills.library,
  ...steering.files,
  ...skills.curation,
  ...mcp.servers,
  ...connections.accounts,
  ...mcp.gateway,
  ...config.settings,
  ...telemetry.kiroUsage,
  ...capabilityManager.catalog,
  ...voice.speechToText,
  ...sourceControl.providers,
  ...chat.slotList,
  ...monitors.loops,
  ...chat.slots,
  ...chatOrganization.sidebar,
  ...chat.send,
  ...system.taskQueue,
  ...memory.knowledge,
  ...notifications.inbox,
  ...sessions.history,
  ...instances.peerReads,
  ...sessions.historyDetail,
  ...chat.composerAutocomplete,
  ...subagents.spawned,
  ...approvals.requests,
  ...system.logs,
  ...taskRunner.runs,
  ...files.reveal,
  ...system.diagnostics,
  ...workflows.engine,
  ...taskRunner.plans,
  ...updates.lifecycle,
  ...files.fileOps,
  ...themes.themeList,
  ...config.dashboardRead,
  ...featureDiscovery.featureVideos,
  ...config.dashboardWrite,
  ...themes.themeEditing,
  ...voice.voiceSettings,
  ...security.consents,
  ...voice.synthesis,
  ...agentChannels.channels,
  ...apps.platform,
  ...artifacts.library,
  // Defined here rather than in ./client/artifacts: the i18n gate reads the
  // query-string template below as copy, and a moved line counts as written.
  browseRemoteArtifacts: (provider: string, opts?: { scope?: string; q?: string; pageToken?: string }) =>
    get(
      `/api/remote-artifacts/${encodeURIComponent(provider)}/browse` +
        `?scope=${encodeURIComponent(opts?.scope ?? 'mine')}` +
        (opts?.q ? `&q=${encodeURIComponent(opts.q)}` : '') +
        (opts?.pageToken ? `&pageToken=${encodeURIComponent(opts.pageToken)}` : ''),
    ).then(j),
  ...artifacts.remoteAndComments,
  ...browserAndComputerUse.hostAutomation,
  ...decisions.consentRead,
  // Defined here rather than in ./client/decisions: decisions.md names this
  // module as the home of the optional-`enabled` consent write.
  // Enabling echoes the endpoint the card showed: the gateway binds consent to
  // that address and answers 409 if config.json moved it since the read.
  // `toolArgs` and `compaction` are each OMITTED when the caller does not pass one,
  // and that omission is meaningful: the gateway preserves the recorded scope for an
  // absent field, so an ordinary switch flip can neither grant nor erase it. Pass a
  // boolean only for the switch the owner actually acted on — including `false`,
  // because on this route a revoke has to be written and cannot be left out.
  // `enabled` is OPTIONAL, and leaving it out is what makes a scope write safe rather
  // than careful: a body that carries the switch can only carry what this client last
  // read, so a view read before a revoke turns egress back on. Omitted, the route reads
  // the switch and the endpoint off the keystone under its own lock, and the write moves
  // only the scopes named. A body with neither the switch nor a scope is a 400.
  saveDecisionsConsent: (
    enabled?: boolean,
    endpoint?: string,
    toolArgs?: boolean,
    compaction?: boolean,
    memoryText?: boolean,
  ) =>
    put('/api/decisions/consent', enabled === undefined
      ? {
        endpoint,
        ...(toolArgs === undefined ? {} : { tool_args: toolArgs }),
        ...(compaction === undefined ? {} : { compaction }),
        ...(memoryText === undefined ? {} : { memory_text: memoryText }),
      }
      : enabled
        ? {
          enabled,
          endpoint,
          ...(toolArgs === undefined ? {} : { tool_args: toolArgs }),
          ...(compaction === undefined ? {} : { compaction }),
          ...(memoryText === undefined ? {} : { memory_text: memoryText }),
        }
        : { enabled }).then(j) as Promise<DecisionsConsentData>,
  ...decisions.scopesAndFeedback,
  ...messaging.channelConfigs,
  ...autoResearch.drafts,
  ...apps.sessionStatus,
  ...autoResearch.campaigns,
  ...apps.fileMenu,
  ...artifacts.publishing,
  ...featureDiscovery.tips,
}
