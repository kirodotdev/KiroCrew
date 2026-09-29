/**
 * "Deploy a mate to the cloud" — pick the mate, pick the lane, confirm once.
 *
 * A mate is ONE agent, which is what makes this dialog different from the crews tab's
 * launcher: that one installs a gateway serving a whole roster, and this one puts a
 * single member in the cloud to chat to. Two steps, in one component, because they are
 * one decision. The picker answers *which mate and where*; the confirmation answers
 * *are you sure*, and it is the only place a launch is started. Nothing before Launch
 * creates anything, and both steps say so.
 *
 * ## Why the mate comes first
 *
 * The lane a mate can run in depends on the mate: a Fargate lane runs ONE
 * digest-pinned image carrying ONE mate's bundle, and its descriptor now publishes
 * which mate that is (`serves_mate`). Asking for the mate first means the lane chips
 * can say, while the user is choosing, that this lane serves a different mate — which
 * is the same information the server would otherwise deliver as a refused launch.
 *
 * ## What the confirmation deliberately does NOT show
 *
 * No image digest, no secret ARN, no warning band. The confirmation is one sentence
 * and one checkbox. The server-side gate is untouched: a lane that demands a
 * credential-recipient confirmation still gets one, sent as `confirm_recipient`
 * verbatim from the lane's own `confirm_before_launch` string. The engine compares
 * that to what it is ABOUT to launch and refuses a mismatch, so the protection is
 * the comparison, not the paragraph — and a paragraph of ARNs in front of every
 * launch is a ritual the reader stops reading, which is worse than no paragraph at
 * all for the one launch where the value is wrong.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink, Search } from 'lucide-react'
import { api, type RemoteProvisioner } from '../../api/client'
import {
  BUILTIN_PROVISIONER_ID,
  FARGATE_PROVISIONER_ID,
  PRICING_CALCULATOR_URL,
} from '../../utils/remoteCrew'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../../components/ui/dialog'
import { Btn } from '../../components/ui'
import CrewAvatar from '../../components/CrewAvatar'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { canRenderRemoteProvisionerKind } from '../../components/remoteProvisionerRenderers'
import type { RemoteLane } from './remoteLane'
import { laneLabel } from './remoteLane'

/** How many mate rows the list shows before it scrolls. */
const VISIBLE_ROWS = 6

/** What a launch this dialog starts carries back to the panel. */
export interface DeployMateRequest {
  mateName: string
  provisioner: RemoteProvisioner
  /** The lane's own `confirm_before_launch`, to be sent verbatim; '' when it has none. */
  confirmRecipient: string
}

/** One lane chip: a lane this deployment could offer, and whether it can be picked. */
interface LaneChoice {
  lane: RemoteLane
  /** The server row, or null for a lane this build ships no provisioner for. */
  provisioner: RemoteProvisioner | null
  /** One short line under the label: what the lane is, or why it cannot be picked. */
  hint: string
  disabled: boolean
  /** A longer sentence shown under the chips when this lane is the reason. */
  blockedReason: string
}

/**
 * The lane of a provisioner row, or null when this build has no lane word for it.
 *
 * Keyed on the descriptor's `kind`, which is the field that names a FORM, so a
 * deployment offering two Fargate rows gets two chips that both read Fargate — which
 * is correct: they differ in their configuration, not in what the user is choosing.
 */
function laneOfProvisioner(p: RemoteProvisioner): RemoteLane | null {
  if (p.kind === FARGATE_PROVISIONER_ID) return 'fargate'
  return null
}

/**
 * The chips, in a fixed order, from what the gateway offers.
 *
 * A lane silently absent is indistinguishable from a lane that does not exist, so a
 * mate lane the gateway knows about but has not configured gets a disabled chip with
 * the reason, rather than empty space. Today that is the Fargate lane before a cluster
 * is set up.
 */
export function laneChoices(rows: readonly RemoteProvisioner[], mateName: string): LaneChoice[] {
  // Drawable AND a mate lane. The built-in EC2 row is drawable and deliberately absent:
  // it creates a crew, which is the other tab's question.
  const drawable = rows.filter(
    p => canRenderRemoteProvisionerKind(p.kind) && p.kind !== BUILTIN_PROVISIONER_ID,
  )
  const pick = (lane: RemoteLane) => drawable.find(p => laneOfProvisioner(p) === lane) ?? null

  const fargate = pick('fargate')
  // A Fargate lane pinned to another mate: offered, refused, and it says which mate it
  // serves. Disabled rather than hidden -- the lane IS configured, and a user who
  // deployed that mate here yesterday needs to know why today's pick cannot go.
  const fargateWrongMate =
    fargate !== null && !!fargate.serves_mate && !!mateName && fargate.serves_mate !== mateName

  // MATE lanes only. EC2 is a CREW lane: it installs a gateway that serves a whole
  // roster, so "which mate?" is not a question it can answer, and offering it here would
  // let the picker post a mate name to a lane that must refuse it. The crews tab has its
  // own launcher for that lane.
  const out: LaneChoice[] = [
    {
      lane: 'fargate',
      provisioner: fargate,
      hint: !fargate
        ? i18nT('pages.settings.remoteCrewPanel.lane_not_set_up')
        : fargate.serves_mate
          ? i18nT('pages.settings.remoteCrewPanel.lane_serves_other', { mate: fargate.serves_mate })
          : i18nT('pages.settings.remoteCrewPanel.lane_fargate_hint'),
      disabled: fargate === null || fargateWrongMate,
      blockedReason: !fargate
        ? i18nT('pages.settings.remoteCrewPanel.lane_fargate_absent')
        : fargateWrongMate
          ? i18nT('pages.settings.remoteCrewPanel.lane_fargate_other_mate', {
              mate: fargate.serves_mate,
              picked: mateName,
            })
          : '',
    },
  ]
  return out
}

export default function DeployMateDialog({
  open,
  onClose,
  onLaunch,
  launching,
  region,
}: {
  open: boolean
  onClose: () => void
  /** Start the launch. The dialog closes itself first; the panel owns the mutation. */
  onLaunch: (req: DeployMateRequest) => void
  launching: boolean
  /**
   * The AWS region this launch will name, which is the panel's own — the same value
   * the EC2 form launches into and the same one the request body carries. It is in
   * the confirmation sentence because "launch it on Fargate" and "launch it on
   * Fargate in us-west-2" are different statements, and only the second one is
   * something a reader can recognise as wrong.
   */
  region: string
}) {
  const [step, setStep] = useState<'pick' | 'confirm'>('pick')
  const [query, setQuery] = useState('')
  const [mateName, setMateName] = useState('')
  const [laneIdx, setLaneIdx] = useState(0)
  const [confirmed, setConfirmed] = useState(false)

  // Reset on every open. A dialog that reopened on the previous pick would put a
  // confirmed checkbox in front of a launch the user has not looked at yet.
  useEffect(() => {
    if (!open) return
    setStep('pick')
    setQuery('')
    setMateName('')
    setLaneIdx(0)
    setConfirmed(false)
  }, [open])

  // Both lists are fetched only while the dialog is open: a settings visit that never
  // opens the picker makes neither call.
  const membersQuery = useQuery({
    queryKey: ['members'],
    queryFn: () => api.members(),
    enabled: open,
  })
  const provisionersQuery = useQuery({
    queryKey: ['cloud', 'provisioners'],
    queryFn: () => api.cloudProvisioners(),
    enabled: open,
  })

  const members = useMemo(() => membersQuery.data?.members ?? [], [membersQuery.data])
  const matches = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return members
    return members.filter(
      m =>
        m.name.toLowerCase().includes(q) ||
        String(m.display_name ?? '').toLowerCase().includes(q),
    )
  }, [members, query])

  const lanes = useMemo(
    () => laneChoices(provisionersQuery.data?.provisioners ?? [], mateName),
    [provisionersQuery.data, mateName],
  )
  const lane = lanes[laneIdx] ?? null
  const canContinue = !!mateName && !!lane && !lane.disabled && !!lane.provisioner

  const startConfirm = () => {
    if (!canContinue) return
    // UNCHECKED. A box that arrives ticked is one the reader has to undo to say no,
    // which makes Launch gated on it decoration: ticking it is the act of consent, so
    // it starts in the state that has not consented.
    setConfirmed(false)
    setStep('confirm')
  }

  const launch = () => {
    if (!lane?.provisioner || !confirmed) return
    onLaunch({
      mateName,
      provisioner: lane.provisioner,
      confirmRecipient: lane.provisioner.confirm_before_launch ?? '',
    })
    onClose()
  }

  // One sentence, built once and used in three places (title, body, checkbox) so the
  // three can never describe different launches.
  const laneName = lane ? laneLabel(lane.lane) : ''
  const sentence = i18nT('pages.settings.remoteCrewPanel.confirm_sentence', {
    mate: mateName,
    lane: laneName,
    region,
  })

  if (step === 'confirm') {
    return (
      <Dialog open={open} onOpenChange={v => { if (!v) onClose() }}>
        <DialogContent maxWidth={460} data-testid="deploy-mate-confirm">
          <DialogHeader>
            <DialogTitle>
              {i18nT('pages.settings.remoteCrewPanel.confirm_title', {
                mate: mateName,
                lane: laneName,
              })}
            </DialogTitle>
          </DialogHeader>
          <DialogBody>
            {/* The SENTENCE is the checkbox's label and appears exactly once. It used to
                be printed above the box as well, in a paragraph that then repeated it
                word for word -- which left the box with nothing of its own to say, and a
                reader no reason to think unticking it did anything. What sits above it
                now is the one thing the sentence does not carry: that nothing exists
                yet. */}
            {/* What it COSTS, before the button that starts the spending. The sibling
                EC2 launcher has carried this line all along and this flow had none, so
                the only cost information in front of a launch was the absence of any --
                and a reader who cannot tell what a click costs does not click. Its own
                sentence, not the EC2 one: a task has no instance, no disk and no setup
                bucket, and is charged for what it holds while it runs. */}
            <div className="flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2.5 mb-3">
              <AlertTriangle size={15} className="mt-0.5 shrink-0 text-warn" />
              <div className="text-[12px] text-text">
                {i18nT('pages.settings.remoteCrewPanel.billing_fargate')}{' '}
                <a
                  className="text-accent font-medium hover:underline inline-flex items-center gap-1"
                  href={PRICING_CALCULATOR_URL}
                  target="_blank"
                  rel="noreferrer"
                >
                  {i18nT('pages.settings.remoteCrewPanel.pricing_calculator')}
                  <ExternalLink size={12} />
                </a>
              </div>
            </div>
            <p className="text-[13px] text-muted m-0">
              {i18nT('pages.settings.remoteCrewPanel.confirm_body')}
            </p>
            <label className="mt-3.5 flex items-start gap-2 text-[12.5px] text-text cursor-pointer">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={confirmed}
                aria-label={sentence}
                onChange={e => setConfirmed(e.target.checked)}
              />
              <span>{sentence}</span>
            </label>
          </DialogBody>
          <DialogFooter>
            {/* Back, not Cancel: it returns to the picker. Labelled Cancel it
                promised to leave a flow it puts the reader back into. */}
            <Btn onClick={() => setStep('pick')} disabled={launching}>
              {i18nT('pages.settings.remoteCrewPanel.back')}
            </Btn>
            <Btn primary onClick={launch} disabled={!confirmed || launching} data-testid="deploy-mate-launch">
              {launching
                ? i18nT('pages.settings.remoteCrewPanel.launching')
                : i18nT('pages.settings.remoteCrewPanel.launch')}
            </Btn>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  return (
    <Dialog open={open} onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent maxWidth={520} data-testid="deploy-mate-picker">
        <DialogHeader>
          <DialogTitle>{i18nT('pages.settings.remoteCrewPanel.deploy_mate_title')}</DialogTitle>
          <DialogDescription>
            {i18nT('pages.settings.remoteCrewPanel.deploy_subtitle')}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <label htmlFor="deploy-mate-q" className="block text-[12px] text-text mb-1.5">
            {i18nT('pages.settings.remoteCrewPanel.which_mate')}
          </label>
          <div className="relative">
            <Search
              size={14}
              className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted pointer-events-none"
              aria-hidden
            />
            <input
              id="deploy-mate-q"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder={i18nT('pages.settings.remoteCrewPanel.search_mates')}
              aria-label={i18nT('pages.settings.remoteCrewPanel.search_mates')}
              className="w-full bg-bg-elevated border border-border rounded-md pl-7 pr-3 py-1.5 text-[13px] text-text outline-hidden focus-ring"
            />
          </div>
          {/* The count is the answer to "is the mate I want in here at all", which a
              filtered list of six rows cannot give on its own. */}
          <div className="mt-1.5 mb-1 font-mono text-[10px] uppercase tracking-[.11em] text-muted">
            {i18nT('pages.settings.remoteCrewPanel.mates_shown', {
              shown: matches.length,
              total: members.length,
            })}
          </div>
          {membersQuery.isError ? (
            // askAgent ON: a failed roster read is a gateway-side failure with nothing
            // to retype here, which is exactly the case the rule names -- and this
            // dialog holds no unsaved draft for the hand-off to destroy.
            <ErrorNotice
              variant="inline"
              message={i18nT('pages.settings.remoteCrewPanel.mates_unavailable')}
              testId="deploy-mate-members-error"
              askAgent
            />
          ) : (
            <div
              className="max-h-[186px] overflow-auto border border-border rounded-md bg-bg p-1"
              role="listbox"
              aria-label={i18nT('pages.settings.remoteCrewPanel.which_mate')}
              style={{ maxHeight: VISIBLE_ROWS * 31 }}
            >
              {matches.length === 0 ? (
                <div className="text-[12px] text-muted px-2 py-1.5">
                  {i18nT('pages.settings.remoteCrewPanel.no_mate_matches')}
                </div>
              ) : (
                matches.map(m => {
                  const on = m.name === mateName
                  return (
                    <button
                      key={m.slug || m.name}
                      type="button"
                      role="option"
                      aria-selected={on}
                      onClick={() => setMateName(m.name)}
                      className={`w-full flex items-center gap-2.5 px-1.5 py-1 rounded-md text-left ${on ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`}
                    >
                      <CrewAvatar seed={m.name} avatar={m.avatar} size={20} className="rounded" />
                      <span
                        className={`flex-1 min-w-0 truncate text-[13px] ${on ? 'text-text-strong font-medium' : 'text-text'}`}
                      >
                        {m.display_name || m.name}
                      </span>
                    </button>
                  )
                })
              )}
            </div>
          )}

          <div className="mt-4 mb-1.5 text-[12px] text-text">
            {i18nT('pages.settings.remoteCrewPanel.where_should_it_run')}
          </div>
          {provisionersQuery.isError ? (
            <ErrorNotice
              variant="inline"
              message={i18nT('pages.settings.remoteCrewPanel.provisioners_unavailable')}
              testId="deploy-mate-lanes-error"
              askAgent
            />
          ) : (
            <>
              <div className="flex gap-2">
                {/* An unusable lane is SELECTABLE but not launchable, and that is the
                    whole point of it being here. A truly `disabled` button cannot be
                    clicked, so its reason — the sentence below, which is the only place
                    "this lane serves a different mate" is written — was unreachable: the
                    user could see a greyed chip and never find out why. Selecting it
                    shows the reason; `canContinue` still refuses, so Continue stays off
                    and nothing can be launched down a lane that would refuse it.
                    `aria-disabled` rather than `disabled` says the same thing to a
                    screen reader while keeping the control focusable. */}
                {lanes.map((l, i) => (
                  <button
                    key={`${l.lane}-${l.provisioner?.id ?? 'none'}`}
                    type="button"
                    aria-disabled={l.disabled}
                    aria-pressed={i === laneIdx}
                    onClick={() => setLaneIdx(i)}
                    className={`flex-1 text-left rounded-md border px-2.5 py-2 flex flex-col gap-0.5 ${
                      l.disabled
                        ? i === laneIdx
                          ? 'border-warn bg-warn/5 opacity-80'
                          : 'border-border bg-bg opacity-60'
                        : i === laneIdx
                          ? 'border-accent bg-accent-subtle'
                          : 'border-border bg-bg hover:border-border-strong'
                    }`}
                  >
                    <span className="font-mono text-[11px] uppercase tracking-[.06em] text-text">
                      {laneLabel(l.lane)}
                    </span>
                    <span className="text-[11px] text-muted">{l.hint}</span>
                  </button>
                ))}
              </div>
              {/* Why each unusable lane cannot be used, in a full sentence, for EVERY
                  one of them rather than only the selected chip. The chip's own line has
                  room for three words; this has room for the remedy, which is what the
                  reader needs -- and a chip that reads "Not set up" is one a reader will
                  not click, so a reason reachable only by selecting it is a reason its
                  own audience never sees. */}
              {lanes.some(l => l.blockedReason) && (
                <div className="mt-1.5 space-y-1" role="status">
                  {lanes.filter(l => l.blockedReason).map(l => (
                    <p
                      key={`why-${l.lane}-${l.provisioner?.id ?? 'none'}`}
                      className="m-0 text-[11px] text-muted-strong"
                    >
                      {l.blockedReason}
                    </p>
                  ))}
                </div>
              )}
            </>
          )}
        </DialogBody>
        <DialogFooter className="justify-between">
          <span className="text-[11px] text-muted-strong mr-auto">
            {i18nT('pages.settings.remoteCrewPanel.nothing_until_confirm')}
          </span>
          <Btn onClick={onClose}>{i18nT('pages.settings.remoteCrewPanel.cancel')}</Btn>
          <Btn primary onClick={startConfirm} disabled={!canContinue} data-testid="deploy-mate-continue">
            {i18nT('pages.settings.remoteCrewPanel.continue')}
          </Btn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
