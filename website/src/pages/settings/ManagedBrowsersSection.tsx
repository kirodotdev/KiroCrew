import { useId, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  Download,
  Globe,
  HardDriveDownload,
  Loader2,
  RotateCcw,
  Server,
} from 'lucide-react'
import { Trans } from 'react-i18next'

import type { BrowserEngine, BrowserInstallData } from '../../api/client'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { Badge, Btn, EmptyState } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { copyToClipboard } from '../../utils/clipboard'
import { engineLabel } from './BrowserInstallStatus'
import {
  BROWSER_ENGINES,
  activityIsCliSetup,
  engineRowState,
  type InstallActivity,
} from './browserInstallState'

/** The three steps `install()` runs, named so the wait is legible rather than blank. */
const INSTALL_STEP_KEYS = [
  'pages.settings.browserPanel.step_npm',
  'pages.settings.browserPanel.step_browser',
  'pages.settings.browserPanel.step_skills',
] as const

/**
 * "Managed browsers on the Kiro Crew host": the Playwright CLI and the browser
 * builds it downloads, all of which land on the machine running the gateway.
 *
 * The CLI and each engine are separate facts with separate rows. Whether the CLI
 * is installed says nothing about whether a browser has finished downloading, and
 * a downloaded engine is a file on disk, not a launch that was checked.
 */
export function ManagedBrowsersSection({
  data,
  hostName,
  hostError,
  activity,
  installBlockText,
  engineBlockText,
  onInstallCli,
  onDownload,
  cliRequestError,
  engineRequestError,
  allowHandoff,
}: {
  data: BrowserInstallData
  /** The gateway's own hostname when the dashboard can read it; never guessed
   *  from the device showing this page. */
  hostName: string | null
  /** Why the hostname could not be read, or `null` when the read did not fail. */
  hostError: string | null
  activity: InstallActivity | null
  /** Why the CLI install is unavailable, or `null` when it is available. */
  installBlockText: string | null
  /** Why engine downloads are unavailable, or `null` when they are available. */
  engineBlockText: string | null
  onInstallCli: () => void
  onDownload: (engine: BrowserEngine) => void
  cliRequestError: string | null
  engineRequestError: { engine: BrowserEngine; message: string } | null
  allowHandoff: boolean
}) {
  const cliReasonId = useId()
  const engineReasonId = useId()
  const unknownReasonId = useId()
  // Latched, not timed out: the label is a confirmation that the paste is ready.
  const [installCmdCopied, setInstallCmdCopied] = useState(false)
  // Separate from `!installCmdCopied`: idle and failed both read as not-copied,
  // but only the failure needs a notice (same split as MobileLoginCard).
  const [installCmdCopyFailed, setInstallCmdCopyFailed] = useState(false)

  // Node is the one prerequisite an install cannot supply for the operator, so a
  // too-old runtime is reported instead of offering a button that would fail.
  const blockedByNode = !data.node_ok
  // Bound once so the render, the copy handler and the guard share one narrowed
  // value.
  const installCommand = data.standalone_install
  const cliRunning = activityIsCliSetup(activity)
  // The CLI binary lands before the browser and skills steps finish, so mid-setup
  // `installed` flips true while the status region still says "setting up". The
  // row follows the job, not the binary: it claims "installed" only once setup
  // is no longer running, and shows the in-progress control until then.
  const cliInstalled = data.installed && !cliRunning
  // The CLI card shows the install-block reason under its button only while the
  // CLI is not installed and Node is not the blocker. When the engine rows are
  // blocked for the same reason (CLI setup running: both texts are one string),
  // the sentence is said once, there, and every engine button is described by
  // that element instead of a second copy under the rows.
  const cliReasonVisible = !cliInstalled && !blockedByNode && installBlockText !== null
  const engineReasonShared = cliReasonVisible && engineBlockText === installBlockText
  const engineStates = Object.fromEntries(
    BROWSER_ENGINES.map((engine) => [engine, engineRowState(data, engine, activity)]),
  ) as Record<BrowserEngine, ReturnType<typeof engineRowState>>
  // One "could not confirm" sentence for the whole list, not one per row.
  const anyUnknown = BROWSER_ENGINES.some((engine) => engineStates[engine] === 'unknown')

  return (
    <SettingsSection
      title={i18nT('pages.settings.browserPanel.managed_title')}
      badge={
        hostName ? (
          <Badge variant="muted" data-testid="browser-host-label">
            {i18nT('pages.settings.browserPanel.host_label', { host: hostName })}
          </Badge>
        ) : undefined
      }
    >
      {/* Which machine is affected, said once and up front: the page may be open
          on a different device from the one the downloads land on. */}
      <SettingsCard>
        <div className="flex items-start gap-3">
          <Server size={18} className="text-muted shrink-0 mt-[2px]" />
          <div className="min-w-0 flex-1 flex flex-col gap-1.5">
            <p className="text-[13px] m-0">{i18nT('pages.settings.browserPanel.managed_explains')}</p>
            <p className="text-[13px] text-muted m-0">{i18nT('pages.settings.browserPanel.managed_host_note')}</p>
            {/* The host badge's failure, where the badge would have said which
                machine this is. No hand-off: the extension token draft shares
                this panel, and the navigation unmounts it. */}
            <ErrorNotice variant="inline" message={hostError} />
          </div>
        </div>
      </SettingsCard>

      <SettingsCard>
        {cliInstalled ? (
          <div className="flex items-start gap-3" data-testid="browser-cli-installed">
            <CheckCircle2 size={18} className="text-ok shrink-0 mt-[2px]" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-medium">{i18nT('pages.settings.browserPanel.cli_installed')}</span>
                {data.cli_version && <Badge variant="ok">{data.cli_version}</Badge>}
              </div>
              <p className="text-[13px] text-muted mt-1.5 mb-0">
                {i18nT('pages.settings.browserPanel.presence_is_consent')}
              </p>
            </div>
          </div>
        ) : (
          <>
            <EmptyState
              testId="browser-not-installed"
              icon={<Globe />}
              title={i18nT('pages.settings.browserPanel.not_installed')}
              subtitle={i18nT('pages.settings.browserPanel.install_explains')}
              action={
                blockedByNode ? (
                  /* Absent and too old are different problems with the same fix,
                     and the fix has to be reachable from here. */
                  <div className="flex flex-col items-center gap-1.5 text-[13px]">
                    <div className="flex items-center gap-2 text-warn">
                      <AlertTriangle size={14} className="shrink-0" />
                      {data.node_version
                        ? i18nT('pages.settings.browserPanel.needs_node', { version: data.node_version })
                        : i18nT('pages.settings.browserPanel.node_missing')}
                    </div>
                    <div className="text-muted text-center max-w-[340px]">
                      {/* ONE key with the link interpolated as <dl>: two joined keys
                          would pin every locale to English word order. */}
                      <Trans
                        i18nKey="pages.settings.browserPanel.node_how"
                        components={{
                          dl: (
                            // eslint-disable-next-line jsx-a11y/anchor-has-content, jsx-a11y/control-has-associated-label -- <Trans> substitutes this element for the <dl> run inside the `node_how` catalog value and supplies its children from that run, so the rendered anchor always carries the localized link text; an aria-label here would instead override it
                            <a
                              href="https://nodejs.org/en/download"
                              target="_blank"
                              rel="noreferrer"
                              className="text-accent hover:underline"
                            />
                          ),
                        }}
                      />
                    </div>
                    {/* The no-admin installer, for a machine where Node cannot be
                        installed. The command comes from the GATEWAY, which knows
                        its own OS; this page may be open on another machine. */}
                    {installCommand && (
                      <div className="text-muted text-left max-w-[340px] mt-1">
                        {i18nT('pages.settings.browserPanel.node_no_admin')}
                        <pre className="mt-1.5 mb-1 whitespace-pre-wrap break-all text-[12px] bg-bg-elevated rounded px-2 py-1.5">
                          <code>{installCommand}</code>
                        </pre>
                        {/* Awaited before the label flips: the Clipboard API is
                            absent on a plain-HTTP remote gateway, and "Copied"
                            must not promise a paste that is not there. */}
                        <Btn
                          onClick={async () => {
                            let ok = false
                            try {
                              ok = await copyToClipboard(installCommand)
                            } catch {
                              ok = false
                            }
                            setInstallCmdCopied(ok)
                            setInstallCmdCopyFailed(!ok)
                          }}
                        >
                          <Copy size={13} className="lucide-inline" />{' '}
                          {installCmdCopied
                            ? i18nT('pages.settings.browserPanel.copied')
                            : i18nT('pages.settings.browserPanel.copy_command')}
                        </Btn>
                        {/* Hand-off only without a token draft: the command stays
                            on screen to select by hand either way. */}
                        {installCmdCopyFailed && (
                          <ErrorNotice
                            variant="inline"
                            className="mt-1.5"
                            message={i18nT('pages.settings.browserPanel.copy_failed')}
                            askAgent={allowHandoff}
                          />
                        )}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-col items-center gap-1.5">
                    <Btn
                      primary
                      onClick={onInstallCli}
                      disabled={installBlockText !== null}
                      aria-busy={cliRunning}
                      aria-describedby={installBlockText ? cliReasonId : undefined}
                    >
                      {cliRunning ? (
                        <>
                          <Loader2 size={14} className="lucide-inline animate-spin" />
                          {i18nT('pages.settings.browserPanel.installing')}
                        </>
                      ) : (
                        <>
                          <Download size={14} className="lucide-inline" />
                          {i18nT('pages.settings.browserPanel.install')}
                        </>
                      )}
                    </Btn>
                    {installBlockText && (
                      <p id={cliReasonId} className="text-[13px] text-muted text-center m-0 max-w-[340px]">
                        {installBlockText}
                      </p>
                    )}
                  </div>
                )
              }
            />
            {/* The POST itself was refused, as opposed to a run that started and
                failed (the status region carries that). No hand-off while a
                token draft could be lost to the navigation. */}
            {cliRequestError && (
              <ErrorNotice
                className="mt-3"
                message={cliRequestError}
                askAgent={allowHandoff}
              />
            )}
            {/* Said before the click, so pressing the button is an informed
                choice; the running stages live in the status region. */}
            {!blockedByNode && !activity && (
              <div className="border-t border-border pt-3 mt-1">
                <div className="text-[13px] text-muted mb-1.5">
                  {i18nT('pages.settings.browserPanel.install_steps_intro')}
                </div>
                <ol className="text-[13px] text-muted/80 m-0 pl-5 list-decimal flex flex-col gap-1">
                  {INSTALL_STEP_KEYS.map((key) => (
                    <li key={key}>{i18nT(key)}</li>
                  ))}
                </ol>
              </div>
            )}
          </>
        )}
      </SettingsCard>

      {/* One row per engine. The CLI picks the engine per command, so what is
          configured here is only "is it downloaded", never which one a session
          uses. */}
      <SettingsCard>
        <div className="flex items-start gap-3">
          <HardDriveDownload size={18} className="text-muted shrink-0 mt-[2px]" />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium">{i18nT('pages.settings.browserPanel.engines_heading')}</div>
            <p className="text-[13px] text-muted mt-1 mb-2.5">
              {i18nT('pages.settings.browserPanel.engines_help')}
            </p>
            <div className="flex flex-col gap-2">
              {BROWSER_ENGINES.map((engine) => {
                const state = engineStates[engine]
                // The list's shared unknown reason first, then the block reason
                // (under the CLI button when it is the same sentence, under this
                // list otherwise), in the order assistive tech reads them.
                const unknownId = state === 'unknown' ? unknownReasonId : null
                const blockId = engineBlockText ? (engineReasonShared ? cliReasonId : engineReasonId) : null
                const describedBy = [unknownId, blockId].filter(Boolean).join(' ') || undefined
                const requestError = engineRequestError?.engine === engine ? engineRequestError.message : null
                return (
                  <div key={engine} className="flex flex-col gap-1">
                    <div
                      className="flex items-center gap-2 justify-between border border-border rounded-md px-3 py-2"
                      data-testid={`browser-engine-${engine}`}
                      data-state={state}
                    >
                      <div className="flex items-center gap-2 min-w-0 flex-wrap">
                        <span className="text-[13px] font-medium">{engineLabel(engine)}</span>
                        {engine === 'chromium' && (
                          <Badge variant="muted">{i18nT('pages.settings.browserPanel.engine_default_badge')}</Badge>
                        )}
                      </div>
                      {state === 'downloaded' ? (
                        <span className="inline-flex items-center gap-1.5 text-[13px] text-ok shrink-0">
                          <CheckCircle2 size={14} />
                          {i18nT('pages.settings.browserPanel.engine_downloaded')}
                        </span>
                      ) : state === 'downloading' ? (
                        <Btn disabled aria-busy aria-describedby={describedBy}>
                          <Loader2 size={13} className="lucide-inline animate-spin" />
                          {i18nT('pages.settings.browserPanel.engine_downloading')}
                        </Btn>
                      ) : (
                        <div className="flex items-center gap-2 shrink-0">
                          {state === 'unknown' && (
                            <span className="text-[13px] text-muted">
                              {i18nT('pages.settings.browserPanel.engine_unknown')}
                            </span>
                          )}
                          <Btn
                            onClick={() => onDownload(engine)}
                            disabled={engineBlockText !== null}
                            aria-describedby={describedBy}
                          >
                            {state === 'retry' ? (
                              <>
                                <RotateCcw size={13} className="lucide-inline" />
                                {i18nT('pages.settings.browserPanel.engine_retry')}
                              </>
                            ) : (
                              <>
                                <Download size={13} className="lucide-inline" />
                                {i18nT('pages.settings.browserPanel.engine_download')}
                              </>
                            )}
                          </Btn>
                        </div>
                      )}
                    </div>
                    {/* No hand-off: the extension token draft shares this panel,
                        and the navigation unmounts it. */}
                    {requestError && <ErrorNotice variant="inline" message={requestError} />}
                  </div>
                )
              })}
            </div>
            {/* "Unknown" beside an enabled Download needs its reason: the cache
                could not confirm the build, and downloading again is safe. Said
                once for the list; every unknown row's button points here. Status
                text, not an error: nothing failed. */}
            {anyUnknown && (
              <p
                id={unknownReasonId}
                className="text-[13px] text-muted mt-2 mb-0"
                data-testid="browser-engine-unknown-reason"
              >
                {i18nT('pages.settings.browserPanel.engine_unknown_reason')}
              </p>
            )}
            {/* Omitted while the CLI card already shows this very sentence under
                its Installing button; the rows are described by that element. */}
            {engineBlockText && !engineReasonShared && (
              <p id={engineReasonId} className="text-[13px] text-muted mt-2 mb-0" data-testid="browser-engine-reason">
                {engineBlockText}
              </p>
            )}
          </div>
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}
