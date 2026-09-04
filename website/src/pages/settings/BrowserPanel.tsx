import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  Puzzle,
  Download,
  ExternalLink,
  Globe,
  HardDriveDownload,
  KeyRound,
  Loader2,
} from 'lucide-react'

import { api, type BrowserInstallData } from '../../api/client'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { Badge, Btn, EmptyState, FormSkeleton, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { isElectron } from '../../lib/electron'
import { useImeGuard } from '../../hooks/useImeGuard'
import type { DashboardConfig } from '../chat/ChatSettings'
import { Trans } from 'react-i18next'
import { i18nT } from '../../i18n/t'
import { copyToClipboard } from '../../utils/clipboard'

const INSTALL_KEY = ['browserInstall'] as const

/**
 * Where the attach-mode extension is published. The id is the one `playwright-cli`
 * itself points at, so this link and the tool agree on which extension counts.
 */
const EXTENSION_URL =
  'https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm'

/**
 * Poll cadence while an install runs. The install downloads a browser, so it
 * outlives any single request and progress is only observable by re-reading.
 */
const INSTALLING_POLL_MS = 2_000

/** Poll cadence at rest, which only has to notice an install done elsewhere. */
const IDLE_POLL_MS = 15_000

/** The three steps `install()` runs, named so the wait is legible rather than blank. */
const INSTALL_STEP_KEYS = [
  'pages.settings.browserPanel.step_npm',
  'pages.settings.browserPanel.step_browser',
  'pages.settings.browserPanel.step_skills',
] as const

/** Engines the CLI can download. Mirrors `browser_cli.install.BROWSER_ENGINES`. */
const BROWSER_ENGINES = ['chromium', 'firefox', 'webkit'] as const

/** Catalog KEY per engine. Keys, not strings: this table is evaluated at module
 *  load, so an `i18nT()` call here would freeze the boot language. Written as a
 *  flat record of full literal keys so `check-i18n-keys.mjs` can resolve them. */
const ENGINE_LABEL_KEY: Record<string, string> = {
  chromium: 'pages.settings.browserPanel.engine_chromium',
  firefox: 'pages.settings.browserPanel.engine_firefox',
  webkit: 'pages.settings.browserPanel.engine_webkit',
}

/**
 * Browsing settings.
 *
 * Availability is not a setting: the agent browses by running `playwright-cli`,
 * so browsing is available exactly when that binary is installed. There is no
 * enable switch to render. This panel is therefore an install surface plus the one
 * disclosure a switch would otherwise have carried — that having the binary at all
 * is what grants the capability — and, once installed, the two things a user can
 * still configure: the attach extension and its optional token.
 */
export function BrowserPanel() {
  const qc = useQueryClient()

  const { data, isLoading, isError } = useQuery<BrowserInstallData>({
    queryKey: INSTALL_KEY,
    queryFn: api.getBrowserInstall,
    refetchInterval: (q) => (q.state.data?.installing ? INSTALLING_POLL_MS : IDLE_POLL_MS),
  })

  // The built-in-browser toggle lives in dashboard config (not the install
  // status), so it round-trips through /api/dashboard/config like the other
  // dashboard settings.
  const dashQ = useQuery<DashboardConfig>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
  })
  const dashMut = useMutation({
    // Send ONLY the changed key: the config handler applies keys present in the
    // body, so a full-object PUT built from this query's cache could clobber a
    // setting another client changed after we cached (lost update).
    mutationFn: (patch: Partial<DashboardConfig>) => api.updateDashboardConfig(patch),
    onSettled: () => { void qc.invalidateQueries({ queryKey: ['dashboardConfig'] }) },
  })
  const setUseBuiltin = (v: boolean) => { dashMut.mutate({ use_builtin_browser: v }) }

  // Never seeded from the server: the status carries only whether a token exists,
  // so there is nothing to prefill and no way for the value to leak back out.
  const [token, setToken] = useState('')
  // Latched, not timed out: the label is a confirmation that the paste is
  // ready, and this panel is not somewhere the user returns to repeatedly.
  const [installCmdCopied, setInstallCmdCopied] = useState(false)

  const tokenMut = useMutation({
    mutationFn: (value: string) => api.setBrowserToken(value),
    onSuccess: () => { setToken(''); void qc.invalidateQueries({ queryKey: INSTALL_KEY }) },
  })
  const ime = useImeGuard()

  const installMut = useMutation({
    mutationFn: api.installBrowserCli,
    onSuccess: (fresh) => qc.setQueryData(INSTALL_KEY, fresh),
  })

  // Separate from installMut so the row that was pressed can show its own
  // spinner: both share the gateway's single install slot, so `installing`
  // alone cannot say WHICH download is running.
  const engineMut = useMutation({
    mutationFn: (engine: string) => api.installBrowserEngine(engine),
    onSuccess: (fresh) => qc.setQueryData(INSTALL_KEY, fresh),
  })

  if (isLoading) {
    return (
      <SettingsSection title={i18nT('pages.settings.browserPanel.browsing')}>
        <SettingsCard>
          <FormSkeleton rows={['info', 'field']} />
        </SettingsCard>
      </SettingsSection>
    )
  }
  if (isError || !data) {
    return (
      <SettingsSection title={i18nT('pages.settings.browserPanel.browsing')}>
        <ErrorNotice message={i18nT('pages.settings.browserPanel.cannot_load')} />
      </SettingsSection>
    )
  }

  // `engineMut.isPending` is part of this, not a detail: the gateway has ONE
  // install slot, and `data.installing` only turns true on the next poll. Without
  // it a second click lands in that window, gets refused (409), and the row's
  // spinner follows `engineMut.variables` -- so the panel would show WebKit
  // downloading while Firefox actually is.
  const installing = data.installing || installMut.isPending || engineMut.isPending
  // Node is the one prerequisite an install cannot supply for the operator, so a
  // too-old runtime is reported instead of offering a button that would fail.
  const blockedByNode = !data.node_ok
  // Bound once so the render, the copy handler and the guard all use the same
  // narrowed value -- otherwise the handler needs a `?? ''` fallback that the
  // guard has already made unreachable, which is an untestable branch rather
  // than a safety net.
  const installCommand = data.standalone_install

  return (
    <SettingsSection title={i18nT('pages.settings.browserPanel.browsing')}>
      {data.installed ? (
        <>
          {/* Status: one row, so "can it browse" is answerable at a glance. */}
          <SettingsCard>
            <div className="flex items-start gap-3">
              <CheckCircle2 size={18} className="text-ok shrink-0 mt-[2px]" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium">
                    {i18nT('pages.settings.browserPanel.available')}
                  </span>
                  {data.cli_version && <Badge variant="ok">{data.cli_version}</Badge>}
                  {!data.browser_ok && (
                    <Badge variant="warn">
                      {i18nT('pages.settings.browserPanel.browser_missing')}
                    </Badge>
                  )}
                </div>
                <p className="text-[13px] text-muted mt-1.5 mb-0">
                  {i18nT('pages.settings.browserPanel.presence_is_consent')}
                </p>
              </div>
            </div>
          </SettingsCard>

          {/*
            Built-in browser toggle. The native panel is a desktop-app-only
            Electron view, so off the desktop the switch is force-disabled and
            reads OFF -- the agent uses playwright-cli there regardless. When ON
            (desktop), the browser tool drives the built-in panel; when the user
            turns it OFF, the tool falls back to playwright-cli.
          */}
          <SettingsCard>
            <SettingsToggle
              label={i18nT('pages.settings.browserPanel.use_builtin_label')}
              configKey="dashboard.use_builtin_browser"
              description={
                isElectron
                  ? i18nT('pages.settings.browserPanel.use_builtin_desc')
                  : i18nT('pages.settings.browserPanel.use_builtin_desktop_only')
              }
              checked={isElectron ? (dashQ.data?.use_builtin_browser ?? true) : false}
              onChange={setUseBuiltin}
              disabled={!isElectron || !dashQ.isSuccess || dashMut.isPending}
            />
          </SettingsCard>

          {/*
            Attach mode needs a Chrome extension, and nothing here can install it:
            a browser extension is granted inside the browser, by the person using
            it. Its own card rather than a footnote on the status row, because for a
            user who wants their real logged-in session this IS the next step.
          */}
          <SettingsCard>
            <div className="flex items-start gap-3">
              <Puzzle size={18} className="text-muted shrink-0 mt-[2px]" />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">
                  {i18nT('pages.settings.browserPanel.attach_title')}
                </div>
                <p className="text-[13px] text-muted mt-1 mb-2">
                  {i18nT('pages.settings.browserPanel.attach_needs_extension')}
                </p>
                <a
                  href={EXTENSION_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1.5 text-[13px] text-accent hover:underline"
                >
                  {i18nT('pages.settings.browserPanel.get_the_extension')}
                  <ExternalLink size={13} />
                </a>
              </div>
            </div>
          </SettingsCard>

          {/*
            Downloads, one row per engine. The old Browser Mode panel exposed
            this as an engine SELECTOR, which conflated two things: which browser
            is on disk, and which one a session uses. The CLI picks the engine per
            command (`open --browser=firefox`), so what is left to configure is
            purely "is it downloaded" — and all three are offered, because
            reporting one boolean made Firefox and WebKit look unavailable.
          */}
          <SettingsCard>
            <div className="flex items-start gap-3">
              <HardDriveDownload size={18} className="text-muted shrink-0 mt-[2px]" />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">
                  {i18nT('pages.settings.browserPanel.engines_title')}
                </div>
                <p className="text-[13px] text-muted mt-1 mb-2.5">
                  {i18nT('pages.settings.browserPanel.engines_explains')}
                </p>
                <div className="flex flex-col gap-2">
                  {BROWSER_ENGINES.map((engine) => {
                    const present = data.browsers?.[engine]
                    return (
                      <div
                        key={engine}
                        className="flex items-center gap-2 justify-between border border-border rounded-md px-3 py-2"
                        data-testid={`browser-engine-${engine}`}
                      >
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="text-[13px] font-medium">
                            {i18nT(ENGINE_LABEL_KEY[engine])}
                          </span>
                          {engine === 'chromium' && (
                            <Badge variant="muted">
                              {i18nT('pages.settings.browserPanel.engine_needed_for_attach')}
                            </Badge>
                          )}
                        </div>
                        {present ? (
                          <span className="inline-flex items-center gap-1.5 text-[13px] text-ok shrink-0">
                            <CheckCircle2 size={14} />
                            {i18nT('pages.settings.browserPanel.engine_downloaded')}
                          </span>
                        ) : (
                          <Btn
                            onClick={() => engineMut.mutate(engine)}
                            disabled={installing}
                            aria-busy={installing && engineMut.variables === engine}
                          >
                            {installing && engineMut.variables === engine ? (
                              <>
                                <Loader2 size={13} className="lucide-inline animate-spin" />
                                {i18nT('pages.settings.browserPanel.installing')}
                              </>
                            ) : (
                              <>
                                <Download size={13} className="lucide-inline" />
                                {i18nT('pages.settings.browserPanel.engine_download')}
                              </>
                            )}
                          </Btn>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            </div>
          </SettingsCard>

          {/*
            The token only removes the browser's approval prompt for an attach.
            Optional on purpose: it is a stored credential whose absence costs one
            click, and that prompt is the one moment a human is told a program is
            about to drive their logged-in browser.
          */}
          <SettingsCard>
            <div className="flex items-start gap-3" data-setting-label={i18nT('pages.settings.browserPanel.token_label')}>
              <KeyRound size={18} className="text-muted shrink-0 mt-[2px]" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <label htmlFor="pw-attach-token" className="text-sm font-medium">
                    {i18nT('pages.settings.browserPanel.token_label')}
                  </label>
                  {data.token && (
                    <Badge variant="ok">{i18nT('pages.settings.browserPanel.token_stored')}</Badge>
                  )}
                </div>
                <p className="text-[13px] text-muted mt-1 mb-2">
                  {i18nT('pages.settings.browserPanel.token_explains')}
                </p>
                <div className="flex items-center gap-2">
                  <Input
                    id="pw-attach-token"
                    type="password"
                    autoComplete="off"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    {...ime.bindEnter({
                      onEnter: () => {
                        if (!tokenMut.isPending && token.trim()) tokenMut.mutate(token)
                      },
                    })}
                    placeholder={
                      data.token
                        ? i18nT('pages.settings.browserPanel.token_set')
                        : i18nT('pages.settings.browserPanel.token_placeholder')
                    }
                    aria-label={i18nT('pages.settings.browserPanel.token_label')}
                    className="flex-1 min-w-0"
                  />
                  <Btn
                    primary
                    onClick={() => tokenMut.mutate(token)}
                    disabled={tokenMut.isPending || !token.trim()}
                  >
                    {i18nT('pages.settings.browserPanel.token_save')}
                  </Btn>
                  {data.token && (
                    <Btn
                      onClick={() => { setToken(''); tokenMut.mutate('') }}
                      disabled={tokenMut.isPending}
                    >
                      {i18nT('pages.settings.browserPanel.token_clear')}
                    </Btn>
                  )}
                </div>
              </div>
            </div>
          </SettingsCard>
        </>
      ) : (
        /*
          Not installed is the FIRST thing most users see here, so it is a guided
          empty state rather than a sentence and a link: what the capability is,
          what the button will do (named steps, since it downloads a browser and
          takes a while), and the one prerequisite this cannot supply.
        */
        <SettingsCard>
          <EmptyState
            testId="browser-not-installed"
            icon={<Globe />}
            title={i18nT('pages.settings.browserPanel.not_installed')}
            subtitle={i18nT('pages.settings.browserPanel.install_explains')}
            action={
              blockedByNode ? (
                /*
                  Node is the one prerequisite an install cannot supply, so this
                  state has to be actionable rather than a bare requirement. It
                  also distinguishes "too old" from "absent": interpolating a
                  found-version into the same sentence produced "found none",
                  which reads as a bug and tells a first-time user nothing about
                  what to do next.
                */
                <div className="flex flex-col items-center gap-1.5 text-[13px]">
                  <div className="flex items-center gap-2 text-warn">
                    <AlertTriangle size={14} className="shrink-0" />
                    {data.node_version
                      ? i18nT('pages.settings.browserPanel.needs_node', {
                          version: data.node_version,
                        })
                      : i18nT('pages.settings.browserPanel.node_missing')}
                  </div>
                  <div className="text-muted text-center max-w-[340px]">
                    {/*
                      ONE key for the whole passage, with the link interpolated
                      as <dl>. Two adjacent keys joined by {' '} is what the
                      render-time i18n gate rejects ("merge, not join"), and the
                      join also pinned every locale to English word order --
                      several languages put the action before the instruction.
                      Same shape as DiscordPanel's `guide_body`.
                    */}
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
                  {/*
                    The link above is a dead end for the operator this panel most
                    needs to help: a locked-down machine where Node cannot be
                    installed, or a registry that answers 401. `playwright-cli.sh`
                    exists for exactly that -- it bootstraps its own Node into the
                    user's home directory and classifies the enterprise failures npm
                    reports as one undifferentiated error -- so the offer belongs
                    HERE, at the moment the install is blocked, not only in the docs.

                    The command comes from the GATEWAY, not from this file and not
                    from the catalogs. Only the gateway knows which OS it runs on, so
                    the operator gets the one command that applies rather than two to
                    choose between -- this page may well be open on a different
                    machine. It also cannot live in either place: the untranslated
                    literal gate forbids the string here, and the catalogs' pseudo-
                    locale accents every Latin character, which would corrupt a URL.
                  */}
                  {installCommand && (
                    <div className="text-muted text-left max-w-[340px] mt-1">
                      {i18nT('pages.settings.browserPanel.node_no_admin')}
                      <pre className="mt-1.5 mb-1 whitespace-pre-wrap break-all text-[12px] bg-bg-elevated rounded px-2 py-1.5">
                        <code>{installCommand}</code>
                      </pre>
                      {/*
                        A copy button, not hand-selection. This is a ~110-character
                        string that must be transcribed EXACTLY, wrapped over three
                        lines by break-all, and a typo in it produces another opaque
                        curl failure for a user who is already stuck.

                        `copyToClipboard`, not navigator.clipboard directly, and
                        awaited before the label flips: the Clipboard API is
                        unavailable on a plain-HTTP remote gateway -- which is a
                        plausible way to be reading this panel -- and flipping the
                        label regardless would promise a paste that is not there.
                        Same reasoning as AboutPanel's gateway command.
                      */}
                      <Btn
                        onClick={async () => {
                          await copyToClipboard(installCommand)
                          setInstallCmdCopied(true)
                        }}
                      >
                        <Copy size={13} className="lucide-inline" />{' '}
                        {installCmdCopied
                          ? i18nT('pages.settings.browserPanel.copied')
                          : i18nT('pages.settings.browserPanel.copy_command')}
                      </Btn>
                    </div>
                  )}
                </div>
              ) : (
                <Btn
                  primary
                  onClick={() => installMut.mutate()}
                  disabled={installing}
                  aria-busy={installing}
                >
                  {installing ? (
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
              )
            }
          />

          {/*
            The steps are listed for BOTH the idle and running cases: before, they
            say what pressing the button will do to the machine; during, they turn
            a multi-minute blank wait into something legible.
          */}
          {!blockedByNode && (
            <div className="border-t border-border pt-3 mt-1">
              <div className="text-[13px] text-muted mb-1.5">
                {installing
                  ? i18nT('pages.settings.browserPanel.install_takes_a_while')
                  : i18nT('pages.settings.browserPanel.install_steps_intro')}
              </div>
              <ol className="text-[13px] text-muted/80 m-0 pl-5 list-decimal flex flex-col gap-1">
                {INSTALL_STEP_KEYS.map((key) => (
                  <li key={key}>{i18nT(key)}</li>
                ))}
              </ol>
            </div>
          )}
        </SettingsCard>
      )}

      {/*
        A failed step is shown verbatim rather than summarized: the useful cases
        are a registry auth error and a blocked download, and both are only
        actionable if the operator can read what the tool said.
      */}
      {data.last_error && !installing && (
        /*
          `askAgent` on purpose. This is the one error in the panel a user often
          cannot act on alone: an npm EACCES on the global prefix, a registry
          that needs auth, a blocked download. The button opens a chat with the
          failure's context attached, which turns a dead end into a debugging
          session. It costs an unsaved token draft if one is mid-typing (this is
          an in-app navigation), and that trade is worth it -- the token field is
          normally empty, and being stranded on an npm error is not recoverable
          from this screen.
        */
        <ErrorNotice message={data.last_error} askAgent />
      )}
    </SettingsSection>
  )
}
