import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, CircleAlert, ExternalLink, LoaderCircle } from 'lucide-react'
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '../../components/ui/dialog'
import { Btn } from '../../components/ui'
import AgentSpecNotRewrittenNotice, { agentSpecNotRewritten } from '../../components/AgentSpecNotRewrittenNotice'
import ErrorNotice from '../../components/ErrorNotice'
import { api } from '../../api/client'
import type { ComputerUseConfigData, ComputerUseSetupAction, ComputerUseSetupDoc, ComputerUseSetupStep } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { COMPUTER_USE_SETUP_QK, useComputerUseSetupAction, useComputerUseSetupQuery } from '../../hooks/useComputerUseSetup'
import {
  openSystemSettings,
  ownerName,
  revokedPane,
  grantRecheckInterval,
  sessionKindKey,
  setupPollInterval,
  stepLabelKey,
  stepsFor,
  viewFor,
  waitExpired,
  waitingPane,
  blockedKey,
  problemText,
  SETUP_INSTALL_POLL_MAX_MS,
  SETUP_POLL_MAX_MS,
  type SetupPane,
  type SetupView,
} from './computerUseSetup'

const CONFIG_QK = ['computer-use-config']

/** Catalog keys for the screen-reader status of each step. Literal keys so the
 *  i18n key checker resolves them. */
const STEP_STATUS_KEY: Record<ComputerUseSetupStep['status'], string> = {
  done: 'pages.settings.computerUseSetup.step_status_done',
  current: 'pages.settings.computerUseSetup.step_status_current',
  pending: 'pages.settings.computerUseSetup.step_status_pending',
  failed: 'pages.settings.computerUseSetup.step_status_failed',
  not_applicable: 'pages.settings.computerUseSetup.step_status_not_applicable',
}

/** The progress rail: one row per step, the current one marked for assistive
 *  technology with `aria-current="step"`. */
function StepRail({ doc }: { doc: ComputerUseSetupDoc }) {
  const steps = stepsFor(doc).filter(s => s.status !== 'not_applicable')
  return (
    <ol aria-label={i18nT('pages.settings.computerUseSetup.progress_label')} className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 p-0" data-testid="cu-setup-steps">
      {steps.map(step => {
        const tone =
          step.status === 'done' ? 'text-ok-text' : step.status === 'current' ? 'text-text-strong font-semibold' : step.status === 'failed' ? 'text-danger-text' : 'text-muted'
        return (
          <li key={step.id} aria-current={step.status === 'current' ? 'step' : undefined} className={`inline-flex items-center gap-1 text-[12px] ${tone}`} data-step={step.id} data-status={step.status}>
            {step.status === 'done' ? <Check size={12} aria-hidden="true" /> : step.status === 'failed' ? <CircleAlert size={12} aria-hidden="true" /> : <span aria-hidden="true">•</span>}
            <span>{i18nT(stepLabelKey(step.id))}</span>
            <span className="sr-only">{i18nT(STEP_STATUS_KEY[step.status] ?? STEP_STATUS_KEY.pending)}</span>
          </li>
        )
      })}
    </ol>
  )
}

/** Desktop facts for Windows and Linux: which session the driver sees, the
 *  Wayland screen-sharing note, and whatever the backend says works differently. */
function DesktopFacts({ doc }: { doc: ComputerUseSetupDoc }) {
  const showSession = doc.platform === 'windows' || doc.platform === 'linux'
  if (!showSession && doc.limitations.length === 0) return null
  return (
    <div className="space-y-1.5 rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-[12.5px]" data-testid="cu-setup-desktop">
      {showSession && (
        <p className="m-0 text-text">
          {i18nT('pages.settings.computerUseSetup.desktop_line', { kind: i18nT(sessionKindKey(doc.session.kind)) })}
          {doc.session.detail ? <span className="text-muted"> ({doc.session.detail})</span> : null}
        </p>
      )}
      {doc.session.kind === 'wayland' && <p className="m-0 text-muted">{i18nT('pages.settings.computerUseSetup.wayland_note')}</p>}
      {doc.limitations.length > 0 && (
        <div>
          <p className="m-0 font-medium text-text">{i18nT('pages.settings.computerUseSetup.limitations_title')}</p>
          <ul className="m-0 list-disc pl-5 text-muted">
            {doc.limitations.map(item => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function Spinner() {
  return <LoaderCircle size={14} aria-hidden="true" className="animate-spin motion-reduce:animate-none" />
}

export interface ComputerUseSetupDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The config snapshot, for the intro's enable and its policy lock. */
  config?: ComputerUseConfigData
}

/**
 * The first-use Computer Use setup dialog.
 *
 * Progressive and resumable: each view answers the setup document's current
 * state, and the progress lives on the server, so closing the dialog at any
 * point loses nothing and reopening it lands on the same step.
 *
 * Three rules this component keeps, each pinned by a test:
 *  - every wait is bounded. A moving state (installing, validating, waiting for
 *    System Settings) polls for a fixed time, then says it is taking longer than
 *    expected and offers Check again. No spinner runs forever;
 *  - System Settings opens only from a click on an Open … Settings button, once
 *    per click, never from a state change or a poll;
 *  - "ready" is shown only when the document says `ready`.
 */
export function ComputerUseSetupDialog({ open, onOpenChange, config }: ComputerUseSetupDialogProps) {
  const qc = useQueryClient()
  // When the current wait began. Reset on every state change and on each user
  // action, so Check again starts a fresh bounded window.
  const [since, setSince] = useState(() => Date.now())
  const [now, setNow] = useState(() => Date.now())
  // True after the user opened System Settings for the grant on screen; only
  // then is a requesting/denied state worth re-reading on a timer.
  const [awaitingSettings, setAwaitingSettings] = useState(false)
  const [actionError, setActionError] = useState('')
  const [sessionsRestarted, setSessionsRestarted] = useState(false)
  // The enable saved, but the agent spec was not rebuilt to carry the tools.
  const [specNotRewritten, setSpecNotRewritten] = useState(false)
  const sinceRef = useRef(since)
  const awaitingRef = useRef(awaitingSettings)
  sinceRef.current = since
  awaitingRef.current = awaitingSettings

  const setupQ = useComputerUseSetupQuery({
    enabled: open,
    refetchInterval: doc => setupPollInterval(doc?.state, awaitingRef.current, sinceRef.current, Date.now()),
  })
  const actionMut = useComputerUseSetupAction()
  // Background rechecks while the user is in System Settings. A separate
  // mutation, so the poll never disables the buttons or spins Check again.
  const pollMut = useComputerUseSetupAction()
  const enableMut = useMutation({
    mutationFn: () => api.saveComputerUseConfig({ enabled: true }),
    onSuccess: data => {
      qc.setQueryData(CONFIG_QK, data)
      if ((data?.sessions_reset ?? 0) > 0) setSessionsRestarted(true)
      if (data && 'agent_spec_rewritten' in data) setSpecNotRewritten(agentSpecNotRewritten(data))
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: CONFIG_QK })
      void qc.invalidateQueries({ queryKey: COMPUTER_USE_SETUP_QK })
    },
  })

  const doc = setupQ.data
  const blockedTextKey = doc ? blockedKey(doc) : null
  const blockedMessage = blockedTextKey ? i18nT(blockedTextKey, { how: doc?.blocked ?? '' }) : null
  const problem = doc ? problemText(doc, blockedMessage) : null
  /** True when the view's own body already shows `problem`, so it is not repeated. */
  let bodyShowsProblem = false
  const state = doc?.state
  const view: SetupView | undefined = doc ? viewFor(doc) : undefined

  // A new state is a new wait. Leaving the grant views also ends the
  // "awaiting System Settings" window, so the next grant needs its own click.
  const lastState = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (state === lastState.current) return
    lastState.current = state
    const t = Date.now()
    setSince(t)
    setNow(t)
    setAwaitingSettings(false)
  }, [state])

  // Re-render once the current wait's bound passes, so a spinner turns into
  // "taking longer than expected" even though the poll has stopped.
  useEffect(() => {
    if (!open || !state) return
    const bound = state === 'installing' ? SETUP_INSTALL_POLL_MAX_MS : SETUP_POLL_MAX_MS
    const left = since + bound - Date.now()
    if (left <= 0) return
    const timer = window.setTimeout(() => setNow(Date.now()), left + 50)
    return () => window.clearTimeout(timer)
  }, [open, state, since])

  // Move focus to the view's heading when the view changes, so a keyboard or
  // screen-reader user lands on the new step instead of a button that vanished.
  const headingRef = useRef<HTMLHeadingElement>(null)
  const lastView = useRef<SetupView | undefined>(undefined)
  useEffect(() => {
    if (!open) {
      lastView.current = undefined
      return
    }
    if (view && lastView.current && view !== lastView.current) headingRef.current?.focus()
    lastView.current = view
  }, [open, view])

  // While the user is in System Settings, ask the driver for the current
  // grants (a non-prompting recheck) on a bounded timer: a plain read returns
  // the last recorded grants, so it would never see the switch turned on.
  const pollPending = pollMut.isPending
  const { mutate: pollRecheck } = pollMut
  useEffect(() => {
    if (!open || pollPending) return
    const delay = grantRecheckInterval(state, awaitingSettings, since, Date.now())
    if (delay === false) return
    const timer = window.setTimeout(() => pollRecheck('recheck'), delay)
    return () => window.clearTimeout(timer)
  }, [open, state, awaitingSettings, since, pollPending, pollRecheck, setupQ.dataUpdatedAt])

  const busy = actionMut.isPending || enableMut.isPending
  const expired = waitExpired(state, since, now, awaitingSettings)

  const run = (action: ComputerUseSetupAction) => {
    setActionError('')
    const t = Date.now()
    setSince(t)
    setNow(t)
    actionMut.mutate(action, {
      onError: () => setActionError(i18nT('pages.settings.computerUseSetup.action_failed')),
      // Switching driver changes what the Settings page reports as supported.
      onSuccess: () => {
        if (action === 'use_builtin_driver') void qc.invalidateQueries({ queryKey: CONFIG_QK })
      },
    })
  }
  const recheck = () => {
    if (setupQ.isError) {
      setActionError('')
      void setupQ.refetch()
      return
    }
    // A failed desktop check is cleared only by checking again for real.
    run(doc?.problem?.code === 'verify_failed' ? 'verify' : 'recheck')
  }
  const openPane = (pane: SetupPane) => {
    // One window per click; the rechecks that follow never open anything.
    openSystemSettings(pane)
    const t = Date.now()
    setSince(t)
    setNow(t)
    setAwaitingSettings(true)
  }
  const enable = () => {
    setActionError('')
    enableMut.mutate(undefined, {
      onError: () => setActionError(i18nT('pages.settings.computerUseSetup.action_failed')),
    })
  }
  const close = () => onOpenChange(false)

  const owner = doc ? ownerName(doc) : ''
  /** The open-source driver was chosen on a Mac, where it is not offered. */
  const macosUpstream = doc?.platform === 'macos' && doc.driver.kind === 'upstream'

  // The upstream driver was blocked at launch: where a built-in driver exists,
  // offer it as the way forward (the error text names it).
  const builtinBtn =
    blockedMessage && doc && doc.platform !== 'linux' ? (
      <Btn type="button" primary onClick={() => run('use_builtin_driver')} disabled={busy} busy={actionMut.isPending && actionMut.variables === 'use_builtin_driver'} data-testid="cu-use-builtin">
        {i18nT('pages.settings.computerUseSetup.use_builtin_action')}
      </Btn>
    ) : null
  const checkAgainBtn = (
    <Btn type="button" onClick={recheck} disabled={busy} busy={actionMut.isPending && actionMut.variables === 'recheck'}>
      {i18nT('pages.settings.computerUseSetup.check_again')}
    </Btn>
  )

  const paneButton = (pane: SetupPane) => (
    <Btn type="button" primary onClick={() => openPane(pane)} disabled={busy} data-testid={`cu-setup-open-${pane}`}>
      {i18nT(pane === 'accessibility' ? 'pages.settings.computerUseSetup.open_accessibility' : 'pages.settings.computerUseSetup.open_screen_recording')}
      <ExternalLink size={13} aria-hidden="true" />
    </Btn>
  )

  let title = ''
  let body: ReactNode = null
  let actions: ReactNode = null

  const waitingLine = (text: string) => (
    <p className="m-0 inline-flex items-center gap-2 text-[13px] text-muted" role="status">
      <Spinner />
      {text}
    </p>
  )
  const slowLine = <p className="m-0 text-[13px] text-warn-text" role="status">{i18nT('pages.settings.computerUseSetup.taking_longer')}</p>

  switch (view) {
    case 'intro': {
      const locked = config?.locked_by_policy === true
      title = i18nT('pages.settings.computerUseSetup.intro_title')
      body = (
        <>
          <p className="m-0">{i18nT('pages.settings.computerUseSetup.intro_body')}</p>
          <p className="m-0 text-muted">{i18nT('pages.settings.computerUseSetup.intro_body_restart')}</p>
          {locked && <p className="m-0 text-warn-text">{i18nT('pages.settings.computerUseSetup.intro_locked')}</p>}
        </>
      )
      actions = (
        <>
          <Btn type="button" onClick={close}>{i18nT('pages.settings.computerUseSetup.not_now')}</Btn>
          <Btn type="button" primary onClick={enable} disabled={busy || locked} busy={enableMut.isPending}>
            {i18nT('pages.settings.computerUseSetup.continue')}
          </Btn>
        </>
      )
      break
    }
    case 'install':
      title = i18nT('pages.settings.computerUseSetup.install_title')
      body = <p className="m-0">{i18nT('pages.settings.computerUseSetup.install_body')}</p>
      actions = (
        <Btn type="button" primary onClick={() => run('install')} disabled={busy} busy={actionMut.isPending}>
          {i18nT('pages.settings.computerUseSetup.install_action')}
        </Btn>
      )
      break
    case 'installing':
      title = i18nT('pages.settings.computerUseSetup.installing_title')
      body = expired ? slowLine : (
        <>
          {waitingLine(i18nT('pages.settings.computerUseSetup.installing_body'))}
          <p className="m-0 text-[13px] text-muted" data-testid="cu-installing-keeps-going">{i18nT('pages.settings.computerUseSetup.installing_keeps_going')}</p>
        </>
      )
      actions = expired ? (
        <>
          {checkAgainBtn}
          <Btn type="button" primary onClick={() => run('install')} disabled={busy}>{i18nT('pages.settings.computerUseSetup.retry_install')}</Btn>
        </>
      ) : null
      break
    case 'install_failed':
      title = i18nT('pages.settings.computerUseSetup.install_failed_title')
      // One message: the specific reason (or the block), then the general hint.
      body = (
        <ErrorNotice
          askAgent
          message={
            blockedMessage ||
            [problem, i18nT('pages.settings.computerUseSetup.install_failed_body')].filter(Boolean).join(' ')
          }
        />
      )
      bodyShowsProblem = true
      actions = (
        <>
          <Btn type="button" primary={!builtinBtn} onClick={() => run('install')} disabled={busy} busy={actionMut.isPending && actionMut.variables === 'install'}>
            {i18nT('pages.settings.computerUseSetup.retry_install')}
          </Btn>
          {builtinBtn}
        </>
      )
      break
    case 'accessibility':
    case 'screen_recording': {
      const pane: SetupPane = view
      const denied = state === 'accessibility_denied' || state === 'screen_recording_denied'
      title = i18nT(pane === 'accessibility' ? 'pages.settings.computerUseSetup.accessibility_title' : 'pages.settings.computerUseSetup.screen_recording_title')
      const bodyKey =
        pane === 'accessibility'
          ? denied
            ? 'pages.settings.computerUseSetup.accessibility_denied_body'
            : 'pages.settings.computerUseSetup.accessibility_body'
          : denied
            ? 'pages.settings.computerUseSetup.screen_recording_denied_body'
            : 'pages.settings.computerUseSetup.screen_recording_body'
      body = (
        <>
          <p className="m-0">{i18nT(bodyKey, { owner })}</p>
          {awaitingSettings && (expired ? slowLine : waitingLine(i18nT('pages.settings.computerUseSetup.waiting_for_switch', { owner })))}
        </>
      )
      actions = (
        <>
          {checkAgainBtn}
          {paneButton(pane)}
        </>
      )
      break
    }
    case 'waiting': {
      const pane = doc ? waitingPane(doc) : 'accessibility'
      title = i18nT('pages.settings.computerUseSetup.waiting_title')
      body = (
        <>
          <p className="m-0">{i18nT('pages.settings.computerUseSetup.waiting_body', { owner })}</p>
          {expired ? slowLine : waitingLine(i18nT('pages.settings.computerUseSetup.waiting_for_switch', { owner }))}
        </>
      )
      actions = (
        <>
          {checkAgainBtn}
          {paneButton(pane)}
        </>
      )
      break
    }
    case 'restart':
      title = i18nT('pages.settings.computerUseSetup.restart_title')
      body = <p className="m-0">{i18nT('pages.settings.computerUseSetup.restart_body')}</p>
      actions = (
        <Btn type="button" primary onClick={() => run('restart_driver')} disabled={busy} busy={actionMut.isPending}>
          {i18nT('pages.settings.computerUseSetup.restart_action')}
        </Btn>
      )
      break
    case 'verify': {
      const running = actionMut.isPending && actionMut.variables === 'verify'
      title = i18nT('pages.settings.computerUseSetup.verify_title')
      body = (
        <>
          <p className="m-0">{i18nT('pages.settings.computerUseSetup.verify_body')}</p>
          {running ? waitingLine(i18nT('pages.settings.computerUseSetup.verify_running')) : expired ? slowLine : null}
        </>
      )
      actions = (
        <Btn type="button" primary onClick={() => run('verify')} disabled={busy} busy={running}>
          {i18nT('pages.settings.computerUseSetup.verify_action')}
        </Btn>
      )
      break
    }
    case 'ready':
      title = i18nT('pages.settings.computerUseSetup.ready_title')
      body = <p className="m-0">{i18nT('pages.settings.computerUseSetup.ready_body')}</p>
      actions = (
        <Btn type="button" primary onClick={close}>
          {i18nT('pages.settings.computerUseSetup.done')}
        </Btn>
      )
      break
    case 'unavailable':
      title = i18nT('pages.settings.computerUseSetup.unavailable_title')
      body = blockedMessage ? (
        <ErrorNotice askAgent message={blockedMessage} />
      ) : (
        <p className="m-0">{i18nT('pages.settings.computerUseSetup.unavailable_body')}</p>
      )
      bodyShowsProblem = Boolean(blockedMessage && problem === blockedMessage)
      // Two actions at most: when the driver was blocked at launch, Restart driver
      // is the one retry and the built-in driver is the way forward.
      actions = (
        <>
          {!builtinBtn && checkAgainBtn}
          <Btn type="button" primary={!builtinBtn} onClick={() => run('restart_driver')} disabled={busy} busy={actionMut.isPending && actionMut.variables === 'restart_driver'}>
            {i18nT('pages.settings.computerUseSetup.restart_action')}
          </Btn>
          {builtinBtn}
        </>
      )
      break
    case 'revoked': {
      const pane = doc ? revokedPane(doc) : 'accessibility'
      title = i18nT('pages.settings.computerUseSetup.revoked_title')
      body = (
        <>
          <p className="m-0">{i18nT('pages.settings.computerUseSetup.revoked_body', { owner })}</p>
          {awaitingSettings && (expired ? slowLine : waitingLine(i18nT('pages.settings.computerUseSetup.waiting_for_switch', { owner })))}
        </>
      )
      actions = (
        <>
          {checkAgainBtn}
          {paneButton(pane)}
        </>
      )
      break
    }
    case 'session0':
      title = i18nT('pages.settings.computerUseSetup.session0_title')
      body = <p className="m-0">{i18nT('pages.settings.computerUseSetup.session0_body')}</p>
      actions = checkAgainBtn
      break
    case 'no_desktop':
      title = i18nT('pages.settings.computerUseSetup.no_desktop_title')
      body = (
        <p className="m-0">
          {i18nT(doc?.platform === 'linux' ? 'pages.settings.computerUseSetup.no_desktop_body_linux' : 'pages.settings.computerUseSetup.no_desktop_body_windows')}
        </p>
      )
      actions = checkAgainBtn
      break
    case 'unsupported':
      // On a Mac the platform works; only the driver choice is refused, so the
      // heading names that rather than saying Computer Use is unavailable.
      title = i18nT(macosUpstream ? 'pages.settings.computerUseSetup.upstream_not_offered_macos_title' : 'pages.settings.computerUseSetup.unsupported_title')
      body = (
        <p className="m-0">
          {i18nT(macosUpstream ? 'pages.settings.computerUseSetup.upstream_not_offered_macos' : 'pages.settings.computerUseSetup.unsupported_body')}
        </p>
      )
      // The open-source driver is not offered on macOS: the body already says
      // why, and the way forward is the built-in driver, one click away.
      bodyShowsProblem = macosUpstream
      actions =
        macosUpstream ? (
          <>
            <Btn type="button" onClick={close}>
              {i18nT('pages.settings.computerUseSetup.close')}
            </Btn>
            <Btn type="button" primary onClick={() => run('use_builtin_driver')} disabled={busy} busy={actionMut.isPending && actionMut.variables === 'use_builtin_driver'} data-testid="cu-use-builtin">
              {i18nT('pages.settings.computerUseSetup.use_builtin_action')}
            </Btn>
          </>
        ) : (
          <Btn type="button" onClick={close}>
            {i18nT('pages.settings.computerUseSetup.close')}
          </Btn>
        )
      break
    case 'error':
      title = i18nT('pages.settings.computerUseSetup.error_title')
      body = <ErrorNotice askAgent message={problem || i18nT('pages.settings.computerUseSetup.error_body')} />
      bodyShowsProblem = true
      actions = (
        <>
          <Btn type="button" onClick={() => run('reset')} disabled={busy}>
            {i18nT('pages.settings.computerUseSetup.reset_action')}
          </Btn>
          {checkAgainBtn}
        </>
      )
      break
    default:
      break
  }

  // The status read itself failed (network, gateway down): its own view, with
  // the one action that can fix it.
  if (setupQ.isError && !doc) {
    title = i18nT('pages.settings.computerUseSetup.error_title')
    body = <ErrorNotice askAgent message={i18nT('pages.settings.computerUseSetup.status_load_error')} />
    actions = checkAgainBtn
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent maxWidth={560} data-testid="cu-setup-dialog" data-view={setupQ.isError && !doc ? 'load_error' : (view ?? 'loading')}>
        <DialogHeader>
          <DialogTitle>{i18nT('pages.settings.computerUseSetup.dialog_title')}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <div className="space-y-3 text-[13px] text-text">
            {doc && view !== 'intro' && <StepRail doc={doc} />}
            {!doc && !setupQ.isError ? (
              waitingLine(i18nT('pages.settings.computerUseSetup.loading'))
            ) : (
              <section aria-labelledby="cu-setup-view-title" className="space-y-2">
                <h3 id="cu-setup-view-title" ref={headingRef} tabIndex={-1} className="m-0 text-[15px] font-semibold text-text-strong outline-none">
                  {title}
                </h3>
                {body}
                {problem && !bodyShowsProblem && (
                  <div data-testid="cu-setup-problem">
                    <ErrorNotice askAgent message={problem} />
                  </div>
                )}
                {doc?.user_action && !doc.problem && !blockedMessage && view !== 'intro' && view !== 'ready' && (
                  <p className="m-0 text-text" data-testid="cu-setup-user-action">{doc.user_action}</p>
                )}
                {specNotRewritten && <AgentSpecNotRewrittenNotice askAgent={false} />}
                {sessionsRestarted && <p className="m-0 text-muted">{i18nT('pages.settings.computerUsePanel.your_chat_sessions_were_restarted')}</p>}
                {doc && view !== 'intro' && <DesktopFacts doc={doc} />}
              </section>
            )}
            {/* The dialog holds no draft, so the hand-off cannot lose anything. */}
            <ErrorNotice
              variant="inline"
              askAgent
              message={
                actionError ||
                // A failed background recheck stops the timer, and a failed refresh
                // keeps the last document on screen: say so rather than show it as live.
                (doc && (pollMut.isError || setupQ.isError) ? i18nT('pages.settings.computerUseSetup.status_load_error') : null)
              }
            />
          </div>
        </DialogBody>
        {actions && (
          <DialogFooter>
            <div className="flex flex-wrap justify-end gap-2">{actions}</div>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}
