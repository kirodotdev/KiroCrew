import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Plus, X } from 'lucide-react'
import { SettingsToggle } from '../../components/settings'
import { Input, Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { useImeGuard } from '../../hooks/useImeGuard'
import { api, type SlackSharedAccessData, type SlackSharedUser } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { i18nT } from '../../i18n/t'
import { splitOnPlaceholder } from '../../lib/splitOnPlaceholder'
import { fmtList } from '../../i18n/format'

/** Query key of `GET /api/slack/shared-access`. */
export const SLACK_SHARED_ACCESS_KEY = ['slack-shared-access'] as const

/** A Slack member id: U or W, then upper-case letters and digits (the server's rule). */
function isSlackUserId(v: string): boolean {
  if (v.length < 2 || v.length > 20 || (v[0] !== 'U' && v[0] !== 'W')) return false
  for (const ch of v.slice(1)) {
    if (!((ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9'))) return false
  }
  return true
}

/** The refusal codes the shared-access routes answer with that name a reason. */
const REASON_CODES = new Set([
  'user_not_found', 'bot_user', 'deactivated', 'other_workspace', 'owner', 'malformed_id',
  'unverifiable', 'not_applied',
])

/** The plain-words reason Slack did not confirm an id, from the server's code. */
function reasonText(code: string): string {
  switch (code) {
    case 'user_not_found': return i18nT('pages.settings.slackSharedAccess.reason_not_found')
    case 'bot_user': return i18nT('pages.settings.slackSharedAccess.reason_bot')
    case 'deactivated': return i18nT('pages.settings.slackSharedAccess.reason_deactivated')
    case 'other_workspace': return i18nT('pages.settings.slackSharedAccess.reason_other_workspace')
    case 'owner': return i18nT('pages.settings.slackSharedAccess.reason_owner')
    case 'malformed_id': return i18nT('pages.settings.slackSharedAccess.reason_malformed')
    case 'not_applied': return i18nT('pages.settings.slackSharedAccess.not_applied')
    default: return i18nT('pages.settings.slackSharedAccess.reason_unverifiable')
  }
}

/** The codes that mean the check itself failed (Slack unreachable), not an answer about the id. */
const CHECK_FAILURES = new Set(['unverifiable'])

/** Whether *code* says the check failed rather than refused the id; an unknown code counts as failed. */
function checkFailed(code: string): boolean {
  return CHECK_FAILURES.has(code) || !REASON_CODES.has(code)
}

/** The server's refusal code, or '' when the body carries none. */
function refusalCode(e: unknown): string {
  if (!(e instanceof ApiError)) return ''
  try {
    const body = JSON.parse(e.body) as { code?: unknown }
    return typeof body.code === 'string' ? body.code : ''
  } catch {
    return ''
  }
}

/** Who is listed, whether Slack confirmed each and why, as one comparable string. */
function listKey(users: SlackSharedUser[]): string {
  return JSON.stringify(users.map(u => [u.user_id, u.confirmed, u.reason]).sort())
}

/** What a 409 `not_applied` says Kiro Crew could not read (`slack`, if anything). */
function unreadNames(e: unknown): string[] {
  if (!(e instanceof ApiError)) return []
  try {
    const body = JSON.parse(e.body) as { unread?: unknown }
    return Array.isArray(body.unread) ? body.unread.filter((v): v is string => typeof v === 'string' && v !== '') : []
  } catch {
    return []
  }
}

/** The config key that turns Slack's check of each listed ID on. */
const VERIFY_KEY = 'slack.shared_access.verify_with_slack'

/** What the owner must read before sharing, shown on the page and in the confirm. */
function SharedAccessWarning({ id, unchecked }: { id?: string, unchecked: boolean }) {
  return (
    <div id={id} role="note" className="flex items-start gap-2 px-3 py-2 my-2 text-[13px] rounded-md bg-warn/10 text-warn-text border border-warn/30">
      <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
      <div className="min-w-0">
        <div className="font-semibold">{i18nT('pages.settings.slackSharedAccess.warning_title')}</div>
        <ul className="list-disc pl-4 mt-1 space-y-0.5">
          <li>{i18nT('pages.settings.slackSharedAccess.warning_acts_as_you')}</li>
          <li>{i18nT('pages.settings.slackSharedAccess.warning_threads')}</li>
          <li>{i18nT('pages.settings.slackSharedAccess.warning_memory')}</li>
          {/* Named, not linked: this warning sits in the Slack settings form and its
              confirm, whose unsaved fields a navigation away would unmount and lose. */}
          <li>
            {splitOnPlaceholder(i18nT('pages.settings.slackSharedAccess.warning_tools'), 'page').map((part, i) =>
              part === null ? (
                <strong key="page">{i18nT('nav.schedule')}</strong>
              ) : (
                <span key={i}>{part}</span>
              ),
            )}
          </li>
          {unchecked && (
            <li>{i18nT('pages.settings.slackSharedAccess.warning_not_checked', { key: VERIFY_KEY })}</li>
          )}
          <li>{i18nT('pages.settings.slackSharedAccess.warning_trust')}</li>
        </ul>
      </div>
    </div>
  )
}

function SharedUserRow({ user, readOnly, sharing, onRemove, removing }: {
  user: SlackSharedUser
  readOnly: boolean
  /** Sharing is on: only then can a Slack answer later give this person access. */
  sharing: boolean
  onRemove: (id: string) => void
  removing: boolean
}) {
  const label = user.name || user.user_id
  // While off, a Slack answer gives nobody access: the row says only the reason.
  // The read-only view has no Remove control, so it drops "Remove them to keep them out".
  const later = sharing
    ? ` ${i18nT(readOnly ? 'pages.settings.slackSharedAccess.not_confirmed_ro' : 'pages.settings.slackSharedAccess.not_confirmed')}`
    : ''
  return (
    <li className="flex items-center gap-2 py-1 text-[13px]">
      <span className="min-w-0 flex-1">
        {user.name && <span className="font-semibold text-text">{user.name}</span>}
        <span className="ml-2 font-mono text-[12px] text-muted break-all">{user.user_id}</span>
        {user.confirmed && user.reason === 'listed' && (
          <span className="block text-[12px] text-muted">{i18nT('pages.settings.slackSharedAccess.reason_listed')}</span>
        )}
        {!user.confirmed && checkFailed(user.reason) && (
          /* No hand-off: the member-ID draft and the Slack settings form around it
             are unsaved; a hand-off to the chat unmounts this panel and loses them. */
          <span className="block">
            <ErrorNotice impact="partial" variant="inline"
              message={`${i18nT('pages.settings.slackSharedAccess.reason_unverifiable_row')}${later}`} />
          </span>
        )}
        {!user.confirmed && !checkFailed(user.reason) && (
          <span className="block text-[12px] text-warn-text">
            {reasonText(user.reason)}
            {/* The owner's own ID needs no consequence line: they already have access. */}
            {user.reason !== 'owner' && later}
          </span>
        )}
      </span>
      {!readOnly && (
        <button type="button" onClick={() => onRemove(user.user_id)} disabled={removing}
          className="shrink-0 text-muted hover:text-danger transition-colors disabled:opacity-40"
          aria-label={i18nT('pages.settings.slackPanel.remove', { name: label })}>
          <X size={14} />
        </button>
      )}
    </li>
  )
}

/**
 * Settings → Messaging Channels → Slack → Share Slack access (`slack.shared_access`).
 *
 * Off by default. The warning stays on the page whatever the switch says, and
 * turning it on goes through a confirm that names who gets access and repeats it;
 * with nobody listed, nobody confirmed, or anyone unconfirmed it does not turn on.
 * Changes apply at once
 * (the gateway reads them on the next Slack event); there is no separate Save.
 */
export function SlackSharedAccess() {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const ime = useImeGuard()
  const [draft, setDraft] = useState('')
  const [error, setError] = useState('')
  // An input hint (a malformed ID, caught before any request), shown beside the
  // field; `error` is reserved for a request that failed.
  const [hint, setHint] = useState('')
  // Why the switch did not turn on, shown under it. Kept as a kind and worded from the
  // current list at render, so a removal or a refetch never leaves a stale name there.
  const [switchHint, setSwitchHint] = useState<'' | 'first' | 'none' | 'pending' | 'changed'>('')
  // True while the list is read again after the confirm (see onToggle).
  const [rechecking, setRechecking] = useState(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  // Re-read on every visit and window focus: the owner may change access from
  // another window, and this section must not show a grant that has changed.
  // shortcut: no push from the server; add a refresh frame if that proves too slow.
  const { data, error: loadError } = useQuery({
    queryKey: SLACK_SHARED_ACCESS_KEY,
    queryFn: api.getSlackSharedAccess,
    refetchOnMount: 'always',
    refetchOnWindowFocus: 'always',
  })
  const refresh = () => qc.invalidateQueries({ queryKey: SLACK_SHARED_ACCESS_KEY })
  // A 409 not_applied: the change is saved but Slack still uses the earlier state, which
  // is what the switch and list show. Said under the switch, opening with that state
  // (`stateNow`), then why and what to do; other failures stay at the foot of the section.
  const [notApplied, setNotApplied] = useState<{ stateNow: string, detail: string } | null>(null)
  const fail = (fallback: string, stateNow: string) => (e: unknown) => {
    const code = refusalCode(e)
    if (code === 'not_applied') {
      setError('')
      // The server names the unread `slack` section; the notice says it in plain words.
      setNotApplied({
        stateNow,
        detail: i18nT(unreadNames(e).includes('slack')
          ? 'pages.settings.slackSharedAccess.not_applied_unread'
          : 'pages.settings.slackSharedAccess.not_applied'),
      })
    } else {
      setNotApplied(null)
      setError(REASON_CODES.has(code) ? reasonText(code) : fallback)
    }
    // A 409 means the file was saved but the gateway kept the old list: re-read
    // what the gateway reports rather than show the change as made.
    void refresh()
  }
  const done = () => { setError(''); setNotApplied(null); void refresh() }
  const switchMut = useMutation({
    mutationFn: (enabled: boolean) => api.setSlackSharedAccess(enabled),
    onSuccess: done,
    onError: (e, enabled) => fail(i18nT('pages.settings.slackSharedAccess.save_failed'),
      i18nT(enabled ? 'pages.settings.slackSharedAccess.not_applied_on' : 'pages.settings.slackSharedAccess.not_applied_off'))(e),
  })
  const addMut = useMutation({
    mutationFn: (id: string) => api.addSlackSharedUser(id),
    onSuccess: () => { setDraft(''); setSwitchHint(''); done() },
    onError: (e, id) => fail(i18nT('pages.settings.slackSharedAccess.save_failed'),
      i18nT('pages.settings.slackSharedAccess.not_applied_add', { name: id }))(e),
  })
  const removeMut = useMutation({
    mutationFn: (id: string) => api.removeSlackSharedUser(id),
    onSuccess: done,
    // By the name the row showed: after a 409 the re-read list still holds it.
    onError: (e, id) => fail(i18nT('pages.settings.slackSharedAccess.save_failed'),
      i18nT('pages.settings.slackSharedAccess.not_applied_remove', { name: data?.users?.find(u => u.user_id === id)?.name || id }))(e),
  })

  // No hand-off: this notice sits inside the Slack settings form, whose unsaved
  // fields a hand-off to the chat would unmount and lose.
  if (loadError) {
    // The inline notice draws no action row, so Try again sits beside it; it re-reads the section.
    return (
      <div className="flex flex-wrap items-center gap-2">
        <ErrorNotice message={i18nT('pages.settings.slackSharedAccess.load_failed')} variant="inline" />
        <Btn onClick={() => { void refresh() }}>{i18nT('components.errorBoundary.try_again')}</Btn>
      </div>
    )
  }
  if (!data) return null
  // A gateway older than this route answers it with some other body: show nothing.
  if (typeof data.enabled !== 'boolean') return null
  const users = Array.isArray(data.users) ? data.users : []
  // The owner's own ID on the list already has access: it is neither granted nor refused here.
  const others = users.filter(u => u.reason !== 'owner')
  const granted = users.filter(u => u.confirmed)
  const refused = others.filter(u => !u.confirmed)
  const ro = data.read_only !== false
  // An older gateway sends no flag and always checks with Slack.
  const unchecked = data.verify_with_slack === false
  const busy = rechecking || switchMut.isPending || addMut.isPending || removeMut.isPending
  const switchHintText = {
    '': '',
    // Each kind holds only while the list still says so (a removal or refetch ends it).
    first: others.length === 0 ? i18nT('pages.settings.slackSharedAccess.people_first') : '',
    none: others.length > 0 && granted.length === 0
      ? i18nT('pages.settings.slackSharedAccess.people_none_confirmed')
      : '',
    // Named from the list as it is now: empty once they are removed or confirmed.
    pending: refused.length > 0
      ? i18nT('pages.settings.slackSharedAccess.people_pending_slack', { names: fmtList(refused.map(u => u.name || u.user_id)) })
      : '',
    changed: i18nT('pages.settings.slackSharedAccess.people_changed'),
  }[switchHint]
  // "People with access" only while sharing is on: while off, nobody listed has it.
  const peopleLabel = i18nT(data.enabled
    ? 'pages.settings.slackSharedAccess.people_label'
    : 'pages.settings.slackSharedAccess.people_label_off')

  const onToggle = async (next: boolean) => {
    setSwitchHint('')
    if (next) {
      // The confirm names who gets access: only people Slack confirmed do.
      if (others.length === 0) { setSwitchHint('first'); return }
      if (granted.length === 0) { setSwitchHint('none'); return }
      // A UI guard: while anyone listed is unconfirmed, sharing does not turn on here, so
      // the confirm only ever names people who get access. The server still accepts the
      // switch (config.json and its routes are unchanged) and admits only confirmed IDs.
      if (refused.length > 0) { setSwitchHint('pending'); return }
      const shown = listKey(users)
      const ok = await confirm({
        title: i18nT('pages.settings.slackSharedAccess.confirm_title', { count: granted.length }),
        body: (
          <>
            <ul className="list-disc pl-4 text-[13px]" aria-label={i18nT('pages.settings.slackSharedAccess.people_label')}>
              {granted.map(u => <li key={u.user_id}>{u.name || u.user_id}</li>)}
            </ul>
            <SharedAccessWarning unchecked={unchecked} />
          </>
        ),
        confirmLabel: i18nT('pages.settings.slackSharedAccess.confirm_label', { count: granted.length }),
        danger: true,
      })
      if (!ok) return
      // The list may have changed while the confirm was open (another window, a focus
      // refetch still in flight): read it fresh from the server and send nothing unless
      // it is exactly the list the owner just confirmed. A failed read sends nothing: the
      // query's error shows the load-failed notice with Try again in place of the section.
      // While it reads, the switch, Add and Remove are busy, so no second confirm and no
      // list write can start under the answer being checked.
      let now: SlackSharedAccessData
      setRechecking(true)
      try {
        // A read already in flight (a focus refetch) may predate the click: drop it first.
        await qc.cancelQueries({ queryKey: SLACK_SHARED_ACCESS_KEY })
        now = await qc.fetchQuery({ queryKey: SLACK_SHARED_ACCESS_KEY, queryFn: api.getSlackSharedAccess, staleTime: 0 })
      } catch {
        return
      } finally {
        if (mounted.current) setRechecking(false)
      }
      // Left the page while it read: turn nothing on that the owner can no longer see.
      if (!mounted.current) return
      // Already on (another window turned it on): nothing to write, and the page now shows it.
      if (now.enabled === true) return
      if (listKey(Array.isArray(now.users) ? now.users : []) !== shown) { setSwitchHint('changed'); return }
    }
    switchMut.mutate(next)
  }
  const add = () => {
    const v = draft.trim()
    // Enter reaches here even while the buttons are disabled.
    if (!v || busy) return
    if (!isSlackUserId(v)) { setHint(reasonText('malformed_id')); return }
    addMut.mutate(v)
  }

  return (
    <div data-testid="slack-shared-access">
      <SettingsToggle
        label={i18nT('pages.settings.slackSharedAccess.toggle_label')}
        description={i18nT('pages.settings.slackSharedAccess.toggle_desc')}
        checked={data.enabled}
        onChange={v => { void onToggle(v) }}
        // Busy while its own change is checked or written: it keeps its tab stop and says so.
        disabled={ro || addMut.isPending || removeMut.isPending}
        busy={rechecking || switchMut.isPending}
        configKey="slack.shared_access.enabled"
        describedBy="slack-shared-access-warning"
      />
      {/* Said at the switch in both states, so a list under the always-on warning never reads as a live grant. */}
      <div className="text-[12px] text-text">{i18nT(!data.enabled
        ? 'pages.settings.slackSharedAccess.off_note'
        : unchecked
          ? 'pages.settings.slackSharedAccess.on_note_listed'
          : 'pages.settings.slackSharedAccess.on_note')}</div>
      {switchHintText && <div role="status" className="text-[12px] text-warn-text">{switchHintText}</div>}
      {notApplied && (
        /* No hand-off: the member-ID draft and the Slack settings form around it
           are unsaved; a hand-off to the chat unmounts this panel and loses them. */
        <ErrorNotice variant="inline" messagePlacement="below" title={notApplied.stateNow} message={notApplied.detail} />
      )}
      <SharedAccessWarning id="slack-shared-access-warning" unchecked={unchecked} />
      {/* A remote session changes nothing here, so the owner-only line has nothing to say. */}
      {!ro && <div className="text-[12px] text-muted mb-2">{i18nT('pages.settings.slackSharedAccess.owner_only')}</div>}
      {/* The label changes with the switch; a `key:` highlight link finds this list by its config key. */}
      <div data-setting-label={peopleLabel} data-setting-key="slack.shared_access.user_ids" className="flex flex-col gap-1.5 py-1.5">
        <span className="text-[13px] font-semibold text-text">{peopleLabel}</span>
        {users.length === 0
          ? <div className="text-[12px] text-muted">{i18nT('pages.settings.slackSharedAccess.people_empty')}</div>
          : (
            <ul className="divide-y divide-border">
              {users.map(u => (
                <SharedUserRow key={u.user_id} user={u} readOnly={ro} sharing={data.enabled} removing={busy} onRemove={id => removeMut.mutate(id)} />
              ))}
            </ul>
          )}
        {!ro && (
          <div className="flex items-center gap-2">
            <Input value={draft} placeholder={i18nT('pages.settings.slackPanel.u0123abc456')} className="min-w-0 flex-1 font-mono"
              aria-label={i18nT('pages.settings.slackSharedAccess.add_label')}
              aria-describedby={hint ? 'slack-shared-access-hint' : undefined}
              onChange={e => { setDraft(e.target.value); setError(''); setHint('') }}
              {...ime.bindEnter({ onEnter: add })} />
            <Btn onClick={add} disabled={!draft.trim() || busy} className="shrink-0"><Plus size={13} /> {i18nT('pages.settings.slackPanel.add')}</Btn>
          </div>
        )}
        {hint && <div id="slack-shared-access-hint" className="text-[12px] text-danger">{hint}</div>}
        {ro && <div className="text-[12px] text-muted">{i18nT('pages.settings.slackSharedAccess.read_only')}</div>}
        {/* No hand-off: the member-ID draft and the Slack settings form around it
            are unsaved; a hand-off to the chat unmounts this panel and loses them. */}
        <ErrorNotice message={error} variant="inline" />
      </div>
      {confirmDialog}
    </div>
  )
}
