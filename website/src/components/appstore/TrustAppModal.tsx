/**
 * TrustAppModal — the consent gate for third-party app code execution.
 *
 * A third-party app is refused execution until the user grants it trust, so both
 * `POST /api/apps/{name}/enable` and the registry INSTALL path (which checks the
 * gate before cloning, so the refusal arrives before anything is on disk) answer
 * with `code: "app_execution_denied"`. Surfacing that raw backend string left the
 * user with no way forward; this modal turns the refusal into an informed
 * decision and, on confirm, grants trust for THIS APP ONLY and retries whichever
 * action was refused.
 *
 * The trust check keys off the machine-readable CODE, never the English
 * message: `isTrustDeniedError` reads the structured error body, so a reworded
 * backend message can never silently turn the consent flow back into a raw
 * error card. It reads the body STRUCTURALLY (duck-typed `body` / `code`
 * fields) rather than via `instanceof ApiError`, so it keeps working both for
 * the ApiError-shaped rejections that page tests stub in AND for a RESOLVED
 * install result — the SSE stream reports a refused install as a `done` payload
 * carrying the code, not as a rejection.
 */
import { useCallback, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Code2, GitBranch, Loader2, Server, ShieldAlert, Terminal } from 'lucide-react'

import { api } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import Modal from '../Modal'
import ErrorNotice from '../ErrorNotice'
import { Badge, Btn } from '../ui'
import SessionApprovalModes from './SessionApprovalModes'
import { i18nT } from '../../i18n/t'
import { findReport } from '../../utils/errorReport'
import { sourceLabel, type RegistryApp } from './types'

/** Machine-readable code the enable and registry-install paths return when trust is missing. */
export const APP_EXECUTION_DENIED = 'app_execution_denied'
/** Machine-readable code returned when enable needs the detail disclosure. */
export const SESSION_APPROVAL_CONSENT_REQUIRED = 'session_approval_consent_required'
/**
 * Machine-readable code the registry install returns when the gateway is the
 * desktop app's bundled interpreter and the app's Python layout needs a build
 * step it cannot run. A PERMANENT condition for this gateway: retrying cannot
 * change it, which is why the consent modal keys its copy on this code rather
 * than on the sentence beside it.
 */
export const DESKTOP_BUILD_STEP_UNSUPPORTED = 'desktop_build_step_unsupported'

/**
 * The subset of an app row the consent modal needs: identity for the grant,
 * display name for the title, provenance (clone target + source label), and the
 * session approval grant so the user can see WHO and WHAT they are about to trust.
 */
export type TrustAppTarget = Pick<RegistryApp, 'name' | '_registry' | 'origin'> & {
  displayName?: string
  trustRepository?: string
  sessionApproval?: boolean
}

/** Mirror the backend's credential-free Git coordinate projection. */
export function credentialFreeRepository(raw?: string): string | undefined {
  if (!raw) return raw
  const schemeAt = raw.indexOf('://')
  if (schemeAt >= 0) {
    const scheme = raw.slice(0, schemeAt).toLowerCase()
    const authorityAt = schemeAt + 3
    const suffixCandidates = ['/', '?', '#']
      .map(delimiter => raw.indexOf(delimiter, authorityAt))
      .filter(index => index >= 0)
    const suffixAt = suffixCandidates.length ? Math.min(...suffixCandidates) : raw.length
    const authority = raw.slice(authorityAt, suffixAt)
    const pathAndSuffix = raw.slice(suffixAt)
    const secretSuffixAt = ['?', '#']
      .map(delimiter => pathAndSuffix.indexOf(delimiter))
      .filter(index => index >= 0)
    const publicPath = pathAndSuffix.slice(
      0,
      secretSuffixAt.length ? Math.min(...secretSuffixAt) : pathAndSuffix.length,
    )
    const userinfoAt = authority.lastIndexOf('@')
    if (userinfoAt < 0) return raw.slice(0, suffixAt) + publicPath

    const userinfo = authority.slice(0, userinfoAt)
    const hostport = authority.slice(userinfoAt + 1)
    if (scheme === 'ssh' || scheme === 'git+ssh') {
      const passwordAt = userinfo.indexOf(':')
      const username = passwordAt >= 0 ? userinfo.slice(0, passwordAt) : userinfo
      const safeAuthority = username ? `${username}@${hostport}` : hostport
      return raw.slice(0, authorityAt) + safeAuthority + publicPath
    }
    return raw.slice(0, authorityAt) + hostport + publicPath
  }
  // A standard SCP username is routing identity and must remain byte-exact.
  // For a remote-only proof, any colon before the first @ is the backend's
  // unsupported/ambiguous SCP shape, regardless of the suffix Git may parse.
  const firstColon = raw.indexOf(':')
  const firstAt = raw.indexOf('@')
  if (firstAt > 0 && firstColon >= 0 && firstColon < firstAt) return undefined
  return raw
}

/** Pull a machine-readable `code` out of an API rejection, if it carries one. */
function errorCode(e: unknown): string | null {
  if (!e || typeof e !== 'object') return null
  const direct = (e as { code?: unknown }).code
  if (typeof direct === 'string') return direct
  const body = (e as { body?: unknown }).body
  if (typeof body !== 'string' || !body.trim().startsWith('{')) return null
  try {
    const code = (JSON.parse(body) as { code?: unknown }).code
    return typeof code === 'string' ? code : null
  } catch {
    return null
  }
}

/**
 * True when a failure is the missing-trust gate rather than a real error.
 *
 * Accepts a rejection OR a resolved payload: a refused registry install comes
 * back as `{ ok: false, code: "app_execution_denied" }` on the SSE `done` event,
 * while a refused enable (and the non-streaming install route) rejects with the
 * code inside the response body.
 */
export function isTrustDeniedError(e: unknown): boolean {
  return errorCode(e) === APP_EXECUTION_DENIED
}

/** True when enable must continue from the detail disclosure surface. */
export function isSessionApprovalConsentRequiredError(e: unknown): boolean {
  return errorCode(e) === SESSION_APPROVAL_CONSENT_REQUIRED
}

/**
 * The server's own account of why the retried action failed, or `''`.
 *
 * A build refusal or a clone failure arrives on the failure the retry throws;
 * this lifts that string so the modal renders the reason beside its own copy
 * instead of leaving it readable only in `security_events.jsonl`.
 *
 * Deliberately the Error's OWN `message`, not a field re-parsed out of the
 * response body: `ApiError.message` is already the unwrapped `{"error": …}`
 * sentence AND the key the error journal recorded the request under, so
 * handing it to `ErrorNotice` as the message keeps the structured report
 * (endpoint, status, backend code) reachable for the ask-the-agent hand-off.
 *
 * A trust refusal returns `''`: `APP_EXECUTION_DENIED` is a machine code the
 * retry throws as a sentinel, not user-facing text, and the modal's own copy
 * already explains a grant that did not take effect.
 */
export function retryFailureDetail(e: unknown): string {
  if (isTrustDeniedError(e)) return ''
  const message = e instanceof Error ? e.message : typeof e === 'string' ? e : ''
  const trimmed = message.trim()
  if (!trimmed || trimmed === APP_EXECUTION_DENIED) return ''
  return trimmed
}

/**
 * The machine code the failure carried beside its reason, or `''`.
 *
 * Only a failure that also has a reason to show (`retryFailureDetail`) yields
 * one: the code selects HOW that reason is presented (a permanent condition
 * gets copy without a retry instruction), and a sentinel refusal with nothing
 * to show has nothing for a code to select.
 */
export function retryFailureCode(e: unknown): string {
  if (!retryFailureDetail(e)) return ''
  return errorCode(e) ?? ''
}

/**
 * Owns the consent-modal state and the grant-then-retry sequence, so the page
 * that owns the refused mutation drives one modal instead of each button
 * duplicating the flow.
 *
 * `retryEnable` is the DEFAULT action re-run once the grant lands (the page's own
 * enable path, including its cache invalidation). Two different gates now open
 * this same modal — enable, and the registry install, which checks the execution
 * gate before cloning — so `open()` takes an optional per-refusal retry: which
 * action to resume is a property of the refusal, not of the hook. The retry
 * travels WITH the target in one state object, so a confirm can never pair an
 * app with the wrong action.
 */
export function useTrustGate(retryEnable: (name: string) => Promise<void>) {
  // This hook mutates trust through `api.trustApp` / `api.untrustApp` directly
  // rather than a `useMutation`, so nothing invalidates the queries that render
  // the resulting state. Without this the Security panel's trusted-apps list and
  // the App Store's own rows keep serving a cached snapshot from before the
  // grant — a settings surface showing a stale answer about who may run code.
  // Invalidated (not hand-patched) because the authoritative post-grant shape is
  // whatever the server returns, including the `ineffective` split.
  const qc = useQueryClient()
  const refreshTrustViews = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['trusted-apps'] })
    qc.invalidateQueries({ queryKey: ['apps'] })
  }, [qc])

  const [gate, setGate] = useState<{
    app: TrustAppTarget
    retry: (name: string) => Promise<void>
  } | null>(null)
  const [pending, setPending] = useState(false)
  const [failed, setFailed] = useState(false)
  // The server's reason for the failure, shown under the generic headline. Reset
  // wherever `failed` is, so a second attempt never displays the first one's
  // cause beside a newer outcome.
  const [detail, setDetail] = useState('')
  // The machine code beside that reason (`retryFailureCode`), which selects how
  // the reason is presented: a permanent condition gets copy without a retry
  // instruction. Reset in step with `detail`, for the same reason.
  const [detailCode, setDetailCode] = useState('')
  // Whether `trustApp` SUCCEEDED before a failure. The two failure cases need
  // different advice: a landed grant leaves state the user should know about and
  // may want to remove; a failed grant changed nothing, and sending them to
  // Settings to "check and remove it" would send them after nothing.
  const [granted, setGranted] = useState(false)

  const open = useCallback((app: TrustAppTarget, retry?: (name: string) => Promise<void>) => {
    const credentialFree = credentialFreeRepository(app.trustRepository)
    // `trustRepository` is a server-owned opaque consent proof.  A client-side
    // rewrite would authorize a coordinate the user was not shown and the server
    // did not issue.  Use the sanitizer only as a defensive validator: if a
    // compromised/stale response is not already credential-free, fail closed
    // without rendering or submitting either the secret or a transformed proof.
    if (credentialFree !== app.trustRepository) {
      setGate(null)
      setPending(false)
      setFailed(false)
      setDetail('')
      setDetailCode('')
      setGranted(false)
      return
    }
    setGate({
      app: { ...app, trustRepository: credentialFree },
      retry: retry ?? retryEnable,
    })
    setPending(false)
    setFailed(false)
    setDetail('')
    setDetailCode('')
    setGranted(false)
  }, [retryEnable])

  const cancel = useCallback(() => {
    // Cancel grants nothing — it only drops the pending action.
    setGate(null)
    setPending(false)
    setFailed(false)
    setDetail('')
    setDetailCode('')
    setGranted(false)
  }, [])

  const confirm = useCallback(async () => {
    if (!gate || pending) return
    setPending(true)
    setFailed(false)
    setDetail('')
    setDetailCode('')
    // Local, not the `granted` state: state updates are not readable in this same
    // closure, and the catch below has to know whether the grant actually landed.
    let grantLanded = false
    try {
      await api.trustApp(gate.app.name, gate.app.trustRepository)
      grantLanded = true
      setGranted(true)
      await gate.retry(gate.app.name)
      refreshTrustViews()
      setGate(null)
    } catch (failure) {
      // Keep the modal open and report inline: the grant may have landed while
      // the retried action failed for an unrelated reason, and closing here
      // would strand the user back on the raw-error path this modal replaces.
      //
      // But a landed grant over a name NO app occupies is an orphan, and grants
      // are keyed on the name alone: whatever is installed under it next would
      // run its own code with no consent prompt. That is the exact state the
      // uninstall path refuses to create (`trust_grant_not_removed`), so this
      // flow must not create it either — the grant is rolled back when the app
      // it was for is not there.
      //
      // OBSERVED, not inferred from which retry ran. The install retry leaves
      // nothing on disk when it fails, the enable retry leaves the app installed,
      // and only the first produces an orphan — but asking the server which is
      // true beats trusting a caller to pass the right flag. When the check
      // itself fails we KEEP the grant: revoking on a guess would switch off an
      // app that exists and works, and the copy below already points the user at
      // Settings to review it.
      let rolledBack = false
      if (grantLanded) {
        try {
          const installed = await api.getApp(gate.app.name)
          if (installed == null) {
            await api.untrustApp(gate.app.name)
            rolledBack = true
          }
        } catch (probe) {
          // `GET /api/apps/{name}` answers 404 for a name no app occupies, and
          // `j()` turns that into a rejection — so "not installed" arrives HERE,
          // not as a null. Only a 404 is proof of absence; a network error or a
          // 500 is not, and revoking on those would switch off an app that
          // exists and works. Status read structurally, matching
          // `isTrustDeniedError` above: page tests stub ApiError-SHAPED objects
          // rather than real instances.
          if (isNotFoundError(probe)) {
            try {
              await api.untrustApp(gate.app.name)
              rolledBack = true
            } catch {
              // The rollback itself failed; the grant stands and the copy below
              // sends the user to Settings to remove it by hand.
              rolledBack = false
            }
          }
        }
      }
      // Either branch changed server-side trust state — the grant landed, or it
      // landed and was rolled back — so the views have to be refreshed even on
      // the failure path, which is exactly where a stale list is most confusing.
      if (grantLanded) refreshTrustViews()
      // A rolled-back grant means nothing was left behind, which is what the
      // `failed_generic` copy says; `granted` is what selects between the two.
      setGranted(grantLanded && !rolledBack)
      setFailed(true)
      // The reason the grant or the retry failed, straight from the failure it
      // threw (the catch covers both, and a failed grant's message is worth
      // showing too), and the code that says how to present it. Without the
      // reason the user sees the generic copy alone while the cause lives only
      // in the security event log — a dead end, since Try again fails identically.
      setDetail(retryFailureDetail(failure))
      setDetailCode(retryFailureCode(failure))
    } finally {
      setPending(false)
    }
  }, [gate, pending, refreshTrustViews])

  return {
    target: gate?.app ?? null,
    pending,
    failed,
    detail,
    detailCode,
    granted,
    open,
    cancel,
    confirm,
  }
}

/**
 * Return *raw* as an href ONLY if it is a scheme we can hand a user, else `null`.
 *
 * Repository text still reaches this dialog from a remote source. Parsed
 * with the URL constructor rather than a regex: a prefix check is defeated by
 * `\tjavascript:`, `JaVaScRiPt:`, and percent/entity encodings that the browser
 * still resolves, whereas `new URL().protocol` is the browser's own normalized
 * answer. Everything outside the allowlist — `javascript:`, `data:`, `blob:`,
 * `file:` — is refused, and a value the parser rejects outright is refused too.
 */
export function safeHref(raw: string): string | null {
  try {
    const url = new URL(raw)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null
  } catch {
    return null
  }
}

/**
 * The three capabilities a trust grant confers, each paired with its icon.
 *
 * The label is a RESOLVED STRING, not a key: `check-i18n-keys.mjs` can only
 * verify a key it sees as a literal at the `i18nT()` call site, so mapping over
 * a list of key strings and calling `i18nT(key)` in the row makes the reference
 * invisible to the gate (it reports the file as a dynamic-key site) and a typo
 * would reach users as a raw key. A function, not a module constant, because the
 * strings must re-resolve on a language switch rather than freeze at import.
 */
function capabilities(): { label: string; Icon: typeof Code2 }[] {
  return [
    { label: i18nT('components.appstore.trustAppModal.capability_python'), Icon: Code2 },
    { label: i18nT('components.appstore.trustAppModal.capability_backend'), Icon: Server },
    { label: i18nT('components.appstore.trustAppModal.capability_shell'), Icon: Terminal },
  ]
}

export default function TrustAppModal({
  app, pending, failed, detail = '', detailCode = '', granted, onCancel, onConfirm,
}: {
  /** The app awaiting consent; `null` keeps the modal closed. */
  app: TrustAppTarget | null
  pending: boolean
  failed: boolean
  /**
   * The server's reason for the failure, from `useTrustGate().detail`. Empty
   * when the failure carried none (or carried only a machine code), which keeps
   * the generic copy as the whole message rather than showing an empty detail.
   */
  detail?: string
  /**
   * The machine code beside that reason, from `useTrustGate().detailCode`. Only
   * `DESKTOP_BUILD_STEP_UNSUPPORTED` changes the rendering: it names a permanent
   * condition, so the notice drops the retry instruction and explains the reason
   * in plain words above the server's own sentence.
   */
  detailCode?: string
  /** True when the grant was written and only the retried action failed. */
  granted: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const name = app ? (app.displayName || app.name) : ''
  // Both keys resolved here as LITERALS: `check-i18n-keys.mjs` verifies only a
  // key it can read at the `i18nT()` call site, so selecting between two
  // resolved strings keeps both references visible to the gate.
  const headline = granted
    ? i18nT('components.appstore.trustAppModal.failed', { app: name })
    : i18nT('components.appstore.trustAppModal.failed_generic', { app: name })
  // A permanent refusal only while the grant did NOT stay: a grant left behind is
  // the more urgent fact (state the user may want to remove), and the `failed`
  // copy is what names it, so that case keeps the generic rendering below.
  const permanentRefusal =
    failed && !!detail && !granted && detailCode === DESKTOP_BUILD_STEP_UNSUPPORTED
  return (
    <Modal
      open={!!app}
      onClose={onCancel}
      maxWidth={560}
      title={i18nT('components.appstore.trustAppModal.title', { app: name })}
      footer={
        permanentRefusal ? (
          // Retrying cannot change what the bundled interpreter can install, and
          // the retry is a full clone/build that ends in the identical refusal and
          // then leans on the rollback probe to revoke the grant it just re-wrote.
          // So the one action left is to close; `app.close` is the shared label.
          <Btn primary onClick={onCancel}>
            {i18nT('app.close')}
          </Btn>
        ) : (
          <>
            <Btn onClick={onCancel} disabled={pending}>
              {i18nT('components.appstore.trustAppModal.cancel')}
            </Btn>
            <Btn primary onClick={onConfirm} disabled={pending}>
              {pending
                ? <><Loader2 size={14} className="animate-spin" /> {i18nT('components.appstore.trustAppModal.working')}</>
                : failed
                  ? <><ShieldAlert size={14} /> {i18nT('components.appstore.trustAppModal.confirm_after_failure')}</>
                  : <><ShieldAlert size={14} /> {i18nT('components.appstore.trustAppModal.confirm')}</>}
            </Btn>
          </>
        )
      }
    >
      {app && (
        <div className="flex flex-col gap-3.5 text-[13px]">
          <p className="text-muted leading-relaxed">
            {i18nT('components.appstore.trustAppModal.scope', { app: name })}
          </p>
          <p className="text-text leading-relaxed">
            {i18nT('components.appstore.trustAppModal.intro', { app: name })}
          </p>
          {/* Explicit color-mix rather than a `bg-card/88` opacity modifier:
              theme colors are raw `var(--x)`, so the translucency depends on
              the config's opacity shim rather than on Tailwind's own alpha
              path. Verified to emit
              `background-color: color-mix(in srgb,var(--card) 88%,transparent)`. */}
          <ul className="flex flex-col gap-2.5 rounded-lg border border-border bg-[color-mix(in_srgb,var(--card)_88%,transparent)] px-3.5 py-3">
            {capabilities().map(({ label, Icon }) => (
              <li key={label} className="flex items-start gap-2.5 text-text leading-relaxed">
                <Icon size={14} className="mt-[3px] shrink-0 text-warn" />
                <span>{label}</span>
              </li>
            ))}
          </ul>
          {/* The three rows are a CEILING, not a manifest reading: trust grants all
              three regardless of what this app happens to use, and Kiro Crew
              cannot narrow it. Listing them without saying so reads as "here is what it
              does", which would be a promise we do not keep. */}
          <p className="text-muted leading-relaxed">
            {i18nT('components.appstore.trustAppModal.capability_note')}
          </p>
          {/* The sessionApproval grant is NOT part of that ceiling: it is a separate,
              manifest-declared request enforced for app-token calls, so it lives in
              its own box under its own heading. Folding it into the list above made
              "all three" miscount the rows the user was consenting to. */}
          {app.sessionApproval && (
            <div className="flex flex-col gap-1.5 rounded-lg border border-warn/30 bg-warn-subtle px-3.5 py-3">
              <div className="flex items-center gap-2 font-semibold text-warn">
                <ShieldAlert size={14} className="shrink-0" />
                <span>{i18nT('components.appstore.trustAppModal.session_approval_heading')}</span>
              </div>
              <p className="text-text leading-relaxed">
                {i18nT('components.appstore.trustAppModal.session_approval_desc')}
              </p>
              <SessionApprovalModes label={i18nT('components.appstore.trustAppModal.session_approval_modes')} />
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 text-muted">
            <GitBranch size={14} className="shrink-0" />
            <span className="shrink-0">{i18nT('components.appstore.trustAppModal.source')}</span>
            {app.trustRepository && (
              /* Provenance is shown as a LINK only when the URL is one the user can
                 safely be handed: the clone target is remote content, and
                 rendering it into `href` unchecked makes `javascript:...` a
                 one-click script-execution vector in the dashboard's own origin —
                 on the very dialog whose job is to gate code execution. So the
                 scheme is allowlisted, and anything else degrades to plain text
                 (still visible, still copyable, just not clickable) rather than
                 being hidden, because a weird URL is exactly what the user should
                 see before granting trust. `noreferrer` also keeps the dashboard
                 URL, which carries a session, out of the referrer header. */
              safeHref(app.trustRepository)
                ? (
                  <a
                    href={safeHref(app.trustRepository) ?? undefined}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-mono text-[12px] text-accent underline break-all hover:text-accent-hover"
                  >
                    {app.trustRepository}
                  </a>
                )
                : (
                  <span className="font-mono text-[12px] text-text break-all">{app.trustRepository}</span>
                )
            )}
            <Badge variant="muted">{sourceLabel(app)}</Badge>
          </div>
          {/* The registry badge above reads as an endorsement. It is not one. */}
          <p className="text-muted leading-relaxed">
            {i18nT('components.appstore.trustAppModal.not_reviewed')}
          </p>
          {/* Consent needs an exit: what refusing does, and that this is reversible
              plus where. Without these the dialog is a one-way door. */}
          <p className="text-muted leading-relaxed">
            {i18nT('components.appstore.trustAppModal.on_cancel', { app: name })}
          </p>
          <p className="text-muted leading-relaxed">
            {i18nT('components.appstore.trustAppModal.revocable')}
          </p>
          {/* Two failure strings, because the two cases need different advice: if
              the grant landed and only the enable failed, the user has state to
              clean up; if nothing was written, telling them to go check Settings
              sends them after something that isn't there. askAgent on: a trust
              decision dialog holds no draft.

              When the failure carried a server reason, that raw string is the
              NOTICE'S MESSAGE and the copy above becomes its title: `message` is
              the error journal's lookup key, so a localized paraphrase in its
              place would cost the ask-the-agent hand-off the endpoint, status and
              backend code in every non-English locale. With no reason to show,
              the copy stays the message exactly as before.

              A PERMANENT refusal (the desktop code) is the one shape where the
              generic headline is wrong: "Choose Try again" cannot help, and the
              server's sentence is developer vocabulary. So the title says what
              is true in the words of the action the user pressed -- the app
              could not be INSTALLED: the build step this code names runs at
              install and nowhere else, so this dialog only ever shows it on the
              way from the detail page's Install button, and that page's own
              banner behind the dialog says "installed" too; one verb on the one
              path, so a reader is never left wondering whether installing and
              turning on are two steps -- and nothing was changed, the assurance
              `failed_generic` gives for the same `!granted` state -- the message
              is one plain sentence about what the app needs and where it can
              install, and the server's sentence stays beneath in the same notice
              (this dialog has no log panel to carry it). That sentence is still
              the journal key, so the report is looked up from it explicitly and
              handed to the notice -- the hand-off keeps the endpoint, the
              install log and this code. */}
          {permanentRefusal && (
            <ErrorNotice
              title={i18nT('components.appstore.trustAppModal.failed_desktop_unsupported', { app: name })}
              message={i18nT('components.appstore.trustAppModal.failed_desktop_unsupported_help')}
              report={findReport(detail)}
              footer={<span className="font-mono text-[12px]">{detail}</span>}
              askAgent
            />
          )}
          {failed && !permanentRefusal && (
            <ErrorNotice
              title={detail ? headline : undefined}
              message={detail || headline}
              messageClassName={detail ? 'font-mono text-[12px]' : undefined}
              askAgent
            />
          )}
        </div>
      )}
    </Modal>
  )
}
