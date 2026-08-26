import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  ChevronRight,
  Copy,
  Download,
  ExternalLink,
  Globe,
  LockKeyhole,
  LogIn,
  Play,
  QrCode,
  RefreshCw,
  Share2,
  ShieldCheck,
  ShieldOff,
  Smartphone,
} from 'lucide-react'
import {
  api,
  type TailnetMobileData,
  type TailnetMobileQr,
  type TailnetMobileStep,
} from '../api/client'
import { Badge, Btn, Card, CardTitle } from './ui'
import Clickable from './Clickable'
import { i18nT } from '../i18n/t'
import { copyToClipboard } from '../utils/clipboard'

/** Copy-to-clipboard button with a transient confirmation tick.
 *
 *  Routed through the shared `copyToClipboard` helper and AWAITED. The previous
 *  form called `navigator.clipboard?.writeText(value)` and then set the tick
 *  unconditionally: on a non-secure origin `clipboard` is `undefined`, so the
 *  optional chain short-circuits, nothing is written, and the user is shown a
 *  success tick over an empty clipboard. That lands hardest on the `ready`
 *  address — the one string the whole feature exists to hand to a phone.
 */
function CopyBtn({ value, label }: { value: string; label: string }) {
  const [done, setDone] = useState(false)
  return (
    <Btn
      aria-label={label}
      onClick={async () => {
        // `copyToClipboard` resolves a boolean (false = the legacy fallback
        // reported failure) and still rejects on a genuine exception — both
        // must suppress the tick. It carries a textarea fallback for the
        // non-secure-origin case, which is why it is used instead of touching
        // `navigator.clipboard` here.
        try {
          if ((await copyToClipboard(value)) === false) return
        } catch {
          return
        }
        setDone(true)
        window.setTimeout(() => setDone(false), 1500)
      }}
    >
      {done ? <Check className="lucide-inline" /> : <Copy className="lucide-inline" />}
      {label}
    </Btn>
  )
}

/** The per-step icon. Kept beside the step union so a new step cannot be added
 *  without choosing one.
 *
 *  None of these may be `Smartphone`: the card title already carries it, and a
 *  step that repeats it renders two identical icons stacked, which reads as a
 *  rendering fault rather than as emphasis. */
const STEP_ICON: Record<TailnetMobileStep, typeof Smartphone> = {
  pinned: ShieldOff,
  install: Download,
  start_daemon: Play,
  sign_in: LogIn,
  enable_magicdns: Globe,
  enable_https: LockKeyhole,
  trust_off: ShieldCheck,
  restart_gateway: RefreshCw,
  occupied: AlertTriangle,
  publish: Share2,
  ready: QrCode,
}

/** Full catalog keys per step, as literals so the i18n key checker can resolve
 *  them statically (a computed `components.tailnetMobile.${step}.title` cannot
 *  be verified and the strict gate rejects it). */
const STEP_TITLE_KEY: Record<TailnetMobileStep, string> = {
  pinned: 'components.tailnetMobile.pinned_title',
  install: 'components.tailnetMobile.install_title',
  start_daemon: 'components.tailnetMobile.start_daemon_title',
  sign_in: 'components.tailnetMobile.sign_in_title',
  enable_magicdns: 'components.tailnetMobile.enable_magicdns_title',
  enable_https: 'components.tailnetMobile.enable_https_title',
  trust_off: 'components.tailnetMobile.trust_off_title',
  restart_gateway: 'components.tailnetMobile.restart_gateway_title',
  occupied: 'components.tailnetMobile.occupied_title',
  publish: 'components.tailnetMobile.publish_title',
  ready: 'components.tailnetMobile.ready_title',
}

const STEP_BODY_KEY: Record<TailnetMobileStep, string> = {
  pinned: 'components.tailnetMobile.pinned_body',
  install: 'components.tailnetMobile.install_body',
  start_daemon: 'components.tailnetMobile.start_daemon_body',
  sign_in: 'components.tailnetMobile.sign_in_body',
  enable_magicdns: 'components.tailnetMobile.enable_magicdns_body',
  enable_https: 'components.tailnetMobile.enable_https_body',
  trust_off: 'components.tailnetMobile.trust_off_body',
  restart_gateway: 'components.tailnetMobile.restart_gateway_body',
  occupied: 'components.tailnetMobile.occupied_body',
  publish: 'components.tailnetMobile.publish_body',
  ready: 'components.tailnetMobile.ready_body',
}

/** Which steps are a normal part of setting mobile access up (neutral framing)
 *  versus a state that needs attention. Drives the badge only. */
const ATTENTION_STEPS = new Set<TailnetMobileStep>(['occupied', 'pinned'])

/** Steps whose `detail` is ABOUT that step, so surfacing the daemon's verbatim
 *  text helps rather than confuses.
 *
 *  Deliberately not every step. `detail` carries whichever of the serve state or
 *  the daemon probe spoke last, so on `trust_off` / `restart_gateway` — whose
 *  remedy is a config switch or a restart — it renders a line about port 443
 *  occupancy that has nothing to do with the action being asked for. A guidance
 *  card that pairs the right instruction with an unrelated technical sentence
 *  reads as though the two are connected, which is worse than omitting it. */
const DETAIL_STEPS = new Set<TailnetMobileStep>([
  'pinned',
  'start_daemon',
  'sign_in',
  'enable_magicdns',
  'occupied',
])

/** Whether the operator has opened the install invitation before.
 *
 *  A UI-local preference, so it lives in localStorage next to the other `mc-*`
 *  view state rather than in `config.json`: it describes one browser's idea of
 *  how much of a card to show, not anything the gateway acts on, and putting it
 *  in the shared config would sync a display choice across every device that
 *  reaches this dashboard. */
const INVITE_EXPANDED_LS_KEY = 'mc-tailnet-mobile-invite-expanded'

/** Steps the dashboard can finish without sending the operator elsewhere.
 *
 * Tailscale installation, daemon startup, sign-in, MagicDNS, and tailnet-wide
 * HTTPS consent remain explicit prerequisites because the gateway cannot safely
 * complete them on a user's behalf. Once those are satisfied, these three steps
 * are one operation from the operator's point of view: trust the daemon-derived
 * name, restart through the formal single-flight path, publish through Tailscale
 * Serve, and mint the QR after the replacement gateway proves readiness.
 */
const ONE_CLICK_SETUP_STEPS = new Set<TailnetMobileStep>([
  'trust_off',
  'restart_gateway',
  'publish',
])

const RESTART_POLL_MS = 1000
const RESTART_WAIT_MS = 60_000

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

/** Wait for the replacement gateway rather than mistaking the old process's
 * final 200 for recovery. The old process can answer `restart_gateway` for the
 * 250 ms response-flush window; only a different step proves startup ran again.
 * Network errors are expected while the listener is between process images.
 */
async function waitForRestartedGateway(
  onStatus: (next: TailnetMobileData) => void,
): Promise<TailnetMobileData> {
  const deadline = Date.now() + RESTART_WAIT_MS

  while (Date.now() < deadline) {
    try {
      const next = await api.tailnetMobile()
      onStatus(next)
      if (next.step !== 'trust_off' && next.step !== 'restart_gateway') return next
    } catch {
      // Expected while the old listener exits and the replacement binds.
    }
    await pause(RESTART_POLL_MS)
  }

  throw new Error(i18nT('components.tailnetMobile.setup_timeout'))
}

/** Reads are guarded because `localStorage` THROWS, it does not merely return
 *  null, when storage is unavailable (Safari private browsing, a blocked
 *  third-party context). An unreadable preference must degrade to the collapsed
 *  default, never take the card down with it. */
function readInviteExpanded(): boolean {
  try {
    return window.localStorage.getItem(INVITE_EXPANDED_LS_KEY) === '1'
  } catch {
    return false
  }
}

function writeInviteExpanded(expanded: boolean): void {
  try {
    window.localStorage.setItem(INVITE_EXPANDED_LS_KEY, expanded ? '1' : '0')
  } catch {
    // A preference that cannot be stored is not worth failing a click over.
  }
}

/**
 * Overview card that walks the operator from "nothing" to "the dashboard is on
 * my phone", one step at a time.
 *
 * The card renders whatever step the backend derived and never re-derives it —
 * `_derive_step` is the single owner, so the two layers cannot disagree about
 * what a host with a name but no published serve means.
 *
 * Two behaviours are deliberate and worth keeping:
 *
 * **The QR is never minted on render.** Its payload is a live session token, so
 * it is fetched only when the operator asks for it, and it is dropped from state
 * when they dismiss it. Rendering the card must never put a credential on screen
 * (or in the query cache) by itself.
 *
 * **`published: null` is not `false`.** An undeterminable serve state arrives as
 * the `occupied` step, so the card renders the manual command (see the
 * `occupied` branch below, which prints `kirocrew tailnet up`) instead of a
 * publish button that would overwrite a mount it could not identify.
 *
 * **Setup is one explicit mutation, even across a gateway restart.** The click
 * itself is the operator's consent to every in-scope step, so the mutation may
 * resume when the listener comes back and mint the QR. It never runs on render,
 * and it never crosses the `occupied` refusal into overwriting another Serve
 * mount.
 */
export function TailnetMobileCard() {
  const qc = useQueryClient()
  const [qr, setQr] = useState<TailnetMobileQr | null>(null)
  const [actionError, setActionError] = useState('')
  const [inviteExpanded, setInviteExpanded] = useState(readInviteExpanded)

  /** Single writer for the invite's open/closed state, so the persisted value
   *  can never drift from what is rendered. */
  const setInvite = (expanded: boolean) => {
    setInviteExpanded(expanded)
    writeInviteExpanded(expanded)
  }

  const { data, isLoading } = useQuery<TailnetMobileData>({
    queryKey: ['tailnet-mobile'],
    queryFn: () => api.tailnetMobile(),
    // Gentle: each poll costs two `tailscale` subprocess round trips
    // server-side. 30s is fast enough to notice a daemon coming up while the
    // operator is following the steps.
    refetchInterval: 30_000,
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['tailnet-mobile'] })
    void qc.invalidateQueries({ queryKey: ['tailnet-status'] })
  }

  const unpublish = useMutation({
    mutationFn: () => api.tailnetMobileUnpublish(),
    onSuccess: (res) => {
      setActionError(res.ok ? '' : res.detail)
      setQr(null)
      invalidate()
    },
    onError: (err: Error) => setActionError(err.message),
  })

  const mintQr = useMutation({
    mutationFn: () => api.tailnetMobileQr(),
    onSuccess: (res) => {
      setActionError('')
      setQr(res)
    },
    onError: (err: Error) => setActionError(err.message),
  })

  /** Finish every gateway-owned step behind one click.
   *
   * Each transition is read back from the server rather than re-derived here.
   * That preserves `_derive_step` as the single owner of readiness and makes a
   * future gateway step fail closed: anything except `publish` or `ready` stops
   * before either exposure or credential minting.
   */
  const setup = useMutation({
    mutationFn: async (): Promise<TailnetMobileQr> => {
      if (!data) throw new Error(i18nT('components.tailnetMobile.setup_timeout'))
      let current = data
      const accept = (next: TailnetMobileData) => {
        current = next
        qc.setQueryData(['tailnet-mobile'], next)
      }

      if (current.step === 'trust_off') {
        await api.patchConfig('dashboard.tailscale.enabled', true)
        accept(await api.tailnetMobile())
      }

      if (current.step === 'restart_gateway') {
        await api.restartGateway()
        current = await waitForRestartedGateway(accept)
      }

      if (current.step === 'publish') {
        const result = await api.tailnetMobilePublish()
        if (!result.ok) throw new Error(result.detail)
        accept(await api.tailnetMobile())
      }

      if (current.step !== 'ready') {
        throw new Error(
          current.detail || i18nT('components.tailnetMobile.setup_timeout'),
        )
      }

      return api.tailnetMobileQr()
    },
    onMutate: () => setActionError(''),
    onSuccess: (res) => {
      setQr(res)
      invalidate()
    },
    onError: (err: Error) => setActionError(err.message),
  })

  if (isLoading || !data) return null

  // Owner-only as a whole surface, enforced SERVER-side: GET /api/tailnet/mobile
  // itself refuses a non-owner, so the query above yields no data and the
  // `!data` guard already returns null. Deliberately not re-checked here — a
  // renderer-side flag would be a second, weaker copy of the same rule, and
  // it would still have shipped the hostname and peer counts over the wire.

  const step = data.step

  // `step` is TYPED as TailnetMobileStep, which is a claim about the contract and
  // not a fact about the bytes: the value arrives over HTTP, so an older gateway,
  // a partial response, or a test fixture can hand us a step this build has never
  // heard of. Indexing the literal maps below with one would yield `undefined`,
  // and rendering `undefined` as a component throws "Element type is invalid" —
  // which React escalates to the nearest error boundary, so an unrecognised step
  // in THIS optional card would blank out the whole Settings Overview page.
  // Guidance is worth having and not worth a page for, so an unknown step renders
  // nothing. Checked against STEP_ICON because every render path needs an icon.
  if (!step || !(step in STEP_ICON)) return null

  // The `install` step is a pure INVITATION: this machine has no Tailscale, so the
  // operator has expressed no interest in phone access at all. Every other step
  // follows either demonstrated intent (Tailscale is present, so they are mid-flow)
  // or a live thing to manage — those earn a card. This one does not, and a
  // permanent full-height panel advertising a product the user may never want is
  // exactly the Overview clutter that makes people stop reading the page.
  //
  // Collapsed state IS persisted, in localStorage under
  // `mc-tailnet-mobile-invite-expanded`: an operator who has decided they do not
  // want this should not have to re-decide on every page load. It stores
  // expanded-ness only, not a permanent dismissal — the row returns collapsed on a
  // fresh load, which is the cheap approximation and is called out in the PR.
  if (step === 'install' && !inviteExpanded) {
    return (
      <Clickable
        onClick={() => {
          setInvite(true)
          // Re-probe on expand. This is what lets `install` drop the Re-check
          // button (max-two-buttons-per-row) without making an operator who just
          // installed Tailscale wait out the 30s poll: they come back, expand,
          // and the card has already re-read the daemon.
          invalidate()
        }}
        aria-label={i18nT('components.tailnetMobile.install_teaser')}
        className="flex items-center gap-2 rounded-lg border border-border bg-card px-3.5 py-2.5 text-muted hover:text-text"
      >
        <Smartphone className="lucide-inline shrink-0" />
        <span className="text-text">{i18nT('components.tailnetMobile.install_teaser')}</span>
        <span>{i18nT('components.tailnetMobile.install_teaser_hint')}</span>
        <ChevronRight className="lucide-inline ml-auto shrink-0" />
      </Clickable>
    )
  }

  const Icon = STEP_ICON[step]
  const busy =
    unpublish.isPending ||
    mintQr.isPending ||
    setup.isPending

  return (
    <Card>
      <CardTitle>
        <Smartphone className="lucide-inline" />
        {i18nT('components.tailnetMobile.card_title')}
        {step === 'ready' ? (
          <Badge variant="ok">{i18nT('components.tailnetMobile.badge_on')}</Badge>
        ) : ATTENTION_STEPS.has(step) ? (
          <Badge variant="warn">{i18nT('components.tailnetMobile.badge_attention')}</Badge>
        ) : (
          <Badge variant="muted">{i18nT('components.tailnetMobile.badge_setup')}</Badge>
        )}
      </CardTitle>

      <div className="flex items-start gap-3">
        <Icon className="lucide-inline mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="font-medium">{i18nT(STEP_TITLE_KEY[step])}</div>
          <p className="mt-1 text-muted">{i18nT(STEP_BODY_KEY[step])}</p>

          {/* The daemon's own words, never rephrased — the only part that stays
              correct if Tailscale rewords its errors. Shown only where it is
              about the step being asked for (see DETAIL_STEPS). */}
          {data.detail && DETAIL_STEPS.has(step) ? (
            <p className="mt-2 text-muted">
              <code className="select-all break-all min-w-0">{data.detail}</code>
            </p>
          ) : null}

          {/* The command `occupied_body` tells the user to run. Without this the
              copy ("Publish it yourself if you are sure it is safe to
              overwrite") is an instruction with no means to follow it — and
              `occupied` is the ONE state whose remedy is deliberately manual,
              because publishing here would replace whatever Tailscale is
              already serving. No copy button: the row already carries two, and
              `max-two-buttons-per-row` counts a link styled as one.

              No `break-all` on this one, unlike the origin and detail spans: it
              is a FIXED 19-character command, so it cannot overflow even at
              320px, and breaking a command mid-token would only make it harder
              to read. */}
          {step === 'occupied' ? (
            <p className="mt-2 text-muted">
              <code className="select-all">kirocrew tailnet up</code>
            </p>
          ) : null}

          {step === 'ready' && data.origin ? (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <code className="select-all break-all min-w-0">{data.origin}</code>
              <CopyBtn value={data.origin} label={i18nT('components.tailnetMobile.copy')} />
            </div>
          ) : null}

          {step === 'ready' && data.keep_awake ? (
            <p className="mt-2 text-muted">
              {i18nT('components.tailnetMobile.keep_awake_note')}
            </p>
          ) : null}

          {/* The other half of the setup, and the one this machine cannot do for
              the operator. Shown on the two steps where they are about to rely on
              a phone reaching this address: publishing succeeds and the QR renders
              on a tailnet of one, so without this the failure surfaces only as an
              unexplained "cannot connect" in the phone's browser. */}
          {(step === 'ready' || step === 'publish') && data.peer_count === 0 ? (
            <p className="mt-2 text-warn">
              {i18nT('components.tailnetMobile.no_peers')}
            </p>
          ) : (step === 'ready' || step === 'publish') && data.peers_online === 0 ? (
            <p className="mt-2 text-muted">
              {i18nT('components.tailnetMobile.peers_offline')}
            </p>
          ) : null}

          {/* ── Per-step actions ─────────────────────────────────────────── */}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {step === 'install' ? (
              <>
                <a href={data.download_url} target="_blank" rel="noopener noreferrer">
                  <Btn primary>
                    <Download className="lucide-inline" />
                    {i18nT('components.tailnetMobile.download')}
                  </Btn>
                </a>
                {/* Expanding must not be a one-way door — an operator who opened
                    this out of curiosity gets the row back, not a panel they are
                    stuck with for the rest of the session. */}
                <Btn onClick={() => setInvite(false)}>
                  {i18nT('components.tailnetMobile.hide_qr')}
                </Btn>
              </>
            ) : null}

            {ONE_CLICK_SETUP_STEPS.has(step) ? (
              <Btn primary disabled={busy} onClick={() => setup.mutate()}>
                <Smartphone className="lucide-inline" />
                {setup.isPending
                  ? i18nT('components.tailnetMobile.setting_up')
                  : i18nT('components.tailnetMobile.setup_action')}
              </Btn>
            ) : null}

            {step === 'ready' ? (
              <>
                <Btn primary disabled={busy} onClick={() => mintQr.mutate()}>
                  <QrCode className="lucide-inline" />
                  {i18nT('components.tailnetMobile.show_qr')}
                </Btn>
                <Btn disabled={busy} onClick={() => unpublish.mutate()}>
                  {i18nT('components.tailnetMobile.stop')}
                </Btn>
              </>
            ) : null}

            {/* Every non-terminal step gets a refresh: the operator is going off
                to install or sign in, and coming back to a card that only
                updates on a 30s timer feels broken.

                `install` is excluded, and not for cosmetic reasons: with Download
                and Hide already in this group a third sibling breaks
                `max-two-buttons-per-row` (website/AUTOSDE.yaml, blocking), which
                counts a link-styled-as-button and explicitly rejects wrapping as
                a fix. Expanding the invite refetches instead, so returning from
                the Tailscale install still re-probes immediately rather than
                waiting out the poll. */}
            {step !== 'pinned' && step !== 'ready' && step !== 'install' ? (
              <Btn disabled={busy} onClick={invalidate}>
                <RefreshCw className="lucide-inline" />
                {i18nT('components.tailnetMobile.recheck')}
              </Btn>
            ) : null}
          </div>

          {actionError ? (
            <p className="mt-2 text-danger">{actionError}</p>
          ) : null}

          {ONE_CLICK_SETUP_STEPS.has(step) ? (
            <p className="mt-2 text-muted">
              {i18nT('components.tailnetMobile.automatic_https')}
            </p>
          ) : null}

          {/* ── The QR itself ───────────────────────────────────────────── */}
          {qr ? (
            <div className="mt-4 border-t border-border pt-3">
              <img
                src={qr.image}
                width={180}
                height={180}
                alt={i18nT('components.tailnetMobile.qr_alt')}
                className="rounded-md bg-white p-2"
              />
              <p className="mt-2 text-warn">
                {i18nT('components.tailnetMobile.qr_warning', {
                  minutes: Math.max(1, Math.round(qr.link_window_secs / 60)),
                  hours: Math.max(1, Math.round(qr.ttl_secs / 3600)),
                })}
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <CopyBtn
                  value={qr.url}
                  label={i18nT('components.tailnetMobile.copy_link')}
                />
                <Btn onClick={() => setQr(null)}>
                  {i18nT('components.tailnetMobile.hide_qr')}
                </Btn>
              </div>
            </div>
          ) : null}

          {step === 'enable_magicdns' || step === 'enable_https' ? (
            <a
              className="mt-2 inline-flex items-center gap-1 text-accent"
              href="https://login.tailscale.com/admin/dns"
              target="_blank"
              rel="noopener noreferrer"
            >
              <ExternalLink className="lucide-inline" />
              {i18nT(
                step === 'enable_https'
                  ? 'components.tailnetMobile.open_https_admin'
                  : 'components.tailnetMobile.open_dns_admin',
              )}
            </a>
          ) : null}
        </div>
      </div>
    </Card>
  )
}
