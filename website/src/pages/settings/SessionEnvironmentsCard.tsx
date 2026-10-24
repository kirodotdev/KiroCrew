import { useId, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'
import { Check, ChevronRight, CircleDashed, ExternalLink, Lock, Minus, Plus, Trash2 } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import type { ReactNode } from 'react'

import { api, type SessionEnvironmentsCheck } from '../../api/client'
import { AGENT_SANDBOX_PATH } from '../overview/agentSandboxLink'
import { ACP_BACKEND_KAS, acpBackendName } from '../../api/acpBackend'
import { fmtDateTime } from '../../i18n/format'
import { useAppDispatch, useAppSelector } from '../../store'
import { isMemberThreadSlot, memberSlugOfSlot } from '../../hooks/unreadOnAttention'
import { resumeFromHistory } from '../../store/chatSlice'
import { reportForError } from '../../utils/errorReport'
import { safeHttpUrl } from '../../lib/safeUrl'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import StatusNotice from '../../components/StatusNotice'
import { SettingsCombobox, SettingsField, SettingsInput, SettingsSelect, SettingsToggle } from '../../components/settings'
import { Btn, PanelSectionHeader } from '../../components/ui'

interface EnvironmentProfile {
  id: string
  name: string
  template: string
  workdir: string
  /** False for a template whose stop destroys its files. Absent means true:
   * the card writes it only once toggled, so an older gateway never sees it. */
  compute_survives_stop?: boolean
}
/** A Kiro Cloud Sessions profile: the repositories its cloud sandbox opens. */
interface KiroCloudProfile {
  id: string
  name: string
  repositories: string[]
}
/** Mirrors KIRO_CLOUD_REPOSITORIES_MAX in config/session_environments.py. */
const KIRO_CLOUD_REPOSITORIES_MAX = 10
/** How often the card re-reads the workspace list. */
const WORKSPACES_POLL_MS = 15_000
type ProfileTextKey = 'id' | 'name' | 'template' | 'workdir'
interface EnvironmentSettings {
  coder_url: string
  profiles: EnvironmentProfile[]
  /** Kiro Cloud Sessions profiles. Older gateways omit it. */
  kiro_cloud_profiles?: KiroCloudProfile[]
  /** Profile id new chats start on; "" is this machine. Older gateways omit it. */
  default_profile?: string
  /** Stop every remote environment when the gateway shuts down. Older gateways omit it. */
  stop_on_shutdown?: boolean
}
/** A removed profile cannot stay the default (the gateway refuses that save),
 * so removing the default moves it to the first remaining profile of the same
 * provider, then of the other one, or to this machine when none is left. */
function withoutDefault(next: EnvironmentSettings, removedId: string, kind: 'coder' | 'cloud'): EnvironmentSettings {
  if (next.default_profile !== removedId) return next
  const coder = next.profiles
  const cloud = next.kiro_cloud_profiles ?? []
  const order = kind === 'cloud' ? [...cloud, ...coder] : [...coder, ...cloud]
  return { ...next, default_profile: order.find(p => p.id)?.id ?? '' }
}
/** Every profile a default can name: Coder and Kiro Cloud alike. */
const allProfiles = (s: EnvironmentSettings) => [...s.profiles, ...(s.kiro_cloud_profiles ?? [])]
/** The repositories field is edited as one comma-separated line; blanks left by
 * a trailing comma are dropped only when saving, so typing is not fought. */
function withCleanRepositories(next: EnvironmentSettings): EnvironmentSettings {
  if (!next.kiro_cloud_profiles) return next
  return {
    ...next,
    kiro_cloud_profiles: next.kiro_cloud_profiles.map(p => ({ ...p, repositories: p.repositories.filter(r => r) })),
  }
}
interface ConfigResponse {
  session_environments?: EnvironmentSettings
}

const SETUP_DOCS = 'https://coder.com/docs/admin/setup'
const URL_ERROR_ID = 'session-environments-url-error'

/** One saved profile as a disclosure; collapsing loses nothing (values live
 * in the parent draft). */
function ProfileDisclosure({ title, subtitle, open, onToggle, children }: {
  title: string
  subtitle?: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const bodyId = useId()
  return (
    <div className="min-w-0 rounded-lg border border-border">
      <Clickable onClick={onToggle} aria-expanded={open} aria-controls={bodyId}
        className="group flex min-w-0 items-center gap-2.5 rounded-lg px-3 py-2.5 cursor-pointer hover:bg-bg-hover/40 transition-colors">
        <motion.span className="flex shrink-0 items-center text-muted group-hover:text-text"
          animate={{ rotate: open ? 90 : 0 }} transition={{ type: 'spring', damping: 22, stiffness: 300 }}>
          <ChevronRight className="lucide-inline" aria-hidden="true" />
        </motion.span>
        <span className="truncate text-[13px] font-semibold text-text">{title}</span>
        {subtitle && <span className="truncate text-[12px] text-muted">{subtitle}</span>}
      </Clickable>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div key="body" id={bodyId}
            initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.7 }} style={{ overflow: 'hidden' }}>
            <fieldset aria-label={title} className="min-w-0 space-y-3 px-3 pb-3">{children}</fieldset>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/** How a check line's glyph reads. Only `ok` is coloured: a git host is
 * optional, so a signed-out or withheld one is never shown as a failure (the
 * blocking Coder and template failures render through ErrorNotice instead).
 * Each non-ok state has a glyph of its own; the text carries the meaning.
 * Signed-out is a dashed ring, a state rather than an arrow or button shape,
 * since the line itself is not something to click. */
type CheckLineStatus = 'ok' | 'signIn' | 'withheld' | 'unknown'
const CHECK_LINE_ICONS = { ok: Check, signIn: CircleDashed, withheld: Lock, unknown: Minus } as const

/** One check line: a status glyph and its text, announced as a list item. */
function CheckLine({ status, children }: { status: CheckLineStatus; children: ReactNode }) {
  const Icon = CHECK_LINE_ICONS[status]
  const tone = status === 'ok' ? 'text-ok' : 'text-muted'
  return (
    <li className="flex min-w-0 items-start gap-2 text-[13px] text-text">
      <Icon size={14} className={`mt-0.5 shrink-0 ${tone}`} aria-hidden="true" />
      <span className="min-w-0 break-words">{children}</span>
    </li>
  )
}

/** Display names for git hosts whose brand is a do-not-translate term; any
 * other host is shown as its host name. */
const GIT_HOST_NAMES: Record<string, string> = {
  'github.com': 'GitHub',
  'gitlab.com': 'GitLab',
  'bitbucket.org': 'Bitbucket',
  'dev.azure.com': 'Azure DevOps',
}

/** The gateway-machine command that signs each git host in. A host with no
 * CLI here (Bitbucket) falls back to git's credential helper and is reported
 * unchecked, never signed out. Commands, not prose: never translated. */
const GIT_SIGN_IN_COMMANDS: Record<string, string> = {
  'github.com': 'gh auth login',
  'gitlab.com': 'glab auth login',
  'dev.azure.com': 'az login',
}

/** What the readiness check found, in the order a person fixes things. */
function CheckResults({ result, profiles }: {
  result: SessionEnvironmentsCheck
  profiles: EnvironmentProfile[]
}) {
  const { t } = useTranslation()
  const names = new Map(profiles.map(p => [p.id, p]))
  // A gateway that predates the git rows sends no `git` at all.
  const git = Object.entries(result.git ?? {})
  // The sandbox tier lives on Developer → Config; link there only when it is
  // what withheld a login, since otherwise nothing here is the sandbox's doing.
  const withheld = git.some(([, status]) => status === 'withheld')
  // 'strict' withholds every host alike, so one line says it once instead of
  // repeating the same sentence per host.
  const allWithheld = withheld && git.every(([, status]) => status === 'withheld')
  const unsupported = git.length > 0 && git.every(([, status]) => status === 'unsupported')
  return (
    <>
    <ul aria-label={t('sessionEnvironments.checkResults')} className="space-y-1.5">
      {result.coder.ok ? (
        <CheckLine status="ok">{t('sessionEnvironments.checkCoderOk', { owner: result.coder.owner ?? '' })}</CheckLine>
      ) : (
        <li>
          <ErrorNotice variant="inline" message={t('sessionEnvironments.checkCoderFailed')} askAgent askAgentLabel={t('sessionEnvironments.askAgentFix')} />
          <p className="pl-5 text-[13px] text-text">
            <Trans i18nKey="sessionEnvironments.checkCoderLogin" components={{ code: <code className="font-mono select-all" /> }} />
          </p>
          {/* The CLI's own words, folded: the line above already says what to do. */}
          {result.coder.error && (
            <details className="pl-5 text-[12px] text-muted">
              <summary className="cursor-pointer">{t('sessionEnvironments.checkDetails')}</summary>
              <pre className="mt-1 whitespace-pre-wrap break-words font-mono">{result.coder.error}</pre>
            </details>
          )}
        </li>
      )}
      {/* No sign-in, no template check, whatever a row says. */}
      {!result.coder.ok ? profiles.map(p => (
        <li key={p.id} className="flex min-w-0 items-start gap-2 text-[13px] text-muted">
          <Minus size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          {t('sessionEnvironments.checkTemplateNotChecked', { name: p.name || p.id })}
        </li>
      )) : result.profiles.map(row => {
        const profile = names.get(row.id)
        const name = profile?.name || row.id
        return row.ok ? (
          <CheckLine key={row.id} status="ok">
            {t('sessionEnvironments.checkTemplateOk', { name, template: profile?.template ?? '' })}
          </CheckLine>
        ) : (
          <li key={row.id}><ErrorNotice variant="inline" message={t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })} askAgent askAgentLabel={t('sessionEnvironments.askAgentFix')} /></li>
        )
      })}
      {allWithheld && <CheckLine status="withheld">{t('sessionEnvironments.checkGitWithheldAll')}</CheckLine>}
      {/* A Windows gateway lends nothing, so one line says so; no sandbox link applies. */}
      {unsupported && <CheckLine status="withheld">{t('sessionEnvironments.checkGitUnsupported')}</CheckLine>}
      {!allWithheld && !unsupported && git.map(([host, status]) => {
        const name = GIT_HOST_NAMES[host] ?? host
        // Signed out names a sign-in command; a host with none reads as unchecked.
        const signedOut = status === 'not_signed_in' && !!GIT_SIGN_IN_COMMANDS[host]
        return (
          <CheckLine key={host} status={status === 'signed_in' ? 'ok' : signedOut ? 'signIn' : status === 'withheld' ? 'withheld' : 'unknown'}>
            {status === 'signed_in'
              ? t('sessionEnvironments.checkGitSignedIn', { name })
              : signedOut
                ? (
                  <Trans
                    i18nKey="sessionEnvironments.checkGitSignedOut"
                    values={{ name, command: GIT_SIGN_IN_COMMANDS[host] }}
                    components={{ code: <code className="rounded bg-bg-elevated px-1 font-mono text-[12px] text-text" translate="no" /> }}
                  />
                )
                : status === 'withheld'
                  ? t('sessionEnvironments.checkGitWithheld', { name })
                  : t('sessionEnvironments.checkGitUnchecked', { name })}
          </CheckLine>
        )
      })}
    </ul>
    {withheld && (
      <p className="text-[13px]">
        <Link to={AGENT_SANDBOX_PATH} className="inline-flex min-w-0 items-center gap-0.5 rounded text-accent hover:underline focus-ring">
          <span className="min-w-0">{t('sessionEnvironments.checkGitSandboxLink')}</span>
          <ChevronRight size={13} aria-hidden="true" className="shrink-0" />
        </Link>
      </p>
    )}
    </>
  )
}

/** The deletion time has passed and the sweep has not run yet (an unreadable date is not). */
function isOverdue(iso: string): boolean {
  return new Date(iso).getTime() <= Date.now()
}

/** Where a reopened chat opens. The chat page cannot show a mate's thread
 * (`isChatPageSurface`), so a thread resumed on the member surface opens its
 * crewmate on the Members page, or the bare page when the key names no slug. */
function reopenedHref(key: string, surface: string): string {
  if (surface !== 'member') return `/chat?sid=${encodeURIComponent(key)}`
  const slug = memberSlugOfSlot(key)
  return slug ? `/members?member=${encodeURIComponent(slug)}` : '/members'
}

/** Reopen an archived chat from history, the step that keeps its workspace.
 * Mounted only on an archived row, so the card needs no store otherwise. */
/** ``needsSaveId``: the list's one "save first" line, which a blocked button points at. */
function ReopenChat({ slot, blocked, needsSaveId }: { slot: string; blocked: boolean; needsSaveId: string }) {
  const { t } = useTranslation()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const reasonId = useId()
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  // Reopened, but edits began while it ran: leaving now would lose them.
  const [waiting, setWaiting] = useState(false)
  const blockedRef = useRef(blocked)
  blockedRef.current = blocked
  const reopen = async () => {
    setBusy(true)
    setFailed(false)
    setWaiting(false)
    try {
      // The row names the bare slot; resume reads the transcript under the
      // history key, so it gets the canonical ``dashboard:`` form.
      const resumed = await dispatch(resumeFromHistory({ key: `dashboard:${slot}`, title: slot })).unwrap()
      if (!resumed.ok) throw new Error('resume refused')
      if (blockedRef.current) {
        setWaiting(true)
        setBusy(false)
        return
      }
      navigate(reopenedHref(resumed.key, resumed.surface))
    } catch {
      setFailed(true)
      setBusy(false)
    }
  }
  return (
    <>
      <Btn type="button" className="h-6 px-2 text-[12px]" disabled={busy || blocked}
        aria-describedby={waiting ? reasonId : blocked ? needsSaveId : undefined} onClick={() => void reopen()}>
        {t('sessionEnvironments.reopenChat')}
      </Btn>
      {/* Leaving the card would lose the unsaved edits, so it waits for them;
        * the list says why once, above the rows. */}
      {waiting && <span id={reasonId} role="status" className="text-[12px] text-muted">{t('sessionEnvironments.reopenedWaiting')}</span>}
      {/* Offered only with no draft (see above), so the hand-off loses nothing. */}
      {failed && !blocked && <ErrorNotice variant="inline" className="w-full" askAgent askAgentLabel={t('sessionEnvironments.askAgentFix')} message={t('sessionEnvironments.reopenFailed')} onDismiss={() => setFailed(false)} />}
    </>
  )
}

/** The workspaces Kiro Crew owns, from the gateway's registry (no Coder call). */
/** ``handoff``: off while the card holds an unsaved draft, which leaving would lose. */
function WorkspaceList({ handoff }: { handoff: boolean }) {
  const { t } = useTranslation()
  const needsSaveId = useId()
  const slots = useAppSelector(s => s.dashboard.slots)
  // A mate's thread lives on the Members page, not the chat page, so its row
  // opens the page that shows it (the open slot's `agent` names the mate).
  const conversationHref = (slot: string) => {
    if (!isMemberThreadSlot(slot, slots)) return `/chat?sid=${encodeURIComponent(slot)}`
    const mate = slots.find(s => s.key === slot)?.agent
    return mate ? `/members?member=${encodeURIComponent(mate)}` : '/members'
  }
  const query = useQuery({
    queryKey: ['sessionEnvironmentsWorkspaces'],
    queryFn: () => api.sessionEnvironmentsWorkspaces(),
    refetchInterval: WORKSPACES_POLL_MS,
    retry: false,
  })
  if (query.isPending) return null
  if (query.isError) return <ErrorNotice askAgent={handoff} askAgentLabel={t('sessionEnvironments.askAgentFix')} message={t('sessionEnvironments.workspacesError')} />
  const rows = query.data.workspaces
  if (!rows.length) return <p className="text-[13px] text-muted">{t('sessionEnvironments.workspacesNone')}</p>
  // Said once for the list: every link and Reopen button waits on the same draft.
  const waitsOnDraft = !handoff && rows.some(row => row.slot && row.state !== 'deleting')
  return (
    <>
    {waitsOnDraft && <p id={needsSaveId} className="text-[12px] text-muted">{t('sessionEnvironments.reopenNeedsSave')}</p>}
    <ul aria-label={t('sessionEnvironments.workspaces')} className="divide-y divide-border rounded-lg border border-border">
      {rows.map(row => {
        // Only a live chat has a slot to open; an archived row says what to do.
        const inUse = row.state !== 'deleting' && !row.deletion_due_at
        const name = 'truncate font-mono text-[12px]'
        return (
        <li key={row.workspace} className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2 text-[13px]">
          {inUse && row.slot && handoff
            ? <Link to={conversationHref(row.slot)} className={`${name} text-accent hover:underline`}>{row.workspace}</Link>
            : <span className={`${name} text-text`}>{row.workspace}</span>}
          {inUse ? (
            // Opening the chat would leave the card and lose the draft, so the
            // name is plain text then and the line above the list says why.
            <span className="text-muted">{t('sessionEnvironments.workspaceSession')}</span>
          ) : (
            // Status and action as one right-aligned group, so the button sits
            // in the same place on every row however long the status is.
            <span className="ml-auto flex min-w-0 flex-wrap items-baseline justify-end gap-x-2 gap-y-1 text-right">
              <span className="text-[12px] text-muted">
                {row.state === 'deleting'
                  ? t('sessionEnvironments.workspaceDeleting')
                  : isOverdue(row.deletion_due_at)
                    ? t('sessionEnvironments.workspaceDeletesSoon', { when: fmtDateTime(row.deletion_due_at) })
                    : t('sessionEnvironments.workspaceDeletes', { when: fmtDateTime(row.deletion_due_at) })}
              </span>
              {/* Reopening is the first step of keeping the workspace; over a draft it waits. */}
              {row.state !== 'deleting' && row.slot && <ReopenChat slot={row.slot} blocked={!handoff} needsSaveId={needsSaveId} />}
            </span>
          )}
        </li>
        )
      })}
    </ul>
    </>
  )
}

/** Saving starts no compute. The preview switch is indexed in
 * FeaturePreviewsSection; the URL setting is searchable only after opting in. */
/** Mounted only behind the Session Environments preview (SettingsPage gates it). */
export function SessionEnvironmentsCard() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const controlId = useId()
  const query = useQuery<ConfigResponse>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  // Null follows fresh server data. Once edited, background refetches never
  // replace the draft. Saving freezes controls until the server responds.
  const [draft, setDraft] = useState<EnvironmentSettings | null>(null)
  const [saved, setSaved] = useState(false)
  // Removal has no confirm: the draft keeps it undoable until Save.
  const [removed, setRemoved] = useState(false)
  // Saved profiles start collapsed; a newly added one opens so it can be
  // filled in. Tracked by row index, so add/remove keep it aligned.
  const [openRows, setOpenRows] = useState<boolean[]>([])
  const toggleRow = (index: number) => setOpenRows(rows => {
    const next = [...rows]
    next[index] = !next[index]
    return next
  })
  const addRow = (index: number) => setOpenRows(rows => {
    const next = [...rows]
    next[index] = true
    return next
  })
  const removeRow = (index: number) => setOpenRows(rows => rows.filter((_, i) => i !== index))
  const [openCloudRows, setOpenCloudRows] = useState<boolean[]>([])
  const mutation = useMutation({
    mutationFn: (value: EnvironmentSettings) => api.patchConfig('session_environments', value) as Promise<ConfigResponse>,
    onMutate: () => qc.cancelQueries({ queryKey: ['kirocrewConfig'] }),
    onSuccess: response => {
      qc.setQueryData(['kirocrewConfig'], response)
      // Chats read their profile's folder from placement; a cached one would hand
      // new file drafts the old folder. Open drafts keep the folder they began in.
      void qc.invalidateQueries({ queryKey: ['slot-environment'] })
      setDraft(null)
      setSaved(true)
      setRemoved(false)
    },
  })
  const config = draft ?? query.data?.session_environments
  // The gateway's normalizer names the field it refused; mark the URL box too.
  const urlRefused = mutation.isError && mutation.error.message.startsWith('coder_url')
  const saveDetail = mutation.isError
    ? (mutation.error.message === 'coder_url must not include a query string'
      ? t('sessionEnvironments.urlQueryError')
      : mutation.error.message || t('pages.settings.instancesPanel.unknown_error'))
    : ''
  // Removing the default's profile moves the default to a remaining profile, or
  // turns it off when none is left: say which, beside the switch.
  const savedDefault = query.data?.session_environments?.default_profile
  const defaultGone = !!draft && removed && !!savedDefault
    && !allProfiles(draft).some(p => p.id === savedDefault)
  const defaultDropped = defaultGone && !draft?.default_profile
  const movedTo = defaultGone && draft?.default_profile
    ? (allProfiles(draft).find(p => p.id === draft.default_profile)?.name || draft.default_profile)
    : ''
  // The check reads the SAVED configuration on the gateway, so it is offered
  // only with no unsaved draft, and its answer is dropped when one appears.
  const savedCoderUrl = safeHttpUrl(query.data?.session_environments?.coder_url ?? '')
  const savedProfiles = query.data?.session_environments?.profiles ?? []
  const checkMutation = useMutation({ mutationFn: () => api.sessionEnvironmentsCheck() })
  // The saved deployment's templates fill the profile editor's template picker;
  // with none read (no saved URL, or Coder refused) the field stays free text.
  const templatesQuery = useQuery({
    queryKey: ['sessionEnvironmentsTemplates', savedCoderUrl],
    queryFn: () => api.sessionEnvironmentsTemplates(),
    enabled: !!savedCoderUrl,
    retry: false,
    staleTime: 60_000,
  })
  const templateNames = templatesQuery.data?.templates ?? []
  // The same list the Manage section polls (shared cache): which live chats a
  // removed profile would strand, said beside Save before it happens.
  const workspacesQuery = useQuery({
    queryKey: ['sessionEnvironmentsWorkspaces'],
    queryFn: () => api.sessionEnvironmentsWorkspaces(),
    enabled: !!savedCoderUrl,
    retry: false,
  })
  const removedIds = draft
    ? savedProfiles.map(p => p.id).filter(id => !draft.profiles.some(p => p.id === id))
    : []
  const strandedChats = (workspacesQuery.data?.workspaces ?? []).filter(row =>
    !!row.profile && removedIds.includes(row.profile) && !row.deletion_due_at && row.state !== 'deleting').length
  // Why the picker is missing, said under the free-text field. Either a transport
  // failure or the gateway's own answer that Coder refused the list.
  const templatesError = templatesQuery.isError
    ? (templatesQuery.error.message || t('pages.settings.instancesPanel.unknown_error'))
    : (templatesQuery.data?.error ?? '')
  const change = (next: EnvironmentSettings) => {
    setDraft(next)
    setSaved(false)
    mutation.reset()
    // The check read the saved settings; an edit makes its answer stale.
    checkMutation.reset()
  }

  if (query.isPending) return <p role="status" className="text-[13px] text-muted">{t('sessionEnvironments.loading')}</p>
  if (query.isError && !query.data) return (
    <ErrorNotice message={t('sessionEnvironments.loadError')} askAgent askAgentLabel={t('sessionEnvironments.askAgentFix')} footer={
      <Btn onClick={() => void query.refetch()}>{t('sessionEnvironments.retry')}</Btn>
    } />
  )
  // An older gateway has no section: never show a form that cannot save.
  if (!config) return <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent askAgentLabel={t('sessionEnvironments.askAgentFix')} />

  const profileFields: { key: ProfileTextKey; label: string; description?: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id'), description: t('sessionEnvironments.idHint') },
    { key: 'name', label: t('sessionEnvironments.name'), description: t('sessionEnvironments.nameHint') },
    { key: 'template', label: t('sessionEnvironments.template'), description: t('sessionEnvironments.templateHint') },
    { key: 'workdir', label: t('sessionEnvironments.workdir'), description: t('sessionEnvironments.workdirHint') },
  ]
  // Per-profile like the fields above, so not a searchable settings row.
  const survivesStopLabel = t('sessionEnvironments.computeSurvivesStop')
  // Renaming the default's id carries the default along: a dangling one is
  // refused on save. A rename that passes through an empty id keeps the old
  // default until the row has an id again, so retyping it does not drop it.
  // The default may name a Coder or a Kiro Cloud profile, so "orphaned" looks
  // at both lists.
  const defaultFollows = (old: string | undefined, value: string) => {
    const current = config.default_profile
    const orphaned = !!current && !allProfiles(config).some(profile => profile.id === current)
    return !!value && (old ? old === current : orphaned)
  }
  const updateProfile = (index: number, field: ProfileTextKey, value: string) => {
    const follows = field === 'id' && defaultFollows(config.profiles[index]?.id, value)
    change({
      ...config,
      ...(follows ? { default_profile: value } : {}),
      profiles: config.profiles.map((profile, i) => i === index ? { ...profile, [field]: value } : profile),
    })
  }
  const cloudProfiles = config.kiro_cloud_profiles
  // Per-record rows, like the Coder ones: not indexed as settings of their own.
  const cloudFields: { key: keyof KiroCloudProfile; label: string; description?: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    {
      key: 'repositories',
      label: t('sessionEnvironments.kiroCloud.repositories'),
      description: t('sessionEnvironments.kiroCloud.repositoriesHint', { max: KIRO_CLOUD_REPOSITORIES_MAX }),
    },
  ]
  const updateCloud = (index: number, patch: Partial<KiroCloudProfile>) => {
    const follows = patch.id !== undefined && defaultFollows(cloudProfiles?.[index]?.id, patch.id)
    change({
      ...config,
      ...(follows ? { default_profile: patch.id } : {}),
      kiro_cloud_profiles: (cloudProfiles ?? []).map((p, i) => i === index ? { ...p, ...patch } : p),
    })
  }
  const addCloudProfile = () => {
    const taken = new Set([...config.profiles, ...(cloudProfiles ?? [])].map(p => p.id))
    let n = 1
    while (taken.has(`kiro-cloud-${n}`)) n++
    setOpenCloudRows(rows => { const next = [...rows]; next[(cloudProfiles ?? []).length] = true; return next })
    change({ ...config, kiro_cloud_profiles: [...(cloudProfiles ?? []), { id: `kiro-cloud-${n}`, name: '', repositories: [] }] })
  }
  const addProfile = () => {
    let n = 1
    while (config.profiles.some(profile => profile.id === `profile-${n}`)) n++
    addRow(config.profiles.length)
    change({ ...config, profiles: [...config.profiles, { id: `profile-${n}`, name: '', template: '', workdir: '' }] })
  }

  return (
    <form className="min-w-0 space-y-3 pt-3" onSubmit={event => {
      event.preventDefault()
      if (draft && !mutation.isPending) mutation.mutate(withCleanRepositories(draft))
    }}>
      {/* The two words the page uses, defined once where the reader starts. */}
      <p className="text-[13px] text-muted">{t('sessionEnvironments.intro')}</p>
      {/* No hand-off: navigating would discard the unsaved environment draft. */}
      <ErrorNotice message={query.isError ? t('sessionEnvironments.loadError') : null} footer={
        <Btn type="button" onClick={() => void query.refetch()}>{t('sessionEnvironments.retry')}</Btn>
      } />
      <section aria-label={t('sessionEnvironments.configureStep')} className="space-y-3 pt-3">
      <PanelSectionHeader label={t('sessionEnvironments.configureStep')} />
      <details className="rounded-lg border border-border p-3">
        <summary className="cursor-pointer text-[13px] font-semibold text-text">{t('sessionEnvironments.setup')}</summary>
        <ol className="list-decimal pl-5 space-y-2 pt-3 text-[13px] text-muted">
          <li>{t('sessionEnvironments.coderSetup1')}</li>
          <li>{t('sessionEnvironments.coderSetup2')}</li>
          <li>{t('sessionEnvironments.coderSetup3')}</li>
        </ol>
        <a className="inline-block pt-3 text-[13px] text-accent underline" target="_blank" rel="noopener noreferrer"
          href={SETUP_DOCS}>
          {t('sessionEnvironments.setupDocs')}
        </a>
      </details>
      <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
        <SettingsInput
          configKey="session_environments.coder_url"
          label={t('sessionEnvironments.url')}
          description={t('sessionEnvironments.urlHint')}
          value={config.coder_url}
          onChange={value => change({ ...config, coder_url: value })}
          invalidMessageId={urlRefused ? URL_ERROR_ID : undefined}
        />
        {/* No hand-off: leaving the page would discard the unsaved draft. */}
        {urlRefused && <ErrorNotice id={URL_ERROR_ID} variant="inline" className="-mt-2" message={saveDetail} />}
        <SettingsField configKey="session_environments.profiles" label={t('sessionEnvironments.profiles')}
          description={t('sessionEnvironments.profilesCoderHint')}>
          <div className="min-w-0 space-y-3">
            {config.profiles.map((profile, index) => (
              // Index stays stable while the id is edited; removal is explicit.
              <ProfileDisclosure key={index} title={profile.name || profile.id} subtitle={profile.template}
                open={!!openRows[index]} onToggle={() => toggleRow(index)}>
                {profileFields.map(field => field.key !== 'template' ? (
                  <SettingsInput key={field.key} label={field.label} description={field.description} value={profile[field.key]} onChange={value => updateProfile(index, field.key, value)} />
                ) : (
                  <div key={field.key} className="min-w-0 space-y-1">
                    {templateNames.length > 0 ? (
                      <SettingsCombobox label={field.label} description={t('sessionEnvironments.templatePickHint')}
                        value={profile.template}
                        options={templateNames.map(name => ({ value: name, label: name }))}
                        onChange={value => updateProfile(index, 'template', value)}
                        triggerFallback={profile.template || t('sessionEnvironments.templateChoose')}
                        searchPlaceholder={t('sessionEnvironments.templateSearch')}
                        customValueOption={typed => ({ label: t('sessionEnvironments.templateUseTyped', { value: typed }) })} />
                    ) : (
                      <SettingsInput label={field.label} description={field.description} value={profile.template} onChange={value => updateProfile(index, 'template', value)} />
                    )}
                    {/* Said whichever field shows, so a failed refresh behind cached
                        choices is not hidden. No hand-off: the card may hold an
                        unsaved draft. Typing the name still works. */}
                    {templatesError && <ErrorNotice variant="inline" message={t('sessionEnvironments.templatesUnavailable', { reason: templatesError })} />}
                  </div>
                ))}
                <SettingsToggle anchor={false} label={survivesStopLabel}
                  description={t('sessionEnvironments.computeSurvivesStopHint')}
                  checked={profile.compute_survives_stop !== false}
                  onChange={on => change({ ...config, profiles: config.profiles.map((p, i) => i === index ? { ...p, compute_survives_stop: on } : p) })} />
                {/* The case people hesitate over, in its own words. */}
                <p className="-mt-2 text-[12px] font-medium text-text">{t('sessionEnvironments.computeSurvivesStopMicroVm')}</p>
                <Btn type="button" onClick={() => {
                  removeRow(index)
                  setRemoved(true)
                  change(withoutDefault({ ...config, profiles: config.profiles.filter((_, i) => i !== index) }, profile.id, 'coder'))
                }}>
                  <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                </Btn>
                <span className="ml-2 text-[12px] text-muted">{t('sessionEnvironments.removeHint')}</span>
              </ProfileDisclosure>
            ))}
            <Btn type="button" disabled={config.profiles.length >= 20} onClick={addProfile}>
              <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
            </Btn>
          </div>
        </SettingsField>
      </fieldset>
      </section>
      <section aria-label={t('sessionEnvironments.verifyStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.verifyStep')} />
        <p id={`${controlId}-verification`} className="text-[13px] text-muted">
          {draft ? t('sessionEnvironments.checkSaveFirst') : savedCoderUrl ? t('sessionEnvironments.verifyHint') : t('sessionEnvironments.checkNeedsUrl')}
        </p>
        <Btn type="button" disabled={!!draft || !savedCoderUrl || checkMutation.isPending}
          aria-describedby={`${controlId}-verification`} onClick={() => checkMutation.mutate()}>
          {checkMutation.isPending ? t('sessionEnvironments.checkRunning') : t('sessionEnvironments.check')}
        </Btn>
        {checkMutation.data && !draft && <CheckResults result={checkMutation.data} profiles={savedProfiles} />}
        {/* No hand-off while a draft exists: leaving the page would discard it. */}
        <ErrorNotice askAgent={!draft} askAgentLabel={t('sessionEnvironments.askAgentFix')} message={checkMutation.isError ? t('sessionEnvironments.checkError') : null} />
      </section>
      <section aria-label={t('sessionEnvironments.manageStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.manageStep')} />
        <p className="text-[13px] text-muted">{t('sessionEnvironments.managementHint')}</p>
        {savedCoderUrl && <WorkspaceList handoff={!draft} />}
        {savedCoderUrl && (
          <a href={savedCoderUrl} target="_blank" rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline">
            {t('sessionEnvironments.openCoder')}<ExternalLink size={12} className="lucide-inline" aria-hidden="true" />
          </a>
        )}
      </section>
      {/* Global: these apply to every profile. They follow the numbered
        * steps, which a first setup walks in order. Older gateways omit
        * either key, and then its control stays hidden. */}
      {(config.default_profile !== undefined || config.stop_on_shutdown !== undefined) && (() => {
        const all = [...config.profiles, ...(config.kiro_cloud_profiles ?? [])].filter(p => p.id)
        const defaultOn = !!config.default_profile
        return (
          <section aria-label={t('sessionEnvironments.globalDefaults')} className="space-y-3 pt-3">
            <PanelSectionHeader label={t('sessionEnvironments.globalDefaults')} />
            <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
              {config.default_profile !== undefined && <>
                <SettingsToggle configKey="session_environments.default_profile"
                  label={t('sessionEnvironments.defaultEnabled')}
                  description={defaultDropped
                    ? t('sessionEnvironments.defaultDropped')
                    : movedTo
                      ? t('sessionEnvironments.defaultMoved', { name: movedTo })
                      : all.length ? t('sessionEnvironments.defaultEnabledHint') : t('sessionEnvironments.defaultEnabledNoProfiles')}
                  checked={defaultOn} disabled={!all.length && !defaultOn}
                  onChange={on => change({ ...config, default_profile: on ? all[0]?.id ?? '' : '' })} />
                {defaultOn && (
                  <SettingsSelect label={t('sessionEnvironments.defaultProfile')}
                    description={t('sessionEnvironments.defaultProfileHint')}
                    value={config.default_profile ?? ''}
                    options={all.map(p => p.id)}
                    optionLabels={all.map(p => p.name || p.id)}
                    // Never ''. The picker reports '' when its value briefly
                    // leaves the options (a rename); that is not a choice.
                    onChange={value => { if (value) change({ ...config, default_profile: value }) }} />
                )}
              </>}
              {config.stop_on_shutdown !== undefined && (
                <SettingsToggle configKey="session_environments.stop_on_shutdown"
                  label={t('sessionEnvironments.stopOnShutdown')}
                  description={all.length ? t('sessionEnvironments.stopOnShutdownHint') : t('sessionEnvironments.defaultEnabledNoProfiles')}
                  checked={!!all.length && config.stop_on_shutdown} disabled={!all.length}
                  onChange={on => change({ ...config, stop_on_shutdown: on })} />
              )}
            </fieldset>
          </section>
        )
      })()}
      {/* Its own section, outside Coder's numbered steps: none of them applies. */}
      {cloudProfiles !== undefined && (
        <section aria-label={t('sessionEnvironments.kiroCloud.title')} className="space-y-3 pt-3">
          <PanelSectionHeader label={t('sessionEnvironments.kiroCloud.title')} />
          {/* What opting in gives up, as a warning in plain view rather than folded away. */}
          <StatusNotice tone="warn" message={t('sessionEnvironments.kiroCloud.limits')} />
          <details className="rounded-lg border border-border p-3">
            <summary className="cursor-pointer text-[13px] font-semibold text-text">{t('sessionEnvironments.kiroCloud.setup')}</summary>
            <ol className="list-decimal pl-5 space-y-2 pt-3 text-[13px] text-muted">
              {/* The backend by the label its picker shows, so the step matches the screen. */}
              <li>{t('sessionEnvironments.kiroCloud.setup1', { tab: t('settings.tabs.agent.label'), backend: acpBackendName({ id: ACP_BACKEND_KAS }) })}</li>
              <li>{t('sessionEnvironments.kiroCloud.setup2')}</li>
            </ol>
          </details>
          <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
            <SettingsField configKey="session_environments.kiro_cloud_profiles" label={t('sessionEnvironments.kiroCloud.profiles')}
              description={t('sessionEnvironments.kiroCloud.profilesHint')}>
              <div className="min-w-0 space-y-3">
                {/* The readiness check above probes Coder and git hosts only. */}
                <p className="text-[12px] text-muted">{t('sessionEnvironments.kiroCloud.notVerified')}</p>
                {cloudProfiles.map((profile, index) => (
                  <ProfileDisclosure key={index} title={profile.name || profile.id} subtitle={profile.repositories.filter(r => r).join(', ')}
                    open={!!openCloudRows[index]} onToggle={() => setOpenCloudRows(rows => { const next = [...rows]; next[index] = !next[index]; return next })}>
                    {cloudFields.map(field => (
                      <SettingsInput key={field.key} label={field.label} description={field.description}
                        value={field.key === 'repositories' ? profile.repositories.join(', ') : profile[field.key]}
                        onChange={value => updateCloud(index, field.key === 'repositories'
                          ? { repositories: value.split(',').map(r => r.trim()) }
                          : { [field.key]: value })} />
                    ))}
                    <Btn type="button" onClick={() => {
                      setOpenCloudRows(rows => rows.filter((_, i) => i !== index))
                      setRemoved(true)
                      change(withoutDefault({ ...config, kiro_cloud_profiles: cloudProfiles.filter((_, i) => i !== index) }, profile.id, 'cloud'))
                    }}>
                      <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.kiroCloud.remove')}
                    </Btn>
                  </ProfileDisclosure>
                ))}
                <Btn type="button" disabled={cloudProfiles.length >= 20} onClick={addCloudProfile}>
                  <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.kiroCloud.add')}
                </Btn>
              </div>
            </SettingsField>
          </fieldset>
        </section>
      )}
      {/* Last, after the defaults, and stuck to the bottom of the panel while
        * there are unsaved edits, so Save stays in view from every section. */}
      <div data-testid="session-environments-save-bar"
        className={`flex flex-wrap items-center gap-2 ${draft ? 'sticky bottom-0 z-10 -mx-1 border-t border-border bg-bg px-1 py-2' : ''}`}>
        <Btn type="submit" primary={!!draft} disabled={!draft || mutation.isPending}>{t('sessionEnvironments.save')}</Btn>
        <Btn type="button" disabled={!draft || mutation.isPending} onClick={() => {
          setDraft(null)
          setSaved(false)
          setRemoved(false)
          setOpenRows([])
          setOpenCloudRows([])
          mutation.reset()
        }}>{t('sessionEnvironments.discard')}</Btn>
        {(saved || (removed && draft)) && <span role="status" className="text-[13px] text-muted">
          {saved ? t('sessionEnvironments.saved') : t('sessionEnvironments.removed')}</span>}
      </div>
      {/* What saving does to chats still on a removed profile, before it is saved. */}
      {strandedChats > 0 && <p role="status" className="text-[13px] font-medium text-text">
        {t('sessionEnvironments.removedInUse', { count: strandedChats, ids: removedIds.join(', ') })}</p>}
      {/* No hand-off: navigating would discard the unsaved environment draft.
        * The server's detail names the field it refused; a refused URL's detail
        * already sits under its box, so the banner only points there. */}
      <ErrorNotice report={reportForError(mutation.error)} message={mutation.isError
        ? (urlRefused ? t('sessionEnvironments.saveErrorUrl') : t('sessionEnvironments.saveError', { detail: saveDetail }))
        : null} />
    </form>
  )
}
