import { EyeOff, Globe, Monitor, MessagesSquare, ShieldAlert, Webhook } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { desktopCapabilityLine, knownDesktopCapabilities, type DesktopCapability } from '../components/appstore/desktopCapabilities'
import { browserVerbLine, knownBrowserVerbs, type BrowserVerb } from '../components/appstore/browserVerbs'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { PREVIEW_APP_BROWSER_SDK } from '../utils/previewFlags'
import { chatLaunchLine, chatLaunchScopeLine, knownChatLaunch } from '../components/appstore/chatLaunch'

export { browserVerbLine, knownBrowserVerbs, knownDesktopCapabilities }

// The declared-grant boxes of the detail page's permissions card, and the
// reach lines of its "approve new permissions" notice. They live here because
// `AppDetailPage.tsx` is at the file line cap.

/** `permissions.workerAutoApprove`, marked while an update holds it back. */
export function WorkerAutoApproveBox({ staged }: { staged: boolean }) {
  return (
    <div className="rounded-md border border-warn/30 bg-warn-subtle px-2.5 py-2">
      <div className="flex items-start gap-2 text-text">
        <ShieldAlert size={13} className="mt-[2px] shrink-0 text-warn" />
        <span>{i18nT('pages.appDetailPage.worker_auto_approve_desc')}</span>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>workerAutoApprove</code>
        {staged && <span className="ml-1 text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
      </div>
    </div>
  )
}

/** `permissions.chatSlots`: the app opens and drives chat sessions of its own. */
export function ChatSlotsBox() {
  return (
    <div className="rounded-md border border-border px-2.5 py-2" data-testid="chat-slots">
      <div className="flex items-start gap-2 text-text">
        <MessagesSquare size={13} className="mt-[2px] shrink-0 text-muted" />
        <span>{i18nT('pages.appDetailPage.chat_slots_desc')}</span>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>chatSlots</code>
      </div>
    </div>
  )
}

/** `permissions.chatLaunch`: the person's chats the app's page may open; nothing when undeclared. */
export function ChatLaunchBox({ declared }: { declared: unknown }) {
  const launch = knownChatLaunch(declared)
  if (!launch) return null
  return (
    <div className="rounded-md border border-border px-2.5 py-2" data-testid="chat-launch">
      <div className="flex items-start gap-2 text-text">
        <MessagesSquare size={13} className="mt-[2px] shrink-0 text-muted" />
        <span>{chatLaunchLine(launch, 'detail')}</span>
      </div>
      <div className="mt-1 text-muted">{chatLaunchScopeLine(launch)}</div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>chatLaunch</code>
      </div>
    </div>
  )
}

/**
 * `permissions.webhookSource`: the app takes inbound webhooks on its own route, and the
 * security policy permits them under the app's own name.
 */
export function WebhookSourceBox({ source }: { source: string }) {
  return (
    <div className="rounded-md border border-border px-2.5 py-2" data-testid="webhook-source">
      <div className="flex items-start gap-2 text-text">
        <Webhook size={13} className="mt-[2px] shrink-0 text-muted" />
        <span>{i18nT('pages.appDetailPage.webhook_source_desc')}</span>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.webhook_source_id')}: <code data-testid="webhook-source-id">{source}</code>
      </div>
    </div>
  )
}

/** `permissions.hiddenSessions`: the app's own chats are left out of the session lists. */
export function HiddenSessionsBox() {
  return (
    <div className="rounded-md border border-border px-2.5 py-2" data-testid="hidden-sessions">
      <div className="flex items-start gap-2 text-text">
        <EyeOff size={13} className="mt-[2px] shrink-0 text-muted" />
        <span>{i18nT('pages.appDetailPage.hidden_sessions_desc')}</span>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>hiddenSessions</code>
      </div>
    </div>
  )
}

const REACH_VERBS = ['read', 'send'] as const

/** The known verbs in a `permissions.reachableByUserSessions` value. Anything else grants nothing. */
export function reachVerbs(value: unknown): string[] {
  return Array.isArray(value) ? REACH_VERBS.filter(verb => value.includes(verb)) : []
}

function reachLine(verb: string): string {
  return verb === 'send'
    ? i18nT('pages.appDetailPage.user_session_reach_send')
    : i18nT('pages.appDetailPage.user_session_reach_read')
}

/**
 * What `permissions.reachableByUserSessions` lets the owner's chats do. Each verb an
 * update holds back carries its own "not approved" tag, so an approved verb never
 * reads as waiting.
 */
export function UserSessionReachBox({ declared, staged }: { declared: string[]; staged: string[] }) {
  if (declared.length === 0) return null
  return (
    <div className="rounded-md border border-border px-2.5 py-2" data-testid="user-session-reach">
      <div className="flex items-start gap-2 text-text">
        <MessagesSquare size={13} className="mt-[2px] shrink-0 text-muted" />
        <div className="flex flex-col gap-0.5">
          {declared.map(verb => (
            <span key={verb} data-testid={`user-session-reach-${verb}`}>
              {reachLine(verb)}
              {staged.includes(verb) && <span className="ml-1 text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
            </span>
          ))}
        </div>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>reachableByUserSessions</code>
      </div>
    </div>
  )
}

/** The reach verbs an update holds back, one line each, for the approval notice. */
export function UserSessionReachStaged({ staged }: { staged: string[] }) {
  return (
    <>
      {staged.map(verb => (
        <div key={verb} className="text-[13px]" data-testid="staged-user-session-reach">{reachLine(verb)}</div>
      ))}
    </>
  )
}

/** The reconsent line for a held-back `permissions.workerAutoApprove`. */
export function WorkerAutoApproveStaged() {
  return <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_worker_auto_approve')}</div>
}

/**
 * What `permissions.desktopCapabilities` lets the app's desktop windows do. Each
 * capability an update holds back carries its own "not approved" tag.
 */
export function DesktopCapabilitiesBox({ declared, staged }: { declared: DesktopCapability[]; staged: DesktopCapability[] }) {
  if (declared.length === 0) return null
  return (
    <div className="rounded-md border border-border px-2.5 py-2" data-testid="desktop-capabilities">
      <div className="flex items-start gap-2 text-text">
        <Monitor size={13} className="mt-[2px] shrink-0 text-muted" />
        <div className="flex flex-col gap-0.5">
          {declared.map(cap => (
            <span key={cap} data-testid={`desktop-capability-${cap}`}>
              {desktopCapabilityLine(cap)}
              {staged.includes(cap) && <span className="ml-1 text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
            </span>
          ))}
        </div>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>desktopCapabilities</code>
      </div>
    </div>
  )
}

/** The desktop capabilities an update holds back, one line each, for the approval notice. */
export function DesktopCapabilitiesStaged({ staged }: { staged: DesktopCapability[] }) {
  return (
    <>
      {staged.map(cap => (
        <div key={cap} className="text-[13px]" data-testid="staged-desktop-capability">{desktopCapabilityLine(cap)}</div>
      ))}
    </>
  )
}

/**
 * What `permissions.browser` lets the app do on its own slots' browser tabs
 * (rfc-app-sdk-browser.md). Each verb not approved yet carries its own tag; with the
 * `app_browser_sdk` preview off, a line says approved verbs do nothing.
 */
export function BrowserVerbsBox({ declared, staged, preview }: { declared: BrowserVerb[]; staged: BrowserVerb[]; preview?: boolean }) {
  // An installed app's row says whether the gateway has the preview on; a store row
  // that is not installed has no grant row, so read the switch itself.
  const switchOn = usePreviewFlag(PREVIEW_APP_BROWSER_SDK)
  if (declared.length === 0) return null
  const previewOn = preview ?? switchOn
  return (
    <div className="rounded-md border border-border px-2.5 py-2" data-testid="browser-verbs">
      <div className="flex items-start gap-2 text-text">
        <Globe size={13} className="mt-[2px] shrink-0 text-muted" />
        <div className="flex flex-col gap-0.5">
          {declared.map(verb => (
            <span key={verb} data-testid={`browser-verb-${verb}`}>
              {browserVerbLine(verb)}
              {staged.includes(verb) && <span className="ml-1 text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
            </span>
          ))}
          {!previewOn && <span className="text-muted" data-testid="browser-preview-off">{i18nT('components.appstore.trustAppModal.browser_preview_off')}</span>}
        </div>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        {i18nT('pages.appDetailPage.session_approval_manifest_key')}: <code>browser</code>
      </div>
    </div>
  )
}

/** The browser verbs waiting for the owner, one line each, for the approval notice. */
export function BrowserVerbsStaged({ staged }: { staged: BrowserVerb[] }) {
  if (staged.length === 0) return null
  return (
    <>
      <div className="text-[13px]" data-testid="staged-browser-note">{i18nT('pages.appDetailPage.grants_reconsent_browser')}</div>
      {staged.map(verb => (
        <div key={verb} className="text-[13px]" data-testid="staged-browser-verb">{browserVerbLine(verb)}</div>
      ))}
    </>
  )
}

/** Held-back entries shown as code chips under a catalog line (browser sites, Kiro operations). */
export function StagedCodeList({ label, entries, testId }: { label: string; entries: string[]; testId: string }) {
  if (entries.length === 0) return null
  return (
    <div>
      <div className="text-[13px]">{label}</div>
      <div className="flex flex-wrap gap-1 mt-1" data-testid={testId}>
        {entries.map(o => (
          <code key={o} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{o}</code>
        ))}
      </div>
    </div>
  )
}
