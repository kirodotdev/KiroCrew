import { useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Copy, Check, Smartphone, X, ArrowRight } from 'lucide-react'
import { api } from '../api/client'
import { Btn } from './ui'
import { settingsPath } from './settingsPath'
import { useAppSelector } from '../store'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { copyToClipboard } from '../utils/clipboard'
import { parseErrorCode } from '../utils/errorReport'
import ErrorBoundary from './ErrorBoundary'
import { getMobileConnectRenderers } from './mobileConnectRenderers'

/** Machine-readable code from a mint error's JSON body (same shape as the
 *  Settings card's helper): lets the UI blame configuration only when the
 *  server actually said `external_origin_unavailable`. */
const linkErrorCode = (error: unknown): string | undefined =>
  typeof error === 'object' && error !== null && 'body' in error
    ? parseErrorCode(typeof error.body === 'string' ? error.body : undefined)
    : undefined

/**
 * "Connect your phone" — the sidebar entry's centered dialog (mockup A1).
 *
 * Renders one section per governed method the `/api/mobile-connect/methods`
 * endpoint returned (the CPP `mobile_connect` seam). The core draws two kinds
 * itself: `tailnet_qr` (server-rendered QR carrying a live session token,
 * minted ONLY on explicit click) and `login_link` (one-time sign-in URL). A
 * downstream edition's kinds are drawn by the renderer seam in
 * `mobileConnectRenderers.tsx`, and those sections come first: a deployment
 * that contributes a method offers it as the primary way in, so the built-in
 * link sits below as the fallback. A kind with neither a built-in section nor a
 * registered renderer renders nothing, so an unknown method is absent rather
 * than a broken panel.
 *
 * Credentials are minted on demand and never on mount: the QR/link responses
 * carry live tokens, so nothing here fetches one until the user asks.
 */
export default function MobileConnectModal({
  kinds,
  onClose,
}: {
  kinds: string[]
  onClose: () => void
}) {
  const { t } = useTranslation()
  const dialogRef = useRef<HTMLDivElement>(null)
  // The repo's modal keyboard contract: focus-in on mount, focus-restore on
  // close, Tab trapped inside the dialog, Escape dismisses.
  useDialogFocusTrap(dialogRef, onClose)

  const hasQr = kinds.includes('tailnet_qr')
  // Only renderers whose kind this deployment actually offers: the endpoint has
  // already filtered every id through `capabilities.mobile_connect`, so a
  // registered renderer for a denied or absent method draws nothing.
  const editionSections = getMobileConnectRenderers().filter(r => kinds.includes(r.kind))

  return (
    <div
      className="fixed inset-0 z-50 bg-bg/80 backdrop-blur-sm flex items-center justify-center animate-rise"
      role="presentation"
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={t('components.mobileConnect.use_kiro_crew_on_your_phone')}
        className="bg-card border border-border rounded-xl shadow-xl w-[440px] max-w-[90vw] max-h-[85vh] overflow-y-auto outline-none p-6 relative"
      >
        <button
          onClick={onClose}
          aria-label={t('components.mobileConnect.close')}
          className="absolute top-3 right-3 text-muted hover:text-text bg-transparent border-none cursor-pointer p-1"
        >
          <X size={16} />
        </button>
        <div className="flex items-center gap-2 text-[15px] font-semibold text-text-strong mb-1.5">
          <Smartphone size={16} className="shrink-0" />
          {t('components.mobileConnect.use_kiro_crew_on_your_phone')}
        </div>
        {hasQr && (
          <p className="text-[12.5px] text-muted leading-relaxed mb-4">
            {t('components.mobileConnect.scan_with_your_phone_camera_to_continue_with_the_s')}
          </p>
        )}
        {/* Edition sections, each in its own ErrorBoundary so a throwing
            renderer disables only itself. Deliberately WITHOUT `fallback={null}`,
            unlike the Overview stat-card slot: a card that vanishes leaves a grid
            of siblings, but a registered section here can be the dialog's ONLY
            content, and silently emptying it would strand a user who reached it
            from a nav row that promised a way in. The default boundary keeps the
            section occupied, which is also what makes counting these sections a
            sound input to `standalone` below. */}
        {editionSections.map(r => (
          <ErrorBoundary key={r.kind} scope={`mobile-connect:${r.kind}`}>
            <r.component onClose={onClose} />
          </ErrorBoundary>
        ))}
        {hasQr && <TailnetQrSection onClose={onClose} />}
        {kinds.includes('login_link') && (
          <LoginLinkSection standalone={!hasQr && editionSections.length === 0} />
        )}
      </div>
    </div>
  )
}

/** Copy button with a transient confirmation tick; failure is reported by the
 *  caller via `onResult` (a silent no-tick reads as success). */
function CopyBtn({ value, label, onResult }: { value: string; label: string; onResult?: (ok: boolean) => void }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    let ok = false
    try {
      ok = await copyToClipboard(value)
    } catch {
      ok = false
    }
    setCopied(ok)
    onResult?.(ok)
    if (ok) setTimeout(() => setCopied(false), 2000)
  }
  return (
    <Btn onClick={copy} className="whitespace-nowrap">
      {copied ? <Check size={14} /> : <Copy size={14} />} {label}
    </Btn>
  )
}

/** Tailnet QR: mint on explicit click when the machine state is `ready`;
 *  otherwise a one-line state + a path to the real setup card (Settings →
 *  Overview), never a rebuilt wizard. */
function TailnetQrSection({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  // The active slot's key rides the mint so the server's restricted-session
  // guard sees the REAL session, not the shared `dashboard:ui` default.
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const sessionKey = activeSlot ? `dashboard:${activeSlot}` : undefined
  // Single probe on open (the card's gentle-poll contract): the modal is
  // short-lived, and minting re-validates server-side anyway.
  const { data: status, isPending: probing, isError: probeFailed, refetch } = useQuery({
    queryKey: ['mobile-connect-tailnet-probe'],
    queryFn: () => api.tailnetMobile(),
    staleTime: 30_000,
    retry: false,
  })
  const mintQr = useMutation({ mutationFn: () => api.tailnetMobileQr(undefined, sessionKey) })
  const [qrCopyFailed, setQrCopyFailed] = useState(false)

  const ready = status?.step === 'ready'
  return (
    <div className="mb-4">
      {probing && (
        <p className="text-[11.5px] text-muted mb-2">{t('components.mobileConnect.checking_remote_access')}</p>
      )}
      {probeFailed && (
        <div className="flex items-center justify-between gap-2 mb-2">
          <p className="text-[11.5px] text-danger">{t('components.mobileConnect.could_not_check_remote_access')}</p>
          <Btn onClick={() => refetch()}>{t('components.mobileConnect.try_again')}</Btn>
        </div>
      )}
      {ready && !mintQr.data && (
        <div className="flex justify-center py-6">
          <Btn onClick={() => mintQr.mutate()} disabled={mintQr.isPending}>
            {mintQr.isPending
              ? t('components.mobileConnect.generating')
              : t('components.mobileConnect.show_qr_code')}
          </Btn>
        </div>
      )}
      {mintQr.data && (
        <div className="flex flex-col items-center gap-3 mb-2">
          <div className="bg-white p-2.5 rounded-lg leading-none">
            {/* Server-rendered PNG carrying a live session token — shown, never logged. */}
            <img src={mintQr.data.image} alt={t('components.mobileConnect.qr_code_for_mobile_access')} width={176} height={176} />
          </div>
          <div className="flex items-center gap-2 w-full">
            <input
              readOnly
              value={mintQr.data.url}
              onFocus={e => e.currentTarget.select()}
              aria-label={t('components.mobileConnect.mobile_access_link')}
              className="flex-1 min-w-0 bg-bg border border-border rounded-md text-muted text-[11.5px] px-2.5 py-2 font-mono overflow-hidden text-ellipsis"
            />
            <CopyBtn value={mintQr.data.url} label={t('components.mobileConnect.copy_link')} onResult={ok => setQrCopyFailed(!ok)} />
          </div>
          {qrCopyFailed && (
            <p className="text-[11.5px] text-danger w-full" role="alert">
              {t('components.mobileConnect.copy_failed_select_the_link_and_copy_it_manually')}
            </p>
          )}
        </div>
      )}
      {mintQr.isError && (
        <p className="text-[11.5px] text-danger mb-2" role="alert">
          {linkErrorCode(mintQr.error) === 'governance_denied'
            ? t('components.mobileConnect.phone_connection_is_disabled_by_policy_on_this_dep')
            : t('components.mobileConnect.could_not_generate_a_code_try_again')}
        </p>
      )}
      {status && !ready && (
        <button
          onClick={() => { onClose(); navigate(settingsPath({ tab: 'overview' })) }}
          className="w-full flex items-center justify-between gap-2 bg-bg-elevated border border-border rounded-lg px-3 py-2.5 mb-2 text-left cursor-pointer hover:bg-bg-hover transition-colors"
        >
          <span className="text-[12px] text-muted">
            {t('components.mobileConnect.remote_access_is_not_set_up_yet_finish_setup_to_ge')}
          </span>
          <ArrowRight size={14} className="shrink-0 text-muted" />
        </button>
      )}
      {ready && mintQr.data && (
        <div className="flex items-center justify-between gap-2">
          <p className="text-[11.5px] text-muted">
            <span className="text-accent">● </span>
            {t('components.mobileConnect.the_code_signs_you_in_automatically_and_expires_in', {
              // The SCAN window (link_window_secs), not the session TTL: the
              // QR stops being redeemable long before the session it opens
              // would expire — the exact confusion the server field exists for.
              minutes: Math.max(1, Math.round((mintQr.data.link_window_secs ?? 300) / 60)),
            })}
          </p>
          <Btn onClick={() => mintQr.mutate()} disabled={mintQr.isPending}>
            {t('components.mobileConnect.new_code')}
          </Btn>
        </div>
      )}
    </div>
  )
}

/** One-time login link for the configured external origin. `standalone` means
 *  the QR section is absent (link-only editions / governance): no divider, and
 *  an intro that does not presuppose a camera alternative. */
function LoginLinkSection({ standalone }: { standalone: boolean }) {
  const { t } = useTranslation()
  // The active slot's key rides the request so the server's restricted-session
  // guard sees the REAL session, not the shared `dashboard:ui` default.
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const sessionKey = activeSlot ? `dashboard:${activeSlot}` : undefined
  const createLink = useMutation({ mutationFn: () => api.mobileLoginLink(sessionKey) })
  const [linkCopyFailed, setLinkCopyFailed] = useState(false)

  return (
    <div className={standalone ? '' : 'border-t border-border pt-3'}>
      <div className="text-[12px] text-muted mb-2">
        {standalone
          ? t('components.mobileConnect.create_a_one_time_sign_in_link_to_send_to_yourself')
          : t('components.mobileConnect.or_create_a_one_time_sign_in_link_to_send_to_yours')}
      </div>
      {!createLink.data && (
        <Btn onClick={() => createLink.mutate()} disabled={createLink.isPending}>
          {createLink.isPending
            ? t('components.mobileConnect.creating')
            : t('components.mobileConnect.create_sign_in_link')}
        </Btn>
      )}
      {createLink.data && (
        <div className="flex items-center gap-2">
          <input
            readOnly
            value={createLink.data.url}
            onFocus={e => e.currentTarget.select()}
            aria-label={t('components.mobileConnect.one_time_sign_in_link')}
            className="flex-1 min-w-0 bg-bg border border-border rounded-md text-muted text-[11.5px] px-2.5 py-2 font-mono overflow-hidden text-ellipsis"
          />
          <CopyBtn value={createLink.data.url} label={t('components.mobileConnect.copy_link')} onResult={ok => setLinkCopyFailed(!ok)} />
        </div>
      )}
      {linkCopyFailed && (
        <p className="text-[11.5px] text-danger mt-2" role="alert">
          {t('components.mobileConnect.copy_failed_select_the_link_and_copy_it_manually')}
        </p>
      )}
      {createLink.isError && (
        <p className="text-[11.5px] text-danger mt-2" role="alert">
          {/* Blame configuration ONLY when the server said so; a policy denial
              is terminal (retrying cannot succeed); anything else gets a plain
              retry line so the user does not hunt a config that is fine. */}
          {linkErrorCode(createLink.error) === 'external_origin_unavailable'
            ? t('components.mobileConnect.could_not_create_a_link_check_that_an_external_add')
            : linkErrorCode(createLink.error) === 'governance_denied'
              ? t('components.mobileConnect.phone_connection_is_disabled_by_policy_on_this_dep')
              : t('components.mobileConnect.could_not_create_a_link_try_again')}
        </p>
      )}
      {createLink.data && (
        <p className="text-[11.5px] text-muted mt-2">
          {t('components.mobileConnect.the_link_works_once_and_expires_in_about_minutes', {
            minutes: Math.max(1, Math.round((createLink.data.expires_in ?? 900) / 60)),
          })}
        </p>
      )}
    </div>
  )
}
