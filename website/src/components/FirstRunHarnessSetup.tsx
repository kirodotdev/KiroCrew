import { useMemo, useRef, useState } from 'react'
import { Trans } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, CircleDashed, RefreshCw } from 'lucide-react'

import { api, type AcpBackendProbe, type KasBundleStatus } from '../api/client'
import type { KiroPrerequisiteStatus } from '../api/client/onboarding'
import { ACP_BACKEND_KAS, configuredAcpBackend, DEFAULT_ACP_BACKEND, type AcpBackendConfig } from '../api/acpBackend'
import { useKasLoginStatus } from '../hooks/useKasLogin'
import { clearCachedModels } from '../providers/adapters/acp'
import { KIRO_PREREQUISITE_KEY } from '../hooks/useKiroPrerequisite'
import { i18nT } from '../i18n/t'
import { HarnessLogo } from './HarnessLogo'
import { providerLabelFromWire } from './kasProviderLabel'
import { KiroSignInFlow } from './KiroSignInDialog'
import { FirstRunConfetti } from './FirstRunConfetti'
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
type Screen = 'start' | 'harness' | 'kiro-cli-sign-in' | 'ready'

/**
 * First-run setup: sign in, or bring a harness, then "ready".
 *
 * Shown by `FirstRunSignInHold` only while the built-in engine is on. Built from
 * the pieces Settings > Agent Harness uses (`agentHarness/*`, the same
 * `/api/acp-backends` probe and the gate's own start verdict), the shared Kiro
 * sign-in (`KiroSignInFlow`) and the kiro-cli sign-in commands. Nothing here is
 * a second copy of those.
 *
 * - The first screen carries the shared Kiro sign-in choices inline; a sign-in
 *   that lands saves the built-in harness. Already signed in, one "Continue with
 *   Kiro" does the same. Kiro CLI installed and signed in adds one "Continue with
 *   Kiro CLI".
 * - "Bring your harness" says in one line what a Kiro sign-in alone gives (the
 *   rows of `KiroAccountUnlocks`), then lists every other harness, Kiro CLI first.
 *   Kiro CLI is always listed; missing, its row shows how to install it.
 * - A machine that cannot run the built-in harness, or a policy that denies it,
 *   opens straight on the harness list.
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
  // minus the two Kiro rows this step draws itself.
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

  // "Bring your harness" starts on Kiro CLI when it can run; otherwise nothing is picked.
  const harnessDefault = cliInstalled && kiroCliAllowed ? KIRO_CLI : ''
  const [picked, setPicked] = useState<string | null>(null)
  const pick = picked ?? harnessDefault
  // Without a usable built-in harness, "Sign in to Kiro" has nothing to save: open on the list.
  const [screenSet, setScreenSet] = useState<Screen | null>(null)
  const screen: Screen = screenSet ?? (builtInUsable ? 'start' : 'harness')
  // The harness last saved, so the screens after the save name what was saved.
  const [savedId, setSavedId] = useState('')

  const otherProbe: AcpBackendProbe | undefined = others.find(b => b.id === pick)
  const usable = pick === KIRO_CLI
    ? cliInstalled && kiroCliAllowed
    : !!otherProbe && configuredBackendCanStart(status, otherProbe)
  const nameOf = (id: string) => (id === BUILT_IN
    ? i18nT('pages.developer.kasBundleCard.engine_builtin')
    : id === KIRO_CLI
      ? i18nT('pages.developer.kasBundleCard.engine_kiro_cli')
      : others.some(b => b.id === id) ? list.nameOf(id) : '')
  const savedProbe = others.find(b => b.id === savedId)

  // The same two config keys Settings > Agent Harness writes.
  const saveMut = useMutation({
    mutationFn: async (id: string) => {
      if (id === BUILT_IN || (id === KIRO_CLI && kasAllowed)) {
        await api.patchConfig('agent.acp_backend', ACP_BACKEND_KAS)
        await api.patchConfig('agent.kas_bundle', id === BUILT_IN)
      } else {
        await api.patchConfig('agent.acp_backend', id)
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

  // No screen change while a save is in flight: its answer still lands, and a
  // second save from another screen could then be overwritten by it.
  const goTo = (next: Screen) => {
    if (saveMut.isPending) return
    saveMut.reset()
    recheckCli.reset()
    recheckOther.reset()
    setScreenSet(next)
  }
  // Advances only once both keys are saved; a failed save stays on its screen with its error.
  const save = async (id: string) => {
    // One save at a time, whoever asks (a button or a sign-in landing): a second
    // one could land after it and replace the harness the person just picked.
    if (saveMut.isPending) return
    try {
      await saveMut.mutateAsync(id)
    } catch {
      return
    }
    setSavedId(id)
    // The built-in harness is saved only once a Kiro sign-in is stored.
    if (id === BUILT_IN) setScreenSet('ready')
    else if (id === KIRO_CLI) setScreenSet(cliSignedIn ? 'ready' : 'kiro-cli-sign-in')
    else setScreenSet('ready')
  }
  // The pick is fixed while it is being saved, so the screens after it name what was saved.
  const choose = (id: string) => {
    if (saveMut.isPending) return
    saveMut.reset()
    recheckCli.reset()
    recheckOther.reset()
    setPicked(id)
  }
  // Names the button that retries: the first screen's Continue with Kiro (CLI), the list's Continue.
  const failedId = saveMut.variables ?? ''
  const saveError = saveMut.error
    ? <ErrorNotice
        testId="harness-setup-save-error"
        className="mt-4"
        noHandoffReason="This step holds the app behind it and no harness is saved yet, so an agent opened from this notice has no chat to open in and no harness it could start."
        message={screen === 'start'
          ? i18nT('components.firstRunHarnessSetup.sign_in_save_failed', {
            button: failedId === KIRO_CLI
              ? i18nT('components.firstRunHarnessSetup.continue_with_kiro_cli')
              : i18nT('components.firstRunHarnessSetup.continue_with_kiro'),
          })
          : i18nT('components.firstRunHarnessSetup.save_failed', { name: nameOf(failedId) })}
      />
    : null

  // Whether a Kiro sign-in was already stored when this step first read it. Only
  // then does the first screen offer "Continue with Kiro" instead of the choices,
  // so a sign-in made here never unmounts the flow before it reports.
  const crewStatusKnown = crewSignIn.data !== undefined || crewSignIn.isError
  const [signedInAtStart, setSignedInAtStart] = useState<boolean | null>(null)
  if (signedInAtStart === null && crewStatusKnown) setSignedInAtStart(crewSignedIn)
  // A sign-in that lands here saves the built-in harness once. From then on the
  // first screen offers "Continue with Kiro", so a failed save, or a Back after
  // one, still has a way to retry (the flow itself shows nothing once signed in).
  const [landed, setLanded] = useState(false)
  const landedOnce = useRef(false)
  const kiroContinue = landed || (crewSignedIn && signedInAtStart === true)
  const onKiroSignedIn = () => {
    if (landedOnce.current) return
    landedOnce.current = true
    setLanded(true)
    void save(BUILT_IN)
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
  // code screen it returns to the sign-in choices. The first screen has no Back.
  const backTo: Screen = screen === 'kiro-cli-sign-in' ? 'harness' : 'start'
  const codeStep = screen === 'start' && !!innerBack
  const header = (title: string, back = true) => (
    <div className="relative flex flex-col items-center text-center" data-testid="harness-setup-hero">
      {back || codeStep ? (
        <IconButton
          aria-label={i18nT('components.firstRunHarnessSetup.back')}
          disabled={codeStep ? innerBack?.disabled : saveMut.isPending}
          onClick={() => (codeStep && innerBack ? innerBack.onBack() : goTo(backTo))}
          className="absolute -left-2 top-0"
          data-testid="harness-setup-back"
        >
          <ArrowLeft size={18} aria-hidden="true" />
        </IconButton>
      ) : null}
      <LookingGhost size={72} />
      <h1 className="m-0 mt-4 max-w-full text-2xl font-semibold tracking-tight text-text-strong">{title}</h1>
    </div>
  )

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
    const ownSignIn = !!(savedProbe?.auth?.signs_in_separately && savedProbe.auth.sign_in_remedy)
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
        {ownSignIn ? (
          <div data-testid="harness-setup-ready">
            <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-bg-hover text-muted">
              <CircleDashed className="lucide-inline" aria-hidden="true" />
            </div>
            <h1 className="mt-5 text-2xl font-semibold tracking-tight text-text-strong">
              {i18nT('components.firstRunHarnessSetup.one_more_step')}
            </h1>
            <p className="mt-2 text-sm text-muted">
              {i18nT('components.firstRunHarnessSetup.ready_body', { name: nameOf(savedId) })}
            </p>
            {/* One line here; the server's full sign-in sentence stays in Settings. */}
            <div className="mt-3 flex flex-wrap items-center gap-3" data-testid="harness-setup-own-sign-in">
              <p className="m-0 text-sm text-text">
                {i18nT('components.firstRunHarnessSetup.own_sign_in', { name: nameOf(savedId) })}
              </p>
              <Btn
                type="button"
                onClick={() => { onDone(); navigate('/settings/agent') }}
                data-testid="harness-setup-open-settings"
              >
                {i18nT('components.firstRunHarnessSetup.open_settings')}
              </Btn>
            </div>
          </div>
        ) : (
          // All set: the ghost from the first screen, one burst of confetti, centered.
          <div className="flex flex-col items-center text-center" data-testid="harness-setup-ready">
            <FirstRunConfetti />
            <h1 className="m-0 mt-4 max-w-full text-2xl font-semibold tracking-tight text-text-strong">
              {i18nT('components.firstRunHarnessSetup.ready_title')}
            </h1>
            <p className="m-0 mt-2 text-sm text-muted">
              {i18nT('components.firstRunHarnessSetup.ready_body', { name: nameOf(savedId) })}
            </p>
          </div>
        )}
      </SetupShell>
    )
  }

  // ── start: two choices ──
  const later = (
    <Btn type="button" onClick={onDone} data-testid="harness-setup-later">
      {i18nT('components.firstRunHarnessSetup.set_up_later')}
    </Btn>
  )
  if (screen === 'start') {
    // Kiro CLI already usable here: one click keeps it, as the list's Continue would.
    const cliReady = cliSignedIn && kiroCliAllowed
    // The same look as the sign-in choices above them (ProviderButton, bare).
    const choiceClass = 'min-h-9 w-full justify-center rounded-lg px-3 text-[13px] font-medium'
    return (
      <SetupShell {...aside} footer={<div className="flex items-center justify-start gap-3">{later}</div>}>
        {/* On the code step the screen is only the sign-in, so its title says so. */}
        {header(codeStep
          ? i18nT('components.kasLogin.sign_in_to_kiro')
          : i18nT('components.firstRunHarnessSetup.start_title'), false)}
        <div className="mt-5 flex w-full flex-col gap-2" data-testid="harness-setup-start-choices">
          {kiroContinue ? (
            <>
            {/* Says why there are no sign-in choices: the account this saves the built-in harness for. */}
            {crewSignIn.data?.provider ? (
              <p className="m-0 mb-1 text-center text-sm text-muted" data-testid="harness-setup-signed-in-as">
                {i18nT('app.signed_in_with', { provider: providerLabelFromWire(crewSignIn.data.provider) })}
              </p>
            ) : null}
            <SendBtn
              type="button"
              className="w-full"
              busy={saveMut.isPending}
              onClick={() => void save(BUILT_IN)}
              data-testid="harness-setup-continue-with-kiro"
            >
              {i18nT('components.firstRunHarnessSetup.continue_with_kiro')}
            </SendBtn>
            </>
          ) : crewStatusKnown ? (
            <div data-testid="harness-setup-sign-in">
              <SignInBackContext.Provider value={backSlot}>
                <KiroSignInFlow onSignedIn={onKiroSignedIn} />
              </SignInBackContext.Provider>
            </div>
          ) : null}
          {codeStep ? null : (
            <>
              <div className="my-1 flex items-center gap-3 text-[12px] text-muted">
                <Trans
                  i18nKey="components.kasLogin.or_divider"
                  components={{ line: <span className="h-px flex-1 bg-border" aria-hidden="true" />, or: <span /> }}
                />
              </div>
              {cliReady ? (
                <Btn
                  type="button"
                  className={choiceClass}
                  disabled={saveMut.isPending}
                  onClick={() => void save(KIRO_CLI)}
                  data-testid="harness-setup-continue-with-kiro-cli"
                >
                  <HarnessLogo id={KIRO_CLI} size={16} />
                  {i18nT('components.firstRunHarnessSetup.continue_with_kiro_cli')}
                </Btn>
              ) : null}
              <Btn
                type="button"
                className={choiceClass}
                disabled={saveMut.isPending}
                onClick={() => goTo('harness')}
                data-testid="harness-setup-bring-your-harness"
              >
                {i18nT('components.firstRunHarnessSetup.bring_your_harness')}
              </Btn>
            </>
          )}
          <ErrorNotice
            testId="harness-setup-read-error"
            className="mt-1"
            noHandoffReason="This step holds the app behind it and no harness is saved yet, so an agent opened from this notice has no chat to open in and no harness it could start."
            message={setupReadFailed ? i18nT('components.firstRunHarnessSetup.setup_read_failed') : null}
            footer={setupReadFailed ? (
              <Btn type="button" onClick={retryReads}>{i18nT('components.kiroPrerequisiteGate.try_again')}</Btn>
            ) : undefined}
          />
          {saveError}
        </div>
      </SetupShell>
    )
  }

  // ── harness: Bring your harness ──
  // Kiro CLI is always listed first; missing, it shows how to install it.
  const ids = [kiroCliAllowed ? KIRO_CLI : '', ...others.map(b => b.id)].filter(Boolean)
  const kiroProbe: AcpBackendProbe = list.probe(KIRO_CLI)
    ?? { id: KIRO_CLI, policy_id: KIRO_CLI, selectable: true, independent_setup: false, installed: 'missing',
      missing_components: [], install_command: '', restart_required: false }
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
  const title = i18nT('components.firstRunHarnessSetup.bring_your_harness')
  return (
    <SetupShell
      {...aside}
      footer={
        <div className="flex items-center justify-between gap-3">
          {later}
          <SendBtn type="button" disabled={!usable || saveMut.isPending} onClick={() => void save(pick)} data-testid="harness-setup-continue">
            {i18nT('components.firstRunHarnessSetup.continue')}
          </SendBtn>
        </div>
      }
    >
      <div className="flex items-center gap-2">
        {builtInUsable ? (
          <IconButton
            aria-label={i18nT('components.firstRunHarnessSetup.back')}
            disabled={saveMut.isPending}
            onClick={() => goTo('start')}
            className="-ml-2"
            data-testid="harness-setup-back"
          >
            <ArrowLeft size={18} aria-hidden="true" />
          </IconButton>
        ) : null}
        <h1 className="m-0 text-2xl font-semibold tracking-tight text-text-strong">{title}</h1>
      </div>
      {/* What a Kiro sign-in alone gives, or why the built-in harness is not offered here. */}
      <p className="mt-2 text-sm text-muted" data-testid="harness-setup-without-sign-in">
        {builtInUsable
          ? i18nT('components.firstRunHarnessSetup.without_sign_in')
          : bundle?.host_unsupported || i18nT('components.firstRunHarnessSetup.without_sign_in')}
      </p>
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
      {/* Shown beside any rows still cached from an earlier read, so a failed refresh is never silent. */}
      {backendsQ.isError ? (
        // No hand-off: the harness pick on this screen is unsaved.
        <ErrorNotice
          className="mt-4"
          message={i18nT('components.kiroPrerequisiteGate.other_agents_unavailable')}
          testId="harness-setup-backends-error"
          footer={<Btn type="button" onClick={() => void backendsQ.refetch()}>{i18nT('components.kiroPrerequisiteGate.try_again')}</Btn>}
        />
      ) : null}
      <HarnessPickerList
        className="mx-0 mt-5 min-w-0 space-y-2 border-none p-0"
        ids={ids}
        group="first-run-harness"
        legend={title}
        selectedId={pick}
        onSelect={choose}
        label={nameOf}
        detailId={id => `first-run-harness-${id}`}
        detailTestId={id => (id === KIRO_CLI ? `first-run-harness-detail-${id}` : 'first-run-harness-detail-other')}
        row={id => {
          if (id === KIRO_CLI) {
            return {
              icon: <HarnessLogo id={KIRO_CLI} size={16} />,
              status: !cliInstalled
                ? <Badge variant="muted">{i18nT('components.kiroPrerequisiteGate.agent_not_installed')}</Badge>
                : cliSignedIn
                  ? <Badge variant="ok">{i18nT('components.firstRunHarnessSetup.signed_in')}</Badge>
                  : <Badge variant="muted">{i18nT('components.firstRunHarnessSetup.not_signed_in')}</Badge>,
              detail: !cliInstalled
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
            }
          }
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
      {saveError}
      {/* No hand-off: the harness pick on this screen is unsaved; Check again retries the re-read. */}
      <ErrorNotice
        testId="harness-setup-recheck-error"
        className="mt-4"
        message={recheckError ? i18nT('components.firstRunHarnessSetup.recheck_failed', { name: nameOf(pick) }) : null}
      />
    </SetupShell>
  )
}
