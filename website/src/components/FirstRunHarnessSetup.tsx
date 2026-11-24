import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Boxes, CheckCircle2, ChevronDown, CircleDashed, RefreshCw } from 'lucide-react'

import { api, type AcpBackendProbe, type KasBundleStatus } from '../api/client'
import type { KiroPrerequisiteStatus } from '../api/client/onboarding'
import { ACP_BACKEND_KAS, configuredAcpBackend, DEFAULT_ACP_BACKEND, type AcpBackendConfig } from '../api/acpBackend'
import { useKasLoginStatus } from '../hooks/useKasLogin'
import { clearCachedModels } from '../providers/adapters/acp'
import { KIRO_PREREQUISITE_KEY } from '../hooks/useKiroPrerequisite'
import { i18nT } from '../i18n/t'
import { HarnessLogo } from './HarnessLogo'
import { KiroSignInFlow } from './KiroSignInDialog'
import ErrorNotice from './ErrorNotice'
import { SignInBackContext, type SignInBackSlot } from './KasLoginGate'
import SignInCommands, { KiroCliCheckAgain } from './KiroCliSignInCommands'
import { SetupShell } from './KiroPrerequisiteSetupShell'
import {
  applyRecheckedBackend,
  configuredBackendCanStart,
  notReadyReason,
  sandboxOffFor,
} from './KiroPrerequisiteGate'
import { AgentNotReadyDetail, AgentStatusBadge, HarnessInstallDetail, HarnessPickerList, harnessList } from './agentHarness'
import { useConfigSchemaQuery } from './settingRef/useConfigSchema'
import { Badge, Btn, IconButton, SendBtn } from './ui'
import { LookingGhost } from '../assets/onboarding/LookingGhost'

/** The built-in harness (Kiro Agent on Crew's own engine) and Kiro CLI, as this step names them. */
const BUILT_IN = 'built-in'
const KIRO_CLI = 'kiro'
type Screen = 'choose' | 'sign-in' | 'kiro-cli-sign-in' | 'ready'

/**
 * First-run setup: pick the harness, sign in once, then "ready".
 *
 * Shown by `FirstRunSignInHold` only while the built-in engine is on. Built from
 * the pieces Settings > Agent Harness uses (`agentHarness/*`, the same
 * `/api/acp-backends` probe and the gate's own start verdict), the shared Kiro
 * sign-in (`KiroSignInFlow`) and the kiro-cli sign-in commands. Nothing here is
 * a second copy of those.
 *
 * - Built-in harness is first and picked by default, unless this machine cannot
 *   run it (`host_unsupported`).
 * - Kiro CLI is always listed; when it is missing its row shows how to install
 *   it, and Continue stays off while it is picked.
 * - Every other harness sits behind one "Other harnesses" row, with the state
 *   the Settings list shows.
 * - The choice is saved with the two keys Settings writes. No chat exists yet,
 *   so there is no restart prompt: the first chat starts on what was saved.
 */
export default function FirstRunHarnessSetup({ status, onDone }: {
  status?: KiroPrerequisiteStatus
  onDone: () => void
}) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const bundleQ = useQuery<KasBundleStatus>({
    queryKey: ['kas-bundle'],
    queryFn: () => api.kasBundleStatus(),
    retry: false,
  })
  const backendsQ = useQuery({ queryKey: ['acpBackends'], queryFn: () => api.acpBackends() })
  const crewSignIn = useKasLoginStatus()
  const bundle = bundleQ.data
  const builtInOk = !bundle?.host_unsupported
  const cliInstalled = status?.installed === true
  const cliSignedIn = cliInstalled && status?.authenticated === true
  const crewSignedIn = crewSignIn.data?.authenticated === true
  // The same list Settings > Agent Harness shows (order, filtering, names, verdicts),
  // minus the two Kiro rows this step draws itself above the expander.
  const { schema, error: schemaError } = useConfigSchemaQuery()
  const cfgQ = useQuery<AcpBackendConfig>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const list = harnessList({
    backends: backendsQ.data?.backends,
    selectable: schema?.get('agent.acp_backend')?.enum,
    current: cfgQ.data ? configuredAcpBackend(cfgQ.data) : DEFAULT_ACP_BACKEND,
  })
  // The deployment's policy can deny a harness (the schema enum Settings reads):
  // a denied one gets no row here either, as in Settings.
  const kasAllowed = !list.unavailable(ACP_BACKEND_KAS)
  // Kiro CLI runs on KAS where KAS is allowed; where it is not, it is saved as itself.
  const kiroCliAllowed = kasAllowed || !list.unavailable(KIRO_CLI)
  const builtInUsable = kasAllowed && builtInOk
  const others = list.offered
    .filter(id => id !== KIRO_CLI && id !== ACP_BACKEND_KAS)
    .map(list.probe)
    .filter((b): b is AcpBackendProbe => !!b && b.independent_setup === true)

  const defaultPick = builtInUsable ? BUILT_IN : cliInstalled && kiroCliAllowed ? KIRO_CLI : ''
  const [picked, setPicked] = useState<string | null>(null)
  const pick = picked ?? defaultPick
  // Opened on its own only when neither Kiro harness can be used here.
  const [othersOpen, setOthersOpen] = useState<boolean | null>(null)
  const showOthers = othersOpen ?? (!builtInUsable && !(cliInstalled && kiroCliAllowed))
  const [screen, setScreen] = useState<Screen>('choose')

  const otherProbe: AcpBackendProbe | undefined = others.find(b => b.id === pick)
  const usable = pick === BUILT_IN
    ? builtInUsable
    : pick === KIRO_CLI
      ? cliInstalled && kiroCliAllowed
      : !!otherProbe && configuredBackendCanStart(status, otherProbe)
  const pickName = pick === BUILT_IN
    ? i18nT('pages.developer.kasBundleCard.engine_builtin')
    : pick === KIRO_CLI
      ? i18nT('pages.developer.kasBundleCard.engine_kiro_cli')
      : otherProbe ? list.nameOf(otherProbe.id) : ''

  // The same two config keys Settings > Agent Harness writes.
  const saveMut = useMutation({
    mutationFn: async () => {
      if (pick === BUILT_IN || (pick === KIRO_CLI && kasAllowed)) {
        await api.patchConfig('agent.acp_backend', ACP_BACKEND_KAS)
        await api.patchConfig('agent.kas_bundle', pick === BUILT_IN)
      } else {
        await api.patchConfig('agent.acp_backend', pick)
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      void qc.invalidateQueries({ queryKey: ['kas-bundle'] })
      // The model list belongs to the harness just saved, as on the Settings switch:
      // drop the last-good list first, then the query, so no old harness's ids remain.
      clearCachedModels()
      void qc.resetQueries({ queryKey: ['available-models'] })
      void qc.resetQueries({ queryKey: ['kiro-usage'] })
    },
  })
  const recheckCli = useMutation({
    // A poll still in flight must not land after this answer and undo it.
    onMutate: () => qc.cancelQueries({ queryKey: KIRO_PREREQUISITE_KEY }),
    mutationFn: () => api.kiroPrerequisite('explicit'),
    onSuccess: data => qc.setQueryData(KIRO_PREREQUISITE_KEY, data),
  })
  // An other harness installed after the list was read: re-probe that one row,
  // the way the Settings list's Check again does.
  const recheckOther = useMutation({
    // As in Settings: cancel the list read first, so a late GET cannot overwrite the re-probed row.
    onMutate: () => qc.cancelQueries({ queryKey: ['acpBackends'] }),
    mutationFn: (id: string) => api.acpBackendRecheck(id),
    onSuccess: ({ backend }) => applyRecheckedBackend(qc, backend),
  })
  const recheckBusy = recheckCli.isPending || recheckOther.isPending
  // The harness list has its own notice in "Other harnesses"; these three fail open.
  const setupReadFailed = bundleQ.isError || cfgQ.isError || !!schemaError
  const retryReads = () => {
    void bundleQ.refetch()
    void cfgQ.refetch()
    void qc.refetchQueries({ queryKey: ['config-schema'] })
  }
  const recheckError = recheckCli.error ?? recheckOther.error
  const recheck = () => {
    recheckCli.reset()
    recheckOther.reset()
    if (pick === KIRO_CLI) recheckCli.mutate()
    else if (otherProbe) recheckOther.mutate(otherProbe.id)
  }

  // Advances only once both keys are saved; a failed save stays on this screen with its error.
  const next = async () => {
    try {
      await saveMut.mutateAsync()
    } catch {
      return
    }
    if (pick === BUILT_IN) setScreen(crewSignedIn ? 'ready' : 'sign-in')
    else if (pick === KIRO_CLI) setScreen(cliSignedIn ? 'ready' : 'kiro-cli-sign-in')
    else setScreen('ready')
  }
  // The pick is fixed while it is being saved, so the screens after it name what was saved.
  const choose = (id: string) => {
    if (saveMut.isPending) return
    saveMut.reset()
    recheckCli.reset()
    recheckOther.reset()
    setPicked(id)
  }

  const aside = {
    asideHeadline: i18nT('components.firstRunHarnessSetup.aside_headline'),
    asideBody: i18nT('components.firstRunHarnessSetup.aside_body'),
    cardLabel: i18nT('components.firstRunHarnessSetup.aside_headline'),
  }
  // The device-code screen hands its own back (to the sign-in choices) to this
  // footer, the way the sign-in dialog takes it into its header: one Back, not two.
  const [innerBack, setInnerBack] = useState<{ onBack: () => void; disabled: boolean } | null>(null)
  const backSlot = useMemo<SignInBackSlot>(() => ({ set: setInnerBack }), [])
  // One centered column, as the Kiro account page lays out its sign-in: the
  // ghost and title sit centered over the buttons. #36888: Back stays an arrow
  // at the top left of the sign-in box, outside the centered title; on the
  // code screen it returns to the sign-in choices.
  const header = (title: string) => (
    <div className="relative flex flex-col items-center text-center" data-testid="harness-setup-hero">
      <IconButton
        aria-label={i18nT('components.firstRunHarnessSetup.back')}
        disabled={screen === 'sign-in' && innerBack?.disabled}
        onClick={() => (screen === 'sign-in' && innerBack ? innerBack.onBack() : setScreen('choose'))}
        className="absolute -left-2 top-0"
        data-testid="harness-setup-back"
      >
        <ArrowLeft size={18} aria-hidden="true" />
      </IconButton>
      <LookingGhost size={72} />
      <h1 className="m-0 mt-4 max-w-full text-2xl font-semibold tracking-tight text-text-strong">{title}</h1>
    </div>
  )

  if (screen === 'sign-in') {
    return (
      <SetupShell {...aside}>
        {header(i18nT('components.kasLogin.sign_in_to_kiro'))}
        <div className="mt-5" data-testid="harness-setup-sign-in">
          <SignInBackContext.Provider value={backSlot}>
            <KiroSignInFlow onSignedIn={() => setScreen('ready')} />
          </SignInBackContext.Provider>
        </div>
      </SetupShell>
    )
  }

  if (screen === 'kiro-cli-sign-in' && status && !status.authenticated) {
    return (
      <SetupShell
        {...aside}
        footer={
          <div className="flex items-center justify-end gap-3">
            <KiroCliCheckAgain installed busy={recheckCli.isPending} onClick={recheck} />
          </div>
        }
      >
        {header(i18nT('components.firstRunHarnessSetup.kiro_cli_sign_in_title'))}
        <p className="mt-2 text-center text-sm text-muted">{i18nT('components.firstRunHarnessSetup.kiro_cli_sign_in_body')}</p>
        <div className="mt-4"><SignInCommands status={status} compact /></div>
        <ErrorNotice
          testId="harness-setup-recheck-error"
          className="mt-4"
          noHandoffReason="The harness just saved is Kiro CLI and it is not signed in, so an agent opened from this notice could not run."
          message={recheckCli.error ? i18nT('components.kiroPrerequisiteGate.could_not_check_kiro_cli') : null}
        />
      </SetupShell>
    )
  }

  // A kiro-cli sign-in that Check again reads as done lands on "ready".
  // Kiro CLI picked here is the person's own choice, so "ready" offers no Kiro
  // Crew sign-in on top of it: that prompt is for people left on kiro-cli by default.
  if (screen === 'ready' || screen === 'kiro-cli-sign-in') {
    // A harness that signs in on its own is not ready until that sign-in is done,
    // which Kiro Crew cannot check: say what is left instead of "all set".
    const ownSignIn = !!(otherProbe?.auth?.signs_in_separately && otherProbe.auth.sign_in_remedy)
    return (
      <SetupShell
        {...aside}
        footer={
          <div className="flex justify-end">
            <SendBtn type="button" onClick={onDone} data-testid="harness-setup-start">
              {i18nT('components.firstRunHarnessSetup.start_chatting')}
            </SendBtn>
          </div>
        }
      >
        <div data-testid="harness-setup-ready">
          {ownSignIn ? (
            <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-bg-hover text-muted">
              <CircleDashed className="lucide-inline" aria-hidden="true" />
            </div>
          ) : (
            <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-ok-subtle text-ok-text">
              <CheckCircle2 className="lucide-inline" aria-hidden="true" />
            </div>
          )}
          <h1 className="mt-5 text-2xl font-semibold tracking-tight text-text-strong">
            {ownSignIn
              ? i18nT('components.firstRunHarnessSetup.one_more_step')
              : i18nT('components.firstRunHarnessSetup.ready_title')}
          </h1>
          <p className="mt-2 text-sm text-muted">
            {i18nT('components.firstRunHarnessSetup.ready_body', { name: pickName })}
          </p>
          {ownSignIn ? (
            // One line here; the server's full sign-in sentence stays in Settings.
            <div className="mt-3 flex flex-wrap items-center gap-3" data-testid="harness-setup-own-sign-in">
              <p className="m-0 text-sm text-text">
                {i18nT('components.firstRunHarnessSetup.own_sign_in', { name: pickName })}
              </p>
              <Btn
                type="button"
                onClick={() => { onDone(); navigate('/settings/agent') }}
                data-testid="harness-setup-open-settings"
              >
                {i18nT('components.firstRunHarnessSetup.open_settings')}
              </Btn>
            </div>
          ) : null}
        </div>
      </SetupShell>
    )
  }

  // ── choose ──
  // Kiro CLI is always listed under the built-in harness; missing, it shows how to install it.
  const kiroIds = [kasAllowed ? BUILT_IN : '', kiroCliAllowed ? KIRO_CLI : ''].filter(Boolean)
  const kiroProbe: AcpBackendProbe = list.probe(KIRO_CLI)
    ?? { id: KIRO_CLI, policy_id: KIRO_CLI, selectable: true, independent_setup: false, installed: 'missing',
      missing_components: [], install_command: '', restart_required: false }
  const nothingKiro = !builtInUsable && !(cliInstalled && kiroCliAllowed)
  // A picked harness that is not usable yet re-reads in its own detail, after its
  // install steps, so the footer keeps its two actions.
  const checkAgain = (
    <div className="mt-3">
      <Btn
        type="button"
        className="inline-flex items-center gap-1.5"
        disabled={recheckBusy || saveMut.isPending}
        onClick={recheck}
        data-testid="harness-setup-check-again"
      >
        <RefreshCw className={`lucide-inline ${recheckBusy ? 'animate-spin' : ''}`} aria-hidden="true" />
        {i18nT('components.kiroPrerequisiteGate.check_again')}
      </Btn>
    </div>
  )
  return (
    <SetupShell
      {...aside}
      footer={
        <div className="flex items-center justify-between gap-3">
          <Btn type="button" onClick={onDone} data-testid="harness-setup-later">
            {i18nT('components.firstRunHarnessSetup.set_up_later')}
          </Btn>
          <SendBtn type="button" disabled={!usable || saveMut.isPending} onClick={() => void next()} data-testid="harness-setup-continue">
            {i18nT('components.firstRunHarnessSetup.continue')}
          </SendBtn>
        </div>
      }
    >
      <h1 className="text-2xl font-semibold tracking-tight text-text-strong">
        {i18nT('components.firstRunHarnessSetup.choose_title')}
      </h1>
      {nothingKiro ? (
        <p className="mt-2 text-sm text-muted" data-testid="harness-setup-none">
          {i18nT('components.firstRunHarnessSetup.no_kiro_harness')}
        </p>
      ) : null}
      {/* No hand-off: the harness pick on this screen is unsaved. These reads fail
          open (the choices still show), so this only says some may be missing. */}
      <ErrorNotice
        testId="harness-setup-read-error"
        className="mt-4"
        message={setupReadFailed ? i18nT('components.firstRunHarnessSetup.setup_read_failed') : null}
        footer={setupReadFailed ? (
          <Btn type="button" onClick={retryReads}>{i18nT('components.kiroPrerequisiteGate.try_again')}</Btn>
        ) : undefined}
      />
      <HarnessPickerList
        className="mx-0 mt-5 min-w-0 space-y-2 border-none p-0"
        ids={kiroIds}
        group="first-run-harness"
        legend={i18nT('components.firstRunHarnessSetup.choose_title')}
        selectedId={pick}
        onSelect={choose}
        label={id => (id === BUILT_IN
          ? i18nT('pages.developer.kasBundleCard.engine_builtin')
          : i18nT('pages.developer.kasBundleCard.engine_kiro_cli'))}
        detailId={id => `first-run-harness-${id}`}
        detailTestId={id => `first-run-harness-detail-${id}`}
        row={id => ({
          icon: <HarnessLogo id={id === BUILT_IN ? ACP_BACKEND_KAS : KIRO_CLI} size={16} />,
          status: id === BUILT_IN
            ? (builtInOk
              ? <Badge variant="aim">{i18nT('components.firstRunHarnessSetup.recommended')}</Badge>
              : <Badge variant="muted">{i18nT('components.firstRunHarnessSetup.cannot_run_here')}</Badge>)
            : !cliInstalled
              ? <Badge variant="muted">{i18nT('components.kiroPrerequisiteGate.agent_not_installed')}</Badge>
              : cliSignedIn
                ? <Badge variant="ok">{i18nT('components.firstRunHarnessSetup.signed_in')}</Badge>
                : <Badge variant="muted">{i18nT('components.firstRunHarnessSetup.not_signed_in')}</Badge>,
          detail: id === BUILT_IN && !builtInOk
            ? <p className="text-[13px] leading-relaxed text-text">{bundle?.host_unsupported}</p>
            : id === KIRO_CLI && !cliInstalled
              ? (
                <>
                  <HarnessInstallDetail
                    value={KIRO_CLI}
                    name={i18nT('pages.developer.kasBundleCard.engine_kiro_cli')}
                    probe={kiroProbe}
                    prerequisite={status}
                  />
                  {checkAgain}
                </>
              )
              : undefined,
        })}
      />

      <div className="mt-4 min-w-0 rounded-xl border border-border" data-testid="harness-setup-others">
        <button
          type="button"
          aria-expanded={showOthers}
          aria-controls="first-run-other-harnesses"
          onClick={() => setOthersOpen(!showOthers)}
          className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-xl border-none bg-transparent px-3 py-2.5 text-left hover:bg-bg-hover focus-ring"
        >
          <span className="flex items-center gap-2 text-sm font-medium text-text-strong">
            <Boxes className="lucide-inline text-muted" aria-hidden="true" />
            {i18nT('components.firstRunHarnessSetup.other_harnesses')}
          </span>
          <ChevronDown
            aria-hidden="true"
            className={`h-5 w-5 shrink-0 text-muted transition-transform ${showOthers ? 'rotate-180' : ''}`}
          />
        </button>
        {/* Shown beside any rows still cached from an earlier read, so a failed refresh is never silent. */}
        {showOthers && backendsQ.isError ? (
          <div className="border-0 border-t border-solid border-border p-3">
            {/* No hand-off: the harness pick on this screen is unsaved. */}
            <ErrorNotice
              message={i18nT('components.kiroPrerequisiteGate.other_agents_unavailable')}
              testId="harness-setup-backends-error"
              footer={<Btn type="button" onClick={() => void backendsQ.refetch()}>{i18nT('components.kiroPrerequisiteGate.try_again')}</Btn>}
            />
          </div>
        ) : null}
        {showOthers && (
          <HarnessPickerList
            id="first-run-other-harnesses"
            className="mx-0 min-w-0 space-y-2 border-0 border-t border-solid border-border p-3"
            ids={others.map(b => b.id)}
            group="first-run-harness"
            legend={i18nT('components.firstRunHarnessSetup.other_harnesses')}
            selectedId={pick}
            onSelect={choose}
            label={list.nameOf}
            detailId={id => `first-run-harness-${id}`}
            detailTestId="first-run-harness-detail-other"
            row={id => {
              const b = list.probe(id) as AcpBackendProbe
              const reason = status
                ? notReadyReason(status, b) ?? (configuredBackendCanStart(status, b) ? null : 'sandbox_blocked')
                : null
              return {
                icon: <HarnessLogo id={id} size={16} />,
                status: (
                  <AgentStatusBadge
                    probe={b}
                    blocked={!configuredBackendCanStart(status, b)}
                    sandboxOff={sandboxOffFor(status, b)}
                  />
                ),
                // Only a blocking state gets a detail; the harness's own sign-in
                // sentence waits for the ready screen, where it is the next step.
                detail: reason ? <><AgentNotReadyDetail reason={reason} name={list.nameOf(id)} probe={b} />{checkAgain}</> : undefined,
              }
            }}
          />
        )}
      </div>
      {/* No hand-off: the harness pick on this screen is unsaved; Continue retries the save. */}
      <ErrorNotice
        testId="harness-setup-save-error"
        className="mt-4"
        message={saveMut.error ? i18nT('components.firstRunHarnessSetup.save_failed', { name: pickName }) : null}
      />
      {/* No hand-off: the harness pick on this screen is unsaved; Check again retries the re-read. */}
      <ErrorNotice
        testId="harness-setup-recheck-error"
        className="mt-4"
        message={recheckError ? i18nT('components.firstRunHarnessSetup.recheck_failed', { name: pickName }) : null}
      />
    </SetupShell>
  )
}
