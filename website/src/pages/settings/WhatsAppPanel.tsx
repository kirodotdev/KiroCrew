import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { QrCode, Loader2, Check, TriangleAlert, Trash2, Unlink } from 'lucide-react'
import {
  ApiError,
  api,
  type WhatsAppConfigData,
  type WhatsAppConfigSave,
  type WhatsAppGroup,
} from '../../api/client'
import { WhatsAppLogo } from '../../components/WhatsAppLogo'
import SimpleSelect from '../../components/SimpleSelect'
import { Btn, IconButton, PanelSectionHeader } from '../../components/ui'
import { SettingsInput, SettingsSelect, SettingsToggle } from '../../components/settings'
import { useChannelFolderSave } from '../../hooks/useChannelFolderSave'
import { useImeGuard } from '../../hooks/useImeGuard'
import { parseErrorCode } from '../../utils/errorReport'
import { TagListEditor } from './SlackPanel'

import { i18nT } from '../../i18n/t'
/** Brand name — do-not-translate, so it lives here rather than in the catalog. */
const CHANNEL_NAME = "WhatsApp"
const SETUP_GUIDE =
  'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/whatsapp-integration.md'

/** How often we poll the QR scan status while a login session is open. */
const POLL_MS = 1500
/** Give up on an unscanned QR after this long (WhatsApp expires linked-device
 *  codes on its own side too, so this only bounds our own polling). */
const QR_TTL_MS = 5 * 60 * 1000
/** How long the unlink button stays armed after the first click. An armed
 *  control left on screen becomes a trap for the next click minutes later. */
const UNLINK_ARM_MS = 5000
/** Cooldown a newly picked group starts with, matching the backend's own default
 *  (`_WHATSAPP_GROUP_COOLDOWN_DEFAULT`) so the row shows what would be stored
 *  if the operator never touches the field. */
const GROUP_COOLDOWN_DEFAULT_S = 120

/** DM policies as PAIRED entries, so a value can never drift from its label.
 *
 *  These were two positional arrays, and they drifted: `open` shipped labelled
 *  "Only allowed numbers", so an operator selecting what read as a restriction
 *  saved the policy that admits every sender. */
const DM_POLICIES = [
  { value: 'self', labelKey: 'pages.settings.whatsAppPanel.only_me_the_default' },
  { value: 'allowlist', labelKey: 'pages.settings.whatsAppPanel.me_plus_allowed_numbers' },
  { value: 'open', labelKey: 'pages.settings.whatsAppPanel.anyone_who_messages_you' },
  { value: 'disabled', labelKey: 'pages.settings.whatsAppPanel.nobody_ignore_all_messages' },
] as const

/** Group participation modes, paired for the same reason as DM_POLICIES. Order
 *  runs quietest first, so the mode that can speak unprompted is never the one
 *  a mis-click lands on. */
const GROUP_MODES = [
  { value: 'mention', labelKey: 'pages.settings.whatsAppPanel.mode_only_when_mentioned' },
  { value: 'rules', labelKey: 'pages.settings.whatsAppPanel.mode_also_when_the_rules_apply' },
  { value: 'off', labelKey: 'pages.settings.whatsAppPanel.mode_off_keeping_the_entry' },
] as const

type Phase =
  | 'idle'
  | 'starting'
  | 'waiting'
  | 'scanned'
  | 'confirmed'
  | 'expired'
  | 'error'
  | 'unavailable'

/** What an unlink attempt actually did. Three outcomes the operator must be able
 *  to tell apart, because two of them leave work to do. */
type UnlinkOutcome = { tone: 'ok' | 'warn' | 'danger'; text: string }

const OUTCOME_TEXT_CLS: Record<UnlinkOutcome['tone'], string> = {
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
}

/**
 * The status badge, driven off `state` rather than `configured`.
 *
 * `configured` cannot express the middle state at all: the gateway computes it as
 * `enabled AND connected`, so it is never true while the channel is down and
 * "paired but currently not running" collapses into "connected". `state` is the
 * live pairing lifecycle, and it is what separates never-paired (`unpaired`) from
 * a session that exists and is not carrying traffic (`logged_out`, `banned`,
 * `error`).
 *
 * One case stays genuinely unknowable: with the channel not running the gateway
 * has no client to ask, so it reports `unpaired` even when a session file is
 * still on disk. That reads as "Not paired", and the connection hint below the
 * badge is what carries the rest of the story.
 */
function statusBadge(config: WhatsAppConfigData): { text: string; textCls: string; dotCls: string } {
  if (config.connected) {
    return {
      text: i18nT('pages.settings.whatsAppPanel.connected'),
      textCls: 'text-ok',
      dotCls: 'bg-ok',
    }
  }
  if (config.state === 'pairing') {
    return {
      text: i18nT('pages.settings.whatsAppPanel.waiting_to_be_paired'),
      textCls: 'text-warn',
      dotCls: 'bg-warn',
    }
  }
  if (config.state === 'logged_out') {
    return {
      text: i18nT('pages.settings.whatsAppPanel.the_link_was_revoked'),
      textCls: 'text-warn',
      dotCls: 'bg-warn',
    }
  }
  if (config.state === 'banned') {
    return {
      text: i18nT('pages.settings.whatsAppPanel.blocked_by_whatsapp'),
      textCls: 'text-danger',
      dotCls: 'bg-danger',
    }
  }
  if (config.state === 'error') {
    return {
      text: i18nT('pages.settings.whatsAppPanel.paired_but_not_connected'),
      textCls: 'text-warn',
      dotCls: 'bg-warn',
    }
  }
  return {
    text: i18nT('pages.settings.whatsAppPanel.not_signed_in'),
    textCls: 'text-muted',
    dotCls: '',
  }
}

/** One line naming WHY the channel is down, the same shape the Slack, Teams,
 *  Webex, iMessage and bot-channel panels use. While pairing, the gateway's
 *  detail IS the scan instruction, which is still the accurate answer. */
function connectionHint(config: WhatsAppConfigData): string {
  if (config.connected) return ''
  if (config.connect_error) {
    return i18nT('pages.settings.whatsAppPanel.whatsapp_did_not_connect', {
      error: config.connect_error,
    })
  }
  if (config.enabled) {
    return i18nT('pages.settings.whatsAppPanel.the_channel_is_enabled_but_not_running')
  }
  return ''
}

/**
 * Why the gateway has no pairing code to hand over, and what would produce one.
 *
 * Pairing is started by the channel's own `connect()` and by nothing else: the
 * dashboard endpoint reports the live client's state and starts nothing, so there
 * is no request the panel can make that begins a pairing session. Every branch
 * here therefore names the restart instead of offering an action that would not
 * happen.
 */
function pairingBlockedReason(state: string): string {
  if (state === 'connected') {
    return i18nT('pages.settings.whatsAppPanel.already_paired_unlink_then_restart')
  }
  if (state === 'logged_out') {
    return i18nT('pages.settings.whatsAppPanel.the_link_was_revoked_restart_to_pair_again')
  }
  if (state === 'banned') {
    return i18nT('pages.settings.whatsAppPanel.blocked_number_cannot_pair')
  }
  return i18nT('pages.settings.whatsAppPanel.pairing_starts_when_the_channel_starts')
}

/**
 * One opted-in group.
 *
 * `rules` and the cooldown commit on blur, not per keystroke: this panel has no
 * Save button, so a per-keystroke write would issue one PUT per character and
 * store every half-typed sentence on the way.
 */
function GroupRow({
  group,
  readOnly,
  onChange,
  onRemove,
}: {
  group: WhatsAppGroup
  readOnly: boolean
  onChange: (next: WhatsAppGroup) => void
  onRemove: () => void
}) {
  const ime = useImeGuard()
  const [rules, setRules] = useState(group.rules)
  const [cooldown, setCooldown] = useState(String(group.cooldown_s))
  // Re-seeded from the server's copy of THIS group, so an edit made elsewhere
  // lands. A refetch that returns the value already on screen leaves the deps
  // unchanged and cannot clobber a draft mid-edit.
  useEffect(() => setRules(group.rules), [group.rules])
  useEffect(() => setCooldown(String(group.cooldown_s)), [group.cooldown_s])

  const commitRules = () => {
    const next = rules.trim()
    if (next !== group.rules) onChange({ ...group, rules: next })
  }
  const commitCooldown = () => {
    const next = Math.max(0, Math.floor(Number(cooldown)))
    // A field emptied or filled with text is not a zero cooldown, zero means
    // "no rate limit at all", which is the opposite of a cautious default.
    if (!Number.isFinite(next)) {
      setCooldown(String(group.cooldown_s))
      return
    }
    setCooldown(String(next))
    if (next !== group.cooldown_s) onChange({ ...group, cooldown_s: next })
  }

  return (
    <div
      className="rounded-lg border border-border bg-card p-3 flex flex-col gap-1"
      data-testid={`whatsapp-group-${group.jid}`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[13px] font-semibold text-text-strong truncate">
            {group.name || group.jid}
          </div>
          <div className="text-[11.5px] text-muted font-mono truncate">{group.jid}</div>
        </div>
        {!readOnly && (
          <IconButton
            variant="danger"
            onClick={onRemove}
            aria-label={i18nT('pages.settings.whatsAppPanel.remove_name_from_the_group_list', {
              name: group.name || group.jid,
            })}
            data-testid={`whatsapp-group-remove-${group.jid}`}
          >
            <Trash2 className="lucide-inline" />
          </IconButton>
        )}
      </div>
      <SettingsSelect
        label={i18nT('pages.settings.whatsAppPanel.how_the_agent_joins_in')}
        options={GROUP_MODES.map(m => m.value)}
        optionLabels={GROUP_MODES.map(m => i18nT(m.labelKey))}
        value={group.mode}
        disabled={readOnly}
        onChange={v => onChange({ ...group, mode: v as WhatsAppGroup['mode'] })}
      />
      {group.mode === 'rules' && (
        <>
          <SettingsInput
            multiline
            label={i18nT('pages.settings.whatsAppPanel.when_the_agent_may_speak')}
            description={i18nT(
              'pages.settings.whatsAppPanel.the_agent_stays_silent_unless_these_apply',
            )}
            value={rules}
            disabled={readOnly}
            placeholder={i18nT('pages.settings.whatsAppPanel.rules_example_placeholder')}
            onChange={setRules}
            onBlur={commitRules}
          />
          <SettingsInput
            type="number"
            min={0}
            label={i18nT('pages.settings.whatsAppPanel.seconds_between_unprompted_replies')}
            value={cooldown}
            disabled={readOnly}
            onChange={setCooldown}
            {...ime.bindComposition({ onBlur: commitCooldown })}
            onKeyDown={e => {
              if (e.key !== 'Enter') return
              // Early-return BEFORE the blur: a committing IME Enter must not commit.
              if (ime.isComposing(e)) return
              e.currentTarget.blur()
            }}
          />
        </>
      )}
    </div>
  )
}

/**
 * WhatsApp (personal account) channel settings.
 *
 * Unlike the token-based channels there is nothing to paste: WhatsApp pairs as a
 * LINKED DEVICE on the operator's own account, so the credential is a session
 * store the gateway holds and the panel never sees. That shapes both halves of
 * this panel:
 *
 * - **Pairing is revealed, not started.** The gateway emits a rotating code from
 *   inside the channel's own connect(); the dashboard endpoint reports state and
 *   starts nothing. So the panel shows a code the gateway already has and
 *   otherwise says what would produce one, rather than offering a button that
 *   silently does nothing.
 * - **Unlink is the only destructive control**, and it is the account's only
 *   in-product revoke: a linked device can read and send every chat, with no
 *   second factor and no expiry.
 */
export function WhatsAppPanel() {
  const ime = useImeGuard()
  const qc = useQueryClient()
  const { data, isError } = useQuery({
    queryKey: ['whatsapp-config'],
    queryFn: api.getWhatsAppConfig,
    retry: false,
  })

  const [phase, setPhase] = useState<Phase>('idle')
  const [qrImg, setQrImg] = useState('')
  const [errMsg, setErrMsg] = useState('')
  /** The state the gateway reported when it had no code to give. */
  const [blockedState, setBlockedState] = useState('')
  const deadlineRef = useRef(0)
  // Server state goes through React Query, including the QR scan poll: the
  // status endpoint is polled via refetchInterval while a login session is open
  // and stops as soon as the flow reaches a terminal phase, so there is no
  // hand-rolled timer to leak on unmount.
  const polling = phase === 'waiting' || phase === 'scanned'
  const { data: qrStatus } = useQuery({
    queryKey: ['whatsapp-qr-status'],
    queryFn: () => api.whatsAppQrStatus(),
    enabled: polling,
    refetchInterval: polling ? POLL_MS : false,
    retry: false,
    gcTime: 0,
  })

  // Drive the phase machine off the polled status.
  useEffect(() => {
    if (!polling || !qrStatus) return
    if (qrStatus.state === 'connected') {
      setPhase('confirmed')
      setQrImg('')
      qc.invalidateQueries({ queryKey: ['whatsapp-config'] })
      return
    }
    if (qrStatus.state === 'logged_out' || qrStatus.state === 'error') {
      setErrMsg(qrStatus.detail || '')
      setPhase('error')
      setQrImg('')
      return
    }
    if (qrStatus.qr_data_url) setQrImg(qrStatus.qr_data_url)
  }, [qrStatus, polling, qc])

  // Give up on a code the user never scanned (WhatsApp expires it on its side).
  useEffect(() => {
    if (!polling) return
    const id = setTimeout(() => {
      if (Date.now() > deadlineRef.current) {
        setPhase('expired')
        setQrImg('')
      }
    }, QR_TTL_MS)
    return () => clearTimeout(id)
  }, [polling])

  const readOnly = !!data?.read_only

  const showCode = useMutation({
    mutationFn: () => api.whatsAppQrStart(),
    onMutate: () => {
      setErrMsg('')
      setPhase('starting')
    },
    onSuccess: r => {
      if (r.error || !r.ok) {
        setErrMsg(r.error || i18nT('pages.settings.whatsAppPanel.could_not_start_pairing'))
        setPhase('error')
        return
      }
      // The endpoint answers with the live client's state, which is the only
      // authority on whether a code exists, the cached config read can be up to
      // a poll interval stale. A non-pairing state means there is nothing to
      // show, and pretending otherwise would leave a spinner up forever.
      if (r.state && r.state !== 'pairing') {
        setBlockedState(r.state)
        setPhase('unavailable')
        return
      }
      deadlineRef.current = Date.now() + QR_TTL_MS
      setPhase('waiting')
    },
    onError: (e: unknown) => {
      const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
      setErrMsg(
        code === 'channel_not_running'
          ? i18nT('pages.settings.whatsAppPanel.the_channel_is_not_running_enable_and_restart')
          : e instanceof Error
            ? e.message
            : i18nT('pages.settings.whatsAppPanel.could_not_start_the_login_flow'),
      )
      setPhase('error')
    },
  })

  const [unlinkArmed, setUnlinkArmed] = useState(false)
  const [unlinkOutcome, setUnlinkOutcome] = useState<UnlinkOutcome | null>(null)
  useEffect(() => {
    if (!unlinkArmed) return
    const timer = window.setTimeout(() => setUnlinkArmed(false), UNLINK_ARM_MS)
    return () => window.clearTimeout(timer)
  }, [unlinkArmed])

  const unlink = useMutation({
    mutationFn: () => api.whatsAppUnlink(),
    onMutate: () => setUnlinkOutcome(null),
    onSuccess: r => {
      // Two DIFFERENT successes, and collapsing them would be the worse of the
      // two: `session_file_kept` means the device IS unlinked but the local store
      // survived, and that file holds the linked-device keys.
      setUnlinkOutcome(
        r.code === 'session_file_kept' || r.warning
          ? {
              tone: 'warn',
              text: i18nT('pages.settings.whatsAppPanel.unlinked_but_the_session_file_was_kept'),
            }
          : {
              tone: 'ok',
              text: i18nT('pages.settings.whatsAppPanel.unlinked_restart_to_pair_again'),
            },
      )
      qc.invalidateQueries({ queryKey: ['whatsapp-config'] })
    },
    onError: (e: unknown) => {
      // A 502 is NOT a partial success. WhatsApp refused the logout, so the device
      // is still linked and still able to read and send, and the gateway kept the
      // session on purpose because it is the only credential that can retry.
      // Reporting this as done leaves a live device on the account.
      const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
      if (code === 'logout_failed') {
        setUnlinkOutcome({
          tone: 'danger',
          text: i18nT('pages.settings.whatsAppPanel.whatsapp_refused_the_unlink_still_linked'),
        })
        return
      }
      if (code === 'channel_not_running') {
        setUnlinkOutcome({
          tone: 'danger',
          text: i18nT('pages.settings.whatsAppPanel.channel_not_running_so_nothing_was_unlinked'),
        })
        return
      }
      setUnlinkOutcome({
        tone: 'danger',
        text:
          e instanceof Error && e.message
            ? e.message
            : i18nT('pages.settings.whatsAppPanel.could_not_unlink_the_device'),
      })
    },
  })

  const saveConfig = useMutation({
    mutationFn: (patch: Partial<WhatsAppConfigSave>) => api.saveWhatsAppConfig(patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['whatsapp-config'] })
    },
  })
  // `onRevert` undoes an optimistic local flip when the server rejects the patch.
  // It is passed by the toggle and NOT by the name field: a rejected name must
  // keep the text the user typed (that is what lets them correct it), while a
  // rejected toggle must snap back to the server's truth, or the switch reads
  // "off" while the gateway is still filing sessions.
  //
  // `mutateAsync` rather than `mutate(patch, {…})`: per-call callbacks live on the
  // mutation OBSERVER, and this panel saves on change, so a second save starting
  // before the first resolves replaces them and the first call's handlers never
  // run. Clicking the toggle is what blurs the name field, so "rename, then
  // switch off" issues both saves back to back — the ordinary path, not a rare
  // race. Attaching the handling to each returned promise keeps every call's own
  // outcome. The mutation-level onSuccess still fires for shared work
  // (invalidating the query).
  //
  // Feedback (the error line, the folder-name "Saved." check) lives HERE, on the
  // per-call chain, guarded by a sequence: back-to-back saves resolve out of
  // order, and only the LATEST attempt may speak for the panel. A slow rename
  // resolving after a newer one was rejected must neither clear that rejection's
  // error nor paint "Saved." next to it — both would assert the failed draft
  // was stored.
  //
  // Folder field + save sequencing live in a shared hook: WeChat's and
  // WhatsApp's panels are the two QR-paired channels and carried
  // byte-identical copies of this. See useChannelFolderSave for the three
  // invariants (accepted-vs-draft name, folder-only sequencing, and
  // ownership-aware error clearing).
  const {
    folderOn,
    folderName,
    setFolderName,
    folderSaved,
    saveError,
    toggleFolder,
    commitFolderName,
    save,
  } = useChannelFolderSave<WhatsAppConfigSave>({
    serverFolder: data?.session_folder,
    defaultName: CHANNEL_NAME,
    mutate: patch => saveConfig.mutateAsync(patch),
  })

  const connected = !!data?.connected
  const liveState = data?.state || 'unpaired'
  const codeAvailable = liveState === 'pairing'
  const groups = data?.groups || []
  const badge = data ? statusBadge(data) : null
  const hint = data ? connectionHint(data) : ''
  // One explanation, never two: after a refused click the config read catches up
  // and both the standing line and the click's answer would say the same thing.
  const refusedClick = phase === 'unavailable'
  const showBlockedReason = !!data && (refusedClick || (!codeAvailable && phase !== 'confirmed'))

  // The joined-group list only exists while the channel is connected (the
  // endpoint answers with an empty list otherwise), so it is not fetched when it
  // could only return nothing.
  const { data: joinedGroups } = useQuery({
    queryKey: ['whatsapp-groups'],
    queryFn: api.getWhatsAppGroups,
    enabled: connected,
    retry: false,
  })
  const configuredJids = new Set(groups.map(g => g.jid))
  const addable = (joinedGroups?.groups || []).filter(g => !configuredJids.has(g.jid))

  // Each group edit rebuilds the WHOLE list, and this panel saves on change, so
  // the payload must compose on the PENDING list rather than on the server
  // snapshot: edit one group and then another before the refetch lands and both
  // reads of `data.groups` are pre-edit, so the second write silently discards the
  // first. Seeding the cache with the new list before the request makes the next
  // `groups` read include this edit. The server still has the last word, because
  // `saveConfig.onSuccess` invalidates; a REJECTED save invalidates here instead,
  // so an optimistic list is never left standing for a patch the gateway refused.
  const saveGroups = (next: WhatsAppGroup[]) => {
    qc.setQueryData(['whatsapp-config'], (prev: WhatsAppConfigData | undefined) =>
      prev ? { ...prev, groups: next } : prev
    )
    // `onRevert` is the hook's own seam for an optimistic local change the server
    // then rejects; invalidating drops the optimistic list back to the gateway's
    // truth rather than leaving a patch it refused on screen.
    save({ groups: next }, () => {
      qc.invalidateQueries({ queryKey: ['whatsapp-config'] })
    })
  }
  const addGroup = (jid: string) => {
    const picked = addable.find(g => g.jid === jid)
    if (!picked) return
    // `mention` on purpose: the mode that never speaks unprompted, so adding a
    // group cannot by itself put the agent into a conversation.
    saveGroups([
      ...groups,
      {
        jid: picked.jid,
        name: picked.name,
        mode: 'mention',
        rules: '',
        cooldown_s: GROUP_COOLDOWN_DEFAULT_S,
      },
    ])
  }

  return (
    <div className="flex flex-col gap-5" data-testid="whatsapp-panel">
      {/* header */}
      <div className="flex items-start gap-3">
        <span className="mt-0.5 shrink-0">
          <WhatsAppLogo size={20} />
        </span>
        <div className="min-w-0">
          <h3 className="text-[15px] font-semibold text-text-strong m-0">{i18nT('pages.settings.whatsAppPanel.whatsapp')}</h3>
          <p className="text-[12.5px] text-muted mt-1 mb-0">
            {i18nT('pages.settings.whatsAppPanel.talk_to_your_agent_from_your_own_whatsapp')}
          </p>
        </div>
      </div>

      {/* status */}
      <div
        className="flex flex-col gap-1.5 rounded-lg border border-border bg-card px-3.5 py-2.5"
        data-testid="whatsapp-status"
      >
        {isError ? (
          <span className="text-[12.5px] text-muted">{i18nT('pages.settings.whatsAppPanel.status_unavailable')}</span>
        ) : !badge ? (
          <span className="text-[12.5px] text-muted">{i18nT('pages.settings.channelsPanel.checking')}</span>
        ) : (
          <>
            <span className="flex items-center gap-2">
              {badge.dotCls && (
                <span
                  aria-hidden="true"
                  className={`w-1.5 h-1.5 rounded-full shrink-0 ${badge.dotCls}`}
                />
              )}
              <span className={`text-[12.5px] font-medium ${badge.textCls}`}>{badge.text}</span>
            </span>
            {/* The reason, not just the verdict. Without it a down channel gives
                the operator nothing to act on, and the gateway already carries
                the detail (a missing dependency extra, a refused pairing, a
                dropped socket). */}
            {hint && (
              <span
                className="text-[11.5px] text-warn flex items-start gap-1.5"
                data-testid="whatsapp-connect-error"
              >
                <TriangleAlert className="lucide-inline mt-0.5" aria-hidden="true" />
                <span className="min-w-0 break-words">{hint}</span>
              </span>
            )}
          </>
        )}
      </div>

      {/* pairing */}
      <div className="rounded-lg border border-border bg-card p-3.5">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="min-w-0">
            <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.whatsAppPanel.pair_with_whatsapp')}</div>
            <div className="text-[11.5px] text-muted mt-0.5">
              {i18nT('pages.settings.whatsAppPanel.scan_the_code_from_linked_devices')}
            </div>
            {/* Beside the Pair control, in warn tone, because this is the one
                decision on the panel that can cost the operator their personal
                number and it is irreversible from here. The group footer also
                mentions the protocol, but a cold reader pairing a phone never
                reaches it, and muted 11.5px fine print is not where a stake this
                size belongs. The setup guide carries the long form. */}
            <div
              className="text-[11.5px] text-warn mt-1 flex items-start gap-1.5"
              data-testid="whatsapp-pairing-risk"
            >
              <TriangleAlert className="lucide-inline mt-0.5" aria-hidden="true" />
              <span className="min-w-0 break-words">
                {i18nT('pages.settings.whatsAppPanel.unofficial_protocol_ban_risk')}
              </span>
            </div>
          </div>
          {!readOnly && (
            <Btn
              onClick={() => showCode.mutate()}
              disabled={
                !codeAvailable || phase === 'starting' || phase === 'waiting' || phase === 'scanned'
              }
              data-testid="whatsapp-connect"
            >
              {phase === 'starting' ? (
                <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
              ) : (
                <QrCode className="lucide-inline" aria-hidden="true" />
              )}
              {i18nT('pages.settings.whatsAppPanel.show_pairing_code')}
            </Btn>
          )}
        </div>

        {/* Said plainly rather than left to a disabled button: no dashboard call
            can begin pairing, so the operator needs the sentence that names what
            can. Louder when it answers a click than when it is just standing
            there. */}
        {showBlockedReason && (
          <div
            className={`mt-2 flex items-start gap-1.5 text-[11.5px] ${refusedClick ? 'text-warn' : 'text-muted'}`}
            data-testid="whatsapp-pairing-unavailable"
          >
            {refusedClick && <TriangleAlert className="lucide-inline mt-0.5" aria-hidden="true" />}
            <span className="min-w-0 break-words">
              {pairingBlockedReason(refusedClick ? blockedState : liveState)}
            </span>
          </div>
        )}

        {(phase === 'waiting' || phase === 'scanned') && (
          <div className="mt-3 flex flex-col items-center gap-2" data-testid="whatsapp-qr">
            {qrImg ? (
              <img
                src={qrImg}
                alt={i18nT('pages.settings.whatsAppPanel.whatsapp_pairing_qr_code')}
                width={180}
                height={180}
                className="rounded-md bg-white p-2"
              />
            ) : (
              <div className="text-[12px] text-muted">{i18nT('pages.settings.whatsAppPanel.waiting_for_a_code')}</div>
            )}
            <div className="flex items-center gap-1.5 text-[12px] text-muted">
              <Loader2 size={12} className="animate-spin" />
              {phase === 'scanned' ? i18nT('pages.settings.whatsAppPanel.scanned_confirm_on_your_phone') : i18nT('pages.settings.whatsAppPanel.waiting_for_scan')}
            </div>
          </div>
        )}

        {phase === 'confirmed' && (
          <div
            className="mt-3 flex items-center gap-1.5 text-[12.5px] text-ok"
            data-testid="whatsapp-confirmed"
          >
            <Check size={13} /> {i18nT('pages.settings.whatsAppPanel.signed_in_restart_the_gateway_to_start_receiving')}
          </div>
        )}

        {phase === 'expired' && (
          <div className="mt-3 flex items-center gap-1.5 text-[12.5px] text-warn" data-testid="whatsapp-expired">
            <TriangleAlert size={13} /> {i18nT('pages.settings.whatsAppPanel.the_code_expired_try_again')}
          </div>
        )}

        {phase === 'error' && (
          <div className="mt-3 flex items-center gap-1.5 text-[12.5px] text-danger" data-testid="whatsapp-error">
            <TriangleAlert size={13} /> {errMsg}
          </div>
        )}
      </div>

      {/* unlink, the only revoke this product has.
          Rendered whatever the reported state, and never hidden on `unpaired`:
          that is exactly the state the gateway reports when the channel is NOT
          running, which is when a device can still be linked and the operator
          most needs the control. Every outcome is reported, including the two
          that leave work to do, so an attempt that changed nothing cannot read
          as a revoke. */}
      {!readOnly && (
        <div className="rounded-lg border border-border bg-card p-3.5" data-testid="whatsapp-unlink">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div className="min-w-0">
              <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.whatsAppPanel.unlink_this_device')}</div>
              <div className="text-[11.5px] text-muted mt-0.5">
                {i18nT('pages.settings.whatsAppPanel.revokes_the_linked_device_on_your_account')}
              </div>
            </div>
            <Btn
              danger
              disabled={unlink.isPending}
              data-testid="whatsapp-unlink-button"
              // No static aria-label: the visible text IS the accessible name and
              // it is what announces the armed step. A pinned label would say the
              // same thing on both clicks, hiding the confirmation from exactly
              // the users who cannot see the colour change.
              onClick={() => {
                if (unlink.isPending) return
                if (!unlinkArmed) {
                  setUnlinkArmed(true)
                  return
                }
                setUnlinkArmed(false)
                unlink.mutate()
              }}
            >
              {unlink.isPending ? (
                <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
              ) : (
                <Unlink className="lucide-inline" aria-hidden="true" />
              )}
              {unlinkArmed
                ? i18nT('pages.settings.whatsAppPanel.confirm_unlink')
                : i18nT('pages.settings.whatsAppPanel.unlink_this_device')}
            </Btn>
          </div>
          {unlinkOutcome && (
            <p
              className={`mt-2 mb-0 flex items-start gap-1.5 text-[12px] ${OUTCOME_TEXT_CLS[unlinkOutcome.tone]}`}
              role={unlinkOutcome.tone === 'ok' ? 'status' : 'alert'}
              data-testid="whatsapp-unlink-outcome"
            >
              {unlinkOutcome.tone === 'ok' ? (
                <Check className="lucide-inline mt-0.5" aria-hidden="true" />
              ) : (
                <TriangleAlert className="lucide-inline mt-0.5" aria-hidden="true" />
              )}
              <span className="min-w-0 break-words">{unlinkOutcome.text}</span>
            </p>
          )}
        </div>
      )}

      {/* enable + access policy */}
      {/* Every other channel panel renders its enable switch as SettingsToggle;
          the shared component owns the label association (visible text doubles
          as the switch's accessible name) and the keyboard/AT semantics.
          data-testid lives on this wrapper because SettingsToggle exposes only
          data-setting-label — same move as whatsapp-dm-policy below. */}
      {/* max-w: this panel has no SettingsCard, so an uncapped row would make
          the whole pane width a Clickable save surface (this panel autosaves —
          a stray click in the empty gap would silently disable the channel)
          and push the switch far from its label. Content-scaling matches the
          dm-policy select below. */}
      <div data-testid="whatsapp-enabled" className="max-w-[380px]">
        <SettingsToggle
          label={i18nT('pages.settings.whatsAppPanel.enable_the_whatsapp_channel')}
          checked={!!data?.enabled}
          disabled={readOnly}
          onChange={v => save({ enabled: v })}
        />
      </div>

      <div>
        {/* maxWidth: the trigger is w-full and this field is a stretch flex item,
            so without a cap it would span the whole panel while every
            neighbouring control stays content-scaled. data-testid stays on this
            wrapper so the Playwright drive (scripts/test-whatsapp-panel.mjs)
            still finds the field. */}
        <div className="block" data-testid="whatsapp-dm-policy" style={{ maxWidth: 280 }}>
          <SettingsSelect
            label={i18nT('pages.settings.whatsAppPanel.who_can_message_the_bot')}
            // Kept as paired entries: as two positional arrays these drifted,
            // and `open` shipped labelled "Only allowed numbers" -- a label
            // claiming a restriction that policy does not apply, so an operator
            // choosing it opened the channel to every sender.
            options={DM_POLICIES.map(p => p.value)}
            optionLabels={DM_POLICIES.map(p => i18nT(p.labelKey))}
            value={data?.dm_policy || 'self'}
            disabled={readOnly}
            onChange={v => save({ dm_policy: v as 'self' | 'allowlist' | 'open' | 'disabled' })}
          />
        </div>
      </div>

      {data?.dm_policy === 'allowlist' && (
        <div data-testid="whatsapp-allowlist">
          <TagListEditor
            label={i18nT('pages.settings.whatsAppPanel.allowed_wa_ids')}
            description={i18nT('pages.settings.whatsAppPanel.allowed_numbers_empty_adds_nobody')}
            values={data?.allowed_wa_ids || []}
            placeholder={i18nT('pages.settings.whatsAppPanel.phone_number_placeholder')}
            onChange={(vals: string[]) => save({ allowed_wa_ids: vals })}
            readOnly={readOnly}
          />
        </div>
      )}

      {/* groups: opt-in, one entry per group, edited here rather than by hand in
          config.json. */}
      <div className="flex flex-col gap-2" data-testid="whatsapp-groups">
        <PanelSectionHeader
          label={i18nT('pages.settings.whatsAppPanel.groups')}
          count={groups.length}
        />
        <div className="text-[11.5px] text-muted">
          {i18nT('pages.settings.whatsAppPanel.the_agent_ignores_every_group_not_listed')}
        </div>
        {groups.map(g => (
          <GroupRow
            key={g.jid}
            group={g}
            readOnly={readOnly}
            onChange={next => saveGroups(groups.map(x => (x.jid === g.jid ? next : x)))}
            onRemove={() => saveGroups(groups.filter(x => x.jid !== g.jid))}
          />
        ))}
        {!readOnly && (
          <div style={{ maxWidth: 280 }} data-testid="whatsapp-group-picker">
            {addable.length > 0 ? (
              <SimpleSelect
                options={addable.map(g => g.jid)}
                optionLabels={addable.map(g => g.name || g.jid)}
                value=""
                triggerFallback={i18nT('pages.settings.whatsAppPanel.add_a_group')}
                onChange={addGroup}
                aria-label={i18nT('pages.settings.whatsAppPanel.add_a_group')}
              />
            ) : (
              <div className="text-[11.5px] text-muted">
                {connected
                  ? i18nT('pages.settings.whatsAppPanel.every_group_you_joined_is_already_listed')
                  : i18nT('pages.settings.whatsAppPanel.your_groups_load_once_the_channel_is_connected')}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Optional session filing, rendered from the same primitives, in the same
          place, with the same divider as every other channel's copy of this
          setting (`BotChannelPanel` for Telegram/Discord/WeCom, and the Slack,
          Teams and Webex panels): bottom of the panel, below a rule, switch above
          the name field. It used to be a bare checkbox wedged between the
          DM-policy picker and the allowlist, which read as part of the
          access-control block and sent users looking for it at the bottom, where
          it was not.

          Off by default: WeChat conversations stay unfiled, and a configured name
          IS the on-state (the backend has one field, where "" means off).

          This panel has no Save button — every other control saves on change — so
          the toggle must persist immediately. Revealing the field without saving
          loses the setting for anyone who turns it on, sees the name already
          filled in, and leaves. The NAME still commits on blur / Enter rather
          than per keystroke, which is why `SettingsInput` is given
          `onBlur`/`onKeyDown` here and the other panels (which have a Save
          button) pass neither. Renaming does not strand the folder it creates:
          the channel's folder is found by its stamp, so a new name relabels that
          same folder instead of building a second one. */}
      <div className="border-t border-border mt-4 pt-4" data-testid="whatsapp-session-folder">
        <SettingsToggle
          label={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder')}
          description={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder_desc', { channel: CHANNEL_NAME })}
          checked={folderOn}
          disabled={readOnly}
          onChange={toggleFolder}
        />
        {folderOn && (
          <div className="mt-4">
            <SettingsInput
              label={i18nT('pages.settings.botChannelPanel.session_folder_name')}
              description={i18nT('pages.settings.whatsAppPanel.created_for_you_when_you_turn_this_on_if_it_does')}
              value={folderName}
              disabled={readOnly}
              placeholder={CHANNEL_NAME}
              onChange={setFolderName}
              {...ime.bindComposition({ onBlur: commitFolderName })}
              onKeyDown={e => {
                if (e.key !== 'Enter') return
                // Early-return BEFORE the blur: a committing IME Enter must not commit.
                if (ime.isComposing(e)) return
                e.currentTarget.blur()
              }}
            />
            {folderSaved && (
              <p
                className="inline-flex items-center gap-1.5 text-[12px] text-ok mt-1 mb-0"
                role="status"
                data-testid="whatsapp-session-folder-saved"
              >
                <Check size={13} /> {i18nT('pages.settings.botChannelPanel.saved')}
              </p>
            )}
          </div>
        )}
        {/* Outside the `folderOn` block on purpose: when an ENABLE is rejected
            the revert returns the switch to the server's value — off, since the
            server has no folder — so an error nested in that block would unmount
            before it could paint and the failure would be silent. */}
        {saveError && (
          <p
            className="text-[11.5px] text-danger mt-1 mb-0"
            role="alert"
            data-testid="whatsapp-session-folder-error"
          >
            {saveError}
          </p>
        )}
      </div>

      <p className="text-[11.5px] text-muted m-0">
        {i18nT('pages.settings.whatsAppPanel.group_participation_is_opt_in_per_group')}{' '}
        <a
          href={SETUP_GUIDE}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent hover:underline"
        >
          {i18nT('pages.settings.whatsAppPanel.setup_guide')}
        </a>
      </p>
    </div>
  )
}
