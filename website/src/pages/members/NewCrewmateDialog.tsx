/**
 * "New crewmate" — the create dialog the Crewmates page opens from its header
 * "+" and its empty-state hero.
 *
 * A crewmate IS a crew record, so this posts to the same `POST /api/agents`
 * the crew manager's create form uses; the two stay one write path with two
 * front doors. The difference is what the user is asked first: a name, what
 * it is built from, and — in plain words — what it looks after. Everything
 * the crew manager's form also asks (workspace, model, routing triggers,
 * session colour) sits behind an "Advanced" disclosure, rendered by the SAME
 * `Field` frame and field components the editor mounts, so the two forms
 * cannot drift.
 *
 * "Built from" lists the installed kiro agents (the templates a crew can
 * boot), never the configured default CREW: a crew named `default` is an
 * alias, and storing its name as `kiro_agent` would make the new crewmate run
 * a fallback instead of that crew's template. The built-in `kirocrew` agent
 * leads the list and is labelled as the default.
 *
 * "What it looks after" is stored as the crew record's `description`: the
 * one free-text field the record already carries for a human-readable
 * account of the crew, and the line the crewmate's first greeting is seeded
 * from (see MembersPage). Memory is provisioned by the server on create
 * (a private store per crewmate, never a choice here), and the avatar is
 * edited on the detail page afterwards — the same split the editor's create
 * form has.
 *
 * Kept mounted and driven by `open` (Modal's own contract): `Modal` renders
 * nothing while closed, and the form state below is reset on every open so a
 * dismissed draft does not reappear.
 */
import { useEffect, useRef, useState, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronRight } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import Modal from '../../components/Modal'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Input } from '../../components/ui'
import { api } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { usePublishNavigationStake, useRegisterNavigationLeaveGuard } from '../../components/NavigationLeaveGuard'
// From the side-effect-free module, not `api/client`: test doubles of the
// client mock only `api`, and an `instanceof` against an undefined import
// throws instead of falling through to the generic message.
import { ApiError } from '../../api/apiError'
import { isValidCrewmateName } from '../../components/MeetCrewmatesFlow'
import { parseErrorCode } from '../../utils/errorReport'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import {
  Field,
  INHERIT_MODEL,
  ModelField,
  SessionColorField,
  TriggersField,
  WorkspaceField,
  WorkspaceModal,
} from '../KiroCrewAgentsPage'

/** The built-in kiro agent every install ships; the list's default entry. */
const BUILTIN_AGENT = 'kirocrew'

/**
 * The form's DOM id. `Modal` renders its footer OUTSIDE the form element, so
 * the Create button is associated by `form=` rather than by nesting: that is
 * what makes it the form's submit button, and a form with a submit button is
 * what gives Enter in the Name field its implicit submission (with two text
 * fields and no submit button, Enter does nothing).
 */
const FORM_ID = 'crewmate-create-form'

/** Longest the create waits for the registry/config caches to re-read before
 *  handing over anyway (see `createMut.onSuccess`). */
const CACHE_WARM_BOUND_MS = 2500
/** Longest a create with no server answer waits for the roster read that
 *  reconciles it (see `createMut.onError`). Past it the roster counts as
 *  unreadable — the same `null` an errored read yields — so the dialog says
 *  "unconfirmed" and unlocks instead of sitting on "Creating…". */
const RECONCILE_BOUND_MS = 2500

/** What the page needs to open the new crewmate's chat and seed its greeting. */
export interface CreatedCrewmate {
  /** Exact crew name — MembersPage's `?member=` resolves by name. */
  name: string
  /** The "what it looks after" line as typed; '' when left blank. */
  job: string
}

/** The `POST /api/agents` body — the crew manager's create payload plus the
 *  record's `description`, which carries "what it looks after". `model` is
 *  sent only when pinned: the inherit spelling is the server's default. */
interface CreateBody {
  name: string
  kiro_agent: string
  workspace: string
  memory_store: string
  description: string
  triggers: string
  session_color: string
  model?: string
}

export default function NewCrewmateDialog({ open, onClose, onCreated, existingNames }: {
  open: boolean
  onClose: () => void
  /** Fired once the server has the record; the page takes it from there. */
  onCreated: (created: CreatedCrewmate) => void
  /**
   * The roster's names as the page last read them. A name already here is
   * refused before any request (the server would answer `agent_exists`), and
   * it is what makes the post-failure reconcile below sound: a row found
   * AFTER a dropped request proves this request committed only if the name
   * was absent BEFORE it.
   */
  existingNames: readonly string[]
}) {
  const { t } = useTranslation()
  const reduceMotion = useReducedMotion()

  const [name, setName] = useState('')
  const [builtFrom, setBuiltFrom] = useState('')
  const [job, setJob] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [workspace, setWorkspace] = useState('default')
  const [pendingWorkspace, setPendingWorkspace] = useState<string | null>(null)
  const [model, setModel] = useState(INHERIT_MODEL)
  const [triggers, setTriggers] = useState('')
  const [sessionColor, setSessionColor] = useState('')
  const [wsModalOpen, setWsModalOpen] = useState(false)
  // The nested New workspace form's unsaved input. Counted into this dialog's
  // navigation stake: a route change unmounts both, and the workspace draft is
  // as lost as the crewmate's. `WorkspaceModal` stays mounted across closes
  // (see its note on Radix layers), so this is read live, not on open.
  const [wsDirty, setWsDirty] = useState(false)
  const queryClient = useQueryClient()
  // Client-side validation ("name is required") is kept apart from a request
  // that FAILED: a blank name never left the browser, so it is not an error.
  const [hint, setHint] = useState('')
  // The server's own "already exists" (a 409 the roster did not predict):
  // said through the request-error notice like every server answer, AND
  // marked on the Name field, since the field is what the answer is about.
  const [nameRefused, setNameRefused] = useState(false)
  const [error, setError] = useState('')
  // The one failure that is NOT "nothing was created": the request got no
  // answer and the roster could not be read to reconcile it. It is rendered
  // through the same notice as every other failure, but under its own lead
  // and test id, because its next step differs — check the list, do not
  // simply resubmit — and a notice that reads like the retry-safe ones
  // invites the second POST that creates a namesake.
  const [unconfirmed, setUnconfirmed] = useState(false)

  // Every open starts blank: a dismissed draft must not come back.
  useEffect(() => {
    if (!open) return
    setName(''); setBuiltFrom(''); setJob(''); setAdvanced(false)
    setWorkspace('default'); setModel(INHERIT_MODEL); setTriggers(''); setSessionColor('')
    setHint(''); setError(''); setNameRefused(false); setUnconfirmed(false); setPendingWorkspace(null)
    // The nested workspace form too: a draft left in it belongs to the
    // dismissed open, and `atStake` must not count it against the next one.
    setWsModalOpen(false); setWsDirty(false)
  }, [open])

  // Option lists come from the same reads the crew editor uses, fetched only
  // while the dialog is open. Each falls back to its built-in default when
  // the read fails, and the failure is SAID: one notice, first failure wins,
  // so a shortened list never passes for the whole set of choices.
  // The same catalog the chat picker reads (`GET /api/agents/catalog`): its
  // template rows already exclude the runtime's background-only spec, fork
  // copies and masked names, so the dialog does not keep a second copy of
  // that rule. No session key: this page has no chat slot, so the catalog is
  // the global one — a crewmate is a global record and must not be built
  // from a template only one project checkout can resolve.
  // Re-read on every open. The app's queries never go stale on their own
  // (queryClient.ts: freshness is WebSocket-driven), but no server event
  // invalidates this key, so under the default a template installed or
  // removed mid-session would stay frozen in the list from the first open
  // until a reload. `staleTime: 0` makes each `enabled` flip (each open)
  // fetch again: the list a user sees is the catalog as of opening the dialog.
  const { data: catalog, error: installedError } = useQuery({
    queryKey: ['agents-catalog', 'global'],
    queryFn: () => api.agentCatalog(),
    enabled: open,
    staleTime: 0,
  })
  const { data: workspacesData, refetch: refetchWorkspaces, error: workspacesError } = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => api.workspaces(),
    enabled: open,
  })
  const { data: availableModels, error: modelsError } = useAvailableModelsQuery({ enabled: open })
  const optionsError = installedError ?? workspacesError ?? modelsError

  // Installed kiro agents only (see the header comment). Private fork copies
  // (one crew's own definition) are not offered — a copy named after crew A
  // means nothing in crew B's list. The built-in agent leads, labelled as the
  // default; it is offered even when the installed read failed, because it
  // ships with every install.
  const installed = Array.isArray(catalog?.agents)
    ? catalog.agents
      .filter((row) => row.selection_kind === 'template' && Boolean(row.name))
      .map((row) => row.name)
      .filter((n: string) => n !== BUILTIN_AGENT)
    : []
  const builtFromOptions = [BUILTIN_AGENT, ...installed]
  const builtFromLabels = builtFromOptions.map((n) =>
    n === BUILTIN_AGENT ? t('pages.membersPage.built_from_default', { agent: n }) : n,
  )
  const builtFromValue = builtFrom || BUILTIN_AGENT
  const workspaceOptions = useMemo(
    () => workspacesData?.workspaces?.map((w: { name: string }) => w.name) || ['default'],
    [workspacesData],
  )
  // A workspace created from Advanced is picked only once its option is on
  // the list: Radix's hidden form <select> gathers its options a commit after
  // the items mount, so writing the value in the same commit as the new option
  // reads back as '' and clears the field. Reset-on-open clears the pending
  // pick, so a dismissed-and-reopened dialog never receives it.
  useEffect(() => {
    if (pendingWorkspace && workspaceOptions.includes(pendingWorkspace)) {
      setWorkspace(pendingWorkspace)
      setPendingWorkspace(null)
    }
  }, [pendingWorkspace, workspaceOptions])
  const modelOptions = [
    INHERIT_MODEL,
    ...(availableModels || []).map((m) => m.name).filter((n) => n && n !== INHERIT_MODEL),
  ]

  // Every editable value counts as a draft, not only the two text fields: a
  // template or an Advanced pick is as lost on an accidental dismissal as a
  // typed name, and the reset-on-open above means there is no way back.
  const dirty = Boolean(
    name || job || builtFrom || workspace !== 'default' || model !== INHERIT_MODEL || triggers || sessionColor,
  )

  // The mutation callbacks below outlive the dialog. A route change the user
  // confirmed through `create_leave_busy` unmounts the page, but React Query
  // still runs `onSuccess` when the POST resolves, and `onCreated` →
  // `openMember` → `setSearchParams` would then `navigate` the page they
  // chose back to `/members?member=<new name>` — the opposite of what the
  // confirm's own text promised. `useMutation` cancels nothing on unmount,
  // so every post-await continuation checks this ref before touching the
  // page. (The ref, not `open`: the dialog stays mounted while closed.)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const createMut = useMutation({
    mutationFn: (body: CreateBody) => api.createKirocrewAgent(body) as Promise<{ error?: string }>,
    onSuccess: async (r, body) => {
      // A 2xx whose body still carries `error` is a refusal in the server's
      // words; like every other failure it is said in the product's.
      if (r?.error) { setError(t('pages.membersPage.create_failed')); return }
      // What the crew manager's own create form does (`refetchAgents`): the
      // page re-reads the roster leaf itself, but the registry and config
      // caches are held at `staleTime: Infinity`, and `POST /api/agents`
      // pushes no refresh frame. Left as a pre-write snapshot, the header
      // pencil's deep link (`?crew=<name>`) finds no such agent in the crew
      // manager and silently drops the editor. `exact`: the roster leaf under
      // this prefix is re-read by the page in its own order (openCreated), and
      // a second concurrent read here would race that one. Awaited, with the
      // inactive queries refetched too (`refetchType: 'all'`): an invalidated
      // query still serves its old data until the refetch lands, and the crew
      // manager mounting in that window would read the pre-write list. But
      // BOUNDED: the crewmate exists server-side the moment the POST resolved,
      // and every dismissal path is refused while the mutation is pending, so
      // a cache warm-up that stalls (a 429 ladder, a half-open socket) must
      // not hold the dialog on "Creating…" with no exit. Past the bound the
      // refetches keep going in the background and the create proceeds.
      const warm = Promise.all([
        queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'], exact: true, refetchType: 'all' }),
        queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'], refetchType: 'all' }),
      ])
      await Promise.race([warm, new Promise<void>((resolve) => setTimeout(resolve, CACHE_WARM_BOUND_MS))])
      if (!mounted.current) return
      onCreated({ name: body.name, job: body.description })
    },
    onError: async (e: Error, body) => {
      if (e instanceof ApiError) {
        const code = parseErrorCode(e.body)
        if (e.status === 409 && code === 'agent_exists') {
          // The server has just proved the roster behind this dialog is
          // stale (the name got past `existingNames`): refresh it, so the row
          // shows and the next attempt is refused here, without a request.
          void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
          setError(t('pages.membersPage.create_name_taken', { name: body.name }))
          setNameRefused(true)
          return
        }
        // A name the server refuses on sight (it looks like a credential, or a
        // URL carrying one) is a request the server answered, so — like the 409
        // above — it is said in the ErrorNotice (the repo's one error surface),
        // with its cause, and the Name field is marked as the thing to change.
        // Collapsing it to "Nothing was created — try again" would invite the
        // same name forever with the cause hidden. The server deliberately does
        // not echo such a name, and neither does the notice.
        if (e.status === 400 && code === 'credential_shaped_name') {
          setError(t('pages.membersPage.create_name_credential_shaped'))
          setNameRefused(true)
          return
        }
        // Verbatim server text is reserved for the codes the dialog knows
        // (above); every other answer — a 5xx, a refused body — is said in the
        // product's words with its next step, never as the server's raw
        // sentence. The server answered, so nothing was created.
        setError(t('pages.membersPage.create_failed'))
        return
      }
      // No server answer at all (a dropped connection, a parse error): the
      // request may still have reached the server and been committed. The
      // dialog reads the roster to say something TRUE, but never claims the
      // create as its own: a row of this name proves only that the name now
      // exists — another tab could have created it in the same window — so
      // there is no request-correlated confirmation to open a chat and seed a
      // greeting on. The row is reported as what a retry would meet (taken),
      // the roster behind the dialog is refreshed so the row shows, and the
      // user picks it from the list; nothing is sent to it. The mutation
      // stays pending until this settles, so the form stays locked meanwhile
      // — which is why the read is BOUNDED like the cache warm-up above: while
      // pending every exit (X, Escape, backdrop, Cancel) is refused, and a
      // roster read that stalls must not hold the dialog on "Creating…" with
      // no way out. Past the bound the roster counts as unreadable.
      const present = await Promise.race([
        api.members()
          .then((r) => r.members.some((m) => m.name === body.name))
          // A rejected read and a body without a roster (the handler above
          // throwing) are both "unreadable": `.then(ok, fail)` would let the
          // handler's own throw escape past `fail` and leave nothing said.
          .catch((): null => null),
        new Promise<null>((resolve) => setTimeout(() => resolve(null), RECONCILE_BOUND_MS)),
      ])
      if (!mounted.current) return
      if (present) {
        void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
        setError(t('pages.membersPage.create_name_taken', { name: body.name }))
        setNameRefused(true)
        return
      }
      // `null`: the roster could not be read either, so whether the create
      // landed is unknown — "nothing was created" would be a guess.
      if (present === null) {
        // The roster behind the dialog is asked to re-read now, not on the
        // next attempt: if the create DID land, the row arrives in
        // `existingNames` and a resubmit of the same name is refused up front
        // as taken (no request), instead of reaching the server as a second
        // create that a stale roster could not predict.
        void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
        setUnconfirmed(true)
        setError(t('pages.membersPage.create_unconfirmed'))
        return
      }
      setError(t('pages.membersPage.create_failed'))
    },
  })
  const busy = createMut.isPending

  // The modal's own guards (`guardAccidentalDismiss`, `dismissDisabled`) cover
  // Escape, the backdrop and the X. A client-side route change — the sidebar,
  // the command palette, the browser's Back — unmounts the whole page and this
  // dialog with it, and none of those see the modal. So the same stake is
  // published to the app shell: a typed draft asks before leaving, and a POST
  // in flight asks too, since leaving loses the answer (the crewmate may be
  // created, but its chat will not open here).
  const atStake = open && (dirty || busy || (wsModalOpen && wsDirty))
  useRegisterNavigationLeaveGuard(() => {
    if (!atStake) return true
    return window.confirm(t(busy ? 'pages.membersPage.create_leave_busy' : 'pages.membersPage.create_leave_draft'))
  })
  usePublishNavigationStake(atStake)

  const submit = () => {
    setError(''); setHint(''); setUnconfirmed(false)
    const n = name.trim()
    if (!n) { setHint(t('pages.membersPage.create_name_required')); return }
    // The roster reads names through the agent-name grammar (`_AGENT_NAME_RE`):
    // a name the server would store but the roster would drop (a space, an
    // accent, a trailing dot) must be refused HERE, or the create lands and
    // the page then declares the new crewmate gone.
    if (!isValidCrewmateName(n)) { setHint(t('pages.membersPage.create_name_invalid')); return }
    // The roster already has this name: the server would answer 409
    // `agent_exists`, so say that without a request — as a HINT under the
    // field like the other validation refusals (nothing failed; `error` and
    // its ErrorNotice are for requests that did). This is also the premise
    // of the reconcile in `onError`: every request that leaves here carries a
    // name the roster did NOT have.
    if (existingNames.includes(n)) { setHint(t('pages.membersPage.create_name_taken', { name: n })); return }
    createMut.mutate({
      name: n,
      kiro_agent: builtFromValue,
      workspace,
      memory_store: 'default',
      description: job.trim(),
      triggers,
      session_color: sessionColor,
      ...(model !== INHERIT_MODEL ? { model } : {}),
    })
  }

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        title={t('pages.membersPage.add_member')}
        maxWidth={480}
        guardAccidentalDismiss={dirty}
        dismissDisabled={busy || wsModalOpen}
        // The nested New workspace dialog is a Radix layer portaled to
        // document.body, outside this panel: while it is open, this panel is
        // inert and its Tab trap stands down with it, or every Tab in that
        // form is pulled back here (the shared trap's stacked-dialog contract).
        interactionDisabled={wsModalOpen}
        footer={
          <>
            <Btn onClick={onClose} disabled={busy || wsModalOpen}>{t('pages.membersPage.create_cancel')}</Btn>
            <Btn primary type="submit" form={FORM_ID} disabled={busy || wsModalOpen} data-testid="crewmate-create-submit">
              {busy ? t('pages.membersPage.create_submitting') : t('pages.membersPage.create_submit')}
            </Btn>
          </>
        }
      >
        <form
          id={FORM_ID}
          className="flex flex-col gap-5"
          data-testid="crewmate-create-form"
          onSubmit={(e) => { e.preventDefault(); if (!busy) submit() }}
        >
          {/* One lock for the whole form while the POST is in flight: a
              disabled fieldset disables every control under it — the Advanced
              toggle and the editor's own fields included, which take no
              `disabled` prop of their own — so an edit cannot land after the
              body was sent and vanish when success closes the dialog. */}
          <fieldset
            disabled={busy || wsModalOpen}
            aria-busy={busy || undefined}
            className="contents min-w-0 m-0 p-0 border-0"
            data-testid="crewmate-create-fieldset"
          >
          <Field label={t('pages.membersPage.create_name')}>
            <Input
              value={name}
              onChange={(e) => { setName(e.target.value); setHint(''); setError(''); setNameRefused(false); setUnconfirmed(false) }}
              aria-label={t('pages.membersPage.create_name')}
              aria-invalid={hint || nameRefused ? true : undefined}
              aria-describedby={hint ? 'crewmate-create-name-hint' : undefined}
              placeholder={t('pages.membersPage.create_name_placeholder')}
              // The variant carries an attribute selector, so it outranks the
              // base `border-border` whatever order the stylesheet emits them in.
              className="aria-invalid:border-danger"
              autoFocus
              disabled={busy}
            />
            {/* A refusal, not a field hint: it reads in the error tone and the
                field's border goes with it, so a blank submit never looks like
                "the form before I typed anything". */}
            {hint && (
              <span id="crewmate-create-name-hint" role="alert" className="text-[11.5px] leading-relaxed text-danger" data-testid="crewmate-create-name-hint">
                {hint}
              </span>
            )}
          </Field>
          <Field label={t('pages.membersPage.agent_template')} hint={t('pages.membersPage.built_from_hint')}>
            <SimpleSelect
              options={builtFromOptions}
              optionLabels={builtFromLabels}
              value={builtFromValue}
              onChange={setBuiltFrom}
              disabled={busy}
              aria-label={t('pages.membersPage.agent_template')}
            />
          </Field>
          <Field
            label={`${t('pages.membersPage.create_job')} · ${t('pages.membersPage.create_optional')}`}
            hint={t('pages.membersPage.create_job_hint')}
          >
            <Input
              value={job}
              onChange={(e) => setJob(e.target.value)}
              aria-label={t('pages.membersPage.create_job')}
              placeholder={t('pages.membersPage.create_job_placeholder')}
              disabled={busy}
            />
          </Field>
          <div className="flex flex-col gap-4">
            <button
              type="button"
              onClick={() => setAdvanced((v) => !v)}
              aria-expanded={advanced}
              aria-controls="crewmate-create-advanced"
              className="flex items-center gap-1 self-start -ml-1 px-1 py-0.5 rounded text-[12px] text-muted hover:text-text bg-transparent border-none cursor-pointer focus-ring"
              data-testid="crewmate-create-advanced-toggle"
            >
              <ChevronRight
                size={13}
                className={`lucide-inline transition-transform duration-150 motion-reduce:transition-none ${advanced ? 'rotate-90' : ''}`}
                aria-hidden="true"
              />
              {t('pages.membersPage.create_advanced')}
            </button>
            {/* The disclosure grows out of its toggle instead of appearing whole:
                the same element, unfolding — so the reader sees where the extra
                fields came from. Cut, not animated, under reduced motion. */}
            <AnimatePresence initial={false}>
              {advanced && (
                <motion.div
                  key="advanced"
                  id="crewmate-create-advanced"
                  className="flex flex-col gap-4 overflow-hidden"
                  initial={reduceMotion ? false : { height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={reduceMotion ? { opacity: 0, transition: { duration: 0 } } : { height: 0, opacity: 0 }}
                  transition={{ duration: 0.18, ease: 'easeOut' }}
                  data-testid="crewmate-create-advanced"
                >
                  <WorkspaceField
                    subject="member"
                    hint={t('pages.membersPage.create_workspace_hint')}
                    options={workspaceOptions}
                    value={workspace}
                    onChange={setWorkspace}
                    onNewWorkspace={() => setWsModalOpen(true)}
                  />
                  <ModelField
                    options={modelOptions}
                    value={model}
                    onChange={setModel}
                    hint={t('pages.kiroCrewAgentsPage.model_inherited_from_default')}
                  />
                  <TriggersField value={triggers} onChange={setTriggers} subject="member" />
                  <SessionColorField value={sessionColor} onChange={setSessionColor} subject="member" />
                </motion.div>
              )}
            </AnimatePresence>
          </div>
          {/* No hand-off on either notice: both sit over this unsaved form —
              the name, job and every Advanced pick live only in local state —
              and the hand-off navigates to the chat, unmounting the dialog
              and the draft with it. */}
          {/* A load that did not happen is a failure (errors-use-error-notice):
              the shared notice, inline, naming WHICH list fell back so the
              user knows what they are not being offered. The create still
              works with the defaults. */}
          {optionsError && !error && (
            <ErrorNotice
              message={t('pages.membersPage.create_options_failed', {
                list: installedError
                  ? t('pages.membersPage.agent_template')
                  : workspacesError
                    ? t('pages.kiroCrewAgentsPage.workspace_2')
                    : t('pages.kiroCrewAgentsPage.model'),
              })}
              variant="inline"
              testId="crewmate-create-options-error"
            />
          )}
          </fieldset>
          {/* An UNCONFIRMED create is not a failure like the others: its lead
              says so in bold and it carries its own test id, so it can never
              be read — by a user or a test — as one of the "nothing was
              created, try again" notices. */}
          {error && (
            <ErrorNotice
              message={error}
              title={unconfirmed ? t('pages.membersPage.create_unconfirmed_title') : undefined}
              // With a title, the block notice would leave the message as a
              // bare text node beside the <strong> lead, so the exact message
              // is no longer addressable on its own (an exact text lookup sees
              // "Not confirmed Couldn't confirm…" as one element). `inline` is
              // a span's default display, so the wrap it buys changes nothing
              // on screen; it only gives the sentence its own element.
              messageClassName={unconfirmed ? 'inline' : undefined}
              testId={unconfirmed ? 'crewmate-create-unconfirmed' : 'crewmate-create-error'}
            />
          )}
        </form>
      </Modal>
      <WorkspaceModal
        open={wsModalOpen}
        onDirtyChange={setWsDirty}
        workspaceOptions={workspaceOptions}
        // No network-deferred state write: the new name goes into the cached
        // list at once and `pendingWorkspace` picks it on the very next commit
        // (see the effect above), so nothing can land on a dialog that was
        // dismissed and reopened while a slow refresh was in flight. The
        // refetch only reconciles the list with the server.
        onCreated={(newName) => {
          setWsModalOpen(false)
          queryClient.setQueryData(['workspaces'], (prev: { workspaces?: { name: string }[] } | undefined) => {
            const list = prev?.workspaces ?? [{ name: 'default' }]
            return list.some((w) => w.name === newName) ? prev : { ...prev, workspaces: [...list, { name: newName }] }
          })
          setPendingWorkspace(newName)
          void refetchWorkspaces()
        }}
        onClose={() => setWsModalOpen(false)}
      />
    </>
  )
}
