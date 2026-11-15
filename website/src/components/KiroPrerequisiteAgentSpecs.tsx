import { AlertTriangle, RefreshCw } from 'lucide-react'
import { Btn } from './ui'
import ErrorNotice from './ErrorNotice'
import { SetupShell } from './KiroPrerequisiteSetupShell'
import { CopyCommand } from './agentHarness'
import { i18nT } from '../i18n/t'

// The two setup screens KiroPrerequisiteGate shows when Kiro CLI cannot use Kiro
// Crew's agent specs: the files are missing, or Kiro CLI refuses to load them.

export function AgentSpecsMissing({
  specs,
  repairError,
  retrying,
  onRepair,
}: {
  specs: string[]
  repairError: string
  retrying: boolean
  onRepair: () => void
}) {
  return (
    <SetupShell
      asideHeadline={i18nT('components.kiroPrerequisiteGate.agent_specs_missing')}
      asideBody={i18nT('components.kiroPrerequisiteGate.kiro_crew_installs_the_agent_specs_kiro_cli_load')}
    >
      <>
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-danger/10 text-danger-text">
          <AlertTriangle className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-danger">
          {i18nT('components.kiroPrerequisiteGate.agent_specs_missing')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kiroPrerequisiteGate.kiro_crew_s_agent_specs_are_not_installed')}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.kiro_crew_writes_its_own_agent_specs_where_kiro')}
        </p>
        <div className="mt-5 w-full max-w-lg text-left">
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
            {i18nT('components.kiroPrerequisiteGate.missing')}
          </p>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-bg p-3 text-xs text-muted">
            {specs.join('\n')}
          </pre>
        </div>
        {/* Verbatim and untranslated: it names the failing install step, which is
            the one thing a support conversation actually needs. Its absence is
            also informative — it means no repair has been attempted yet.
            `role="alert"` because it appears in place after the button press with
            no route change, so a screen reader would otherwise get nothing. */}
        <ErrorNotice
          className="mt-4 w-full max-w-lg text-left text-xs"
          messageClassName="font-mono"
          title={i18nT('components.kiroPrerequisiteGate.the_repair_attempt_failed')}
          message={repairError || null}
          testId="kiro-gate-repair-error"
          noHandoffReason="the agent specs kiro-cli refused are what the agent runs on, and this gate hides the chat the hand-off would open"
        />
        {/* Plain-language next step: the verbatim output above is for a bug
            report, not something a user can act on by itself. */}
        {repairError ? (
          <p className="mt-2 max-w-lg text-left text-[13px] leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.attempt_failed_remedy', { action: i18nT('components.kiroPrerequisiteGate.check_again') })}
          </p>
        ) : null}
        {/* The self-diagnosis dead end: `kiro-cli diagnostic` is the first command
            anyone reaches for, and it refuses with "Kiro CLI app is not running"
            until the app is launched — which reads as the cause and is not. */}
        <p className="mt-5 max-w-lg text-[13px] leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.if_you_are_diagnosing_this_from_a_terminal_kiro')}
        </p>
        <div className="mt-6">
          <Btn type="button" disabled={retrying} onClick={onRepair}>
            <RefreshCw className="lucide-inline" />
            {i18nT('components.kiroPrerequisiteGate.check_again')}
          </Btn>
        </div>
      </>
    </SetupShell>
  )
}

export function AgentSpecsRejected({
  specs,
  reason,
  repairError,
  retrying,
  onRepair,
}: {
  specs: string[]
  reason: string
  repairError: string
  retrying: boolean
  onRepair: () => void
}) {
  return (
    <SetupShell
      asideHeadline={i18nT('components.kiroPrerequisiteGate.agent_specs_rejected')}
      asideBody={i18nT('components.kiroPrerequisiteGate.kiro_crew_installs_the_agent_specs_kiro_cli_load')}
    >
      <>
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-danger/10 text-danger-text">
          <AlertTriangle className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-danger">
          {i18nT('components.kiroPrerequisiteGate.agent_specs_rejected')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kiroPrerequisiteGate.kiro_cli_will_not_load_kiro_crew_s_agent_specs')}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.the_files_are_on_disk_but_kiro_cli_refuses_them')}
        </p>
        <div className="mt-5 w-full max-w-lg text-left">
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
            {i18nT('components.kiroPrerequisiteGate.rejected')}
          </p>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-bg p-3 text-xs text-muted">
            {specs.join('\n')}
          </pre>
        </div>
        {/* Kiro CLI's own words, verbatim and untranslated. It names the file and
            the construct it refused, which is the difference between "my agents
            stopped working" and a report someone can act on. */}
        {reason ? (
          <div className="mt-4 w-full max-w-lg text-left">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
              {i18nT('components.kiroPrerequisiteGate.kiro_cli_s_reason')}
            </p>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-bg p-3 text-xs text-muted">
              {reason}
            </pre>
          </div>
        ) : null}
        <ErrorNotice
          className="mt-4 w-full max-w-lg text-left text-xs"
          messageClassName="font-mono"
          title={i18nT('components.kiroPrerequisiteGate.the_repair_attempt_failed')}
          message={repairError || null}
          testId="kiro-gate-repair-error"
          noHandoffReason="the agent specs kiro-cli refused are what the agent runs on, and this gate hides the chat the hand-off would open"
        />
        {/* Plain-language next step: the verbatim output above is for a bug
            report, not something a user can act on by itself. */}
        {repairError ? (
          <p className="mt-2 max-w-lg text-left text-[13px] leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.attempt_failed_remedy', { action: i18nT('components.kiroPrerequisiteGate.check_again') })}
          </p>
        ) : null}
        {/* Deliberately does not promise a rewrite. The button re-asks kiro-cli
            rather than regenerating the spec: the file is already on disk, and a
            rebuild would discard a concurrent MCP toggle's tools/allowedTools
            grant. The leading cause is a kiro-cli upgrade, which re-checking
            cannot fix, so the two remedies get their own labelled lines rather
            than sitting mid-sentence in a muted paragraph. The commands live in
            <code> outside the catalog: a translator must not be able to alter a
            string the user pastes into a shell, and prose cannot be copied. */}
        <p className="mt-4 max-w-lg text-[13px] leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.repair_rewrites_the_specs_kiro_crew_owns')}
        </p>
        <ul className="mt-3 w-full max-w-lg list-none space-y-2 text-left">
          <li className="text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_spec_rejected_update')}
          </li>
          <li className="text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_spec_rejected_rewrite')}
            <CopyCommand>
              <code>kirocrew setup --agent-only --clean</code>
            </CopyCommand>
          </li>
        </ul>
        {/* Tightened deliberately: the remedy lines and the copy block added
            enough height to push this button under the fold at a 1280x800
            viewport, and the card's primary action must stay on screen. */}
        <div className="mt-4">
          <Btn type="button" disabled={retrying} onClick={onRepair}>
            <RefreshCw className="lucide-inline" />
            {i18nT('components.kiroPrerequisiteGate.check_again')}
          </Btn>
        </div>
      </>
    </SetupShell>
  )
}
