import { useId, useState, type ReactNode } from 'react'
import { RefreshCw, ShieldCheck } from 'lucide-react'
import {
  PANEL_CLASS,
  PINNED_FOOTER_CLASS,
  SCRIM_CLASS,
  SECTION_CLASS,
  ShellAside,
} from './OnboardingChapterShell'
import { useScrollEdgesY } from '../hooks/useScrollEdges'
import { Btn } from './ui'
import { CopyCommand } from './agentHarness'
import { i18nT } from '../i18n/t'

// Shared full-screen chrome for every gate state. This is the SAME container the
// first-run onboarding chapters use (Import setup / Customize): the identical
// scrim, panel geometry, and accent aside with the identical mascot positions,
// imported from OnboardingChapterShell rather than re-declared here. Only the
// copy in the aside and the right-column content differ. `cardLabel` names the
// region for assistive tech.
export function SetupShell({
  children,
  footer,
  cardLabel,
  asideHeadline,
  asideBody,
}: {
  children: ReactNode
  // Rendered OUTSIDE the scroll region, so a state whose content overflows the
  // fixed panel height still shows its closure action whole. A half-clipped
  // primary button reads as a rendering defect rather than a scroll affordance.
  footer?: ReactNode
  cardLabel?: string
  // The default aside says "Install Kiro CLI, sign in once…", which contradicts
  // a state whose headline is "already installed" and which deliberately offers
  // no install action. States like that pass their own copy so the two columns
  // of the same screen do not disagree.
  asideHeadline?: string
  asideBody?: string
}) {
  const label = cardLabel || i18nT('components.kiroPrerequisiteGate.your_crew_is_almost_ready')
  // A scroll cue on the internally-scrolling column: the panel height is fixed,
  // so the tallest states clip below the footer divider, and that divider then
  // reads as the end of the content rather than a fold. `attachContent` follows
  // the body because it swaps with the gate's state, changing the overflow.
  const [attachScroll, edges, , attachScrollContent] = useScrollEdgesY<HTMLDivElement>()
  return (
    <main className={SCRIM_CLASS} aria-label={label}>
      <div className={PANEL_CLASS}>
        <ShellAside
          copy={{
            ariaLabel: label,
            panelHeadline:
              asideHeadline || i18nT('components.kiroPrerequisiteGate.your_crew_is_almost_ready'),
            panelBody:
              asideBody
              || i18nT('components.kiroPrerequisiteGate.install_kiro_cli_sign_in_once_and_kiro_crew_will'),
            panelFootnote: i18nT(
              'components.kiroPrerequisiteGate.secure_setup_on_your_gateway_host',
            ),
          }}
        />
        {/* Same scroll structure as the chapters: the panel height is fixed and
            the right column scrolls internally. `my-auto` keeps the short states
            (status error / non-owner) optically centered without breaking the
            scroll on the tall two-step setup. The edge cues exist because that
            fixed height clips the tallest states, and the footer divider below
            otherwise reads as the end of the content rather than a fold — the
            overlay scrollbar fades when idle and leaves no standing sign. */}
        <section className={SECTION_CLASS}>
          <div className="relative flex min-h-0 flex-1 flex-col">
            <div
              ref={attachScroll}
              data-testid="gate-scroll-region"
              className="flex min-h-0 flex-1 flex-col overflow-y-auto"
            >
              <div ref={attachScrollContent} className="my-auto w-full px-6 py-8 sm:px-10 sm:py-10">{children}</div>
            </div>
            {/* `from-card` matches SECTION_CLASS's own `bg-card`, so the fade
                dissolves into the column rather than onto a mismatched surface.
                `bottom-0`/`top-0` of this relative wrapper are the footer divider
                and the top edge of the scroll region. */}
            {edges.top && (
              <div
                aria-hidden="true"
                data-testid="gate-scroll-cue-top"
                className="pointer-events-none absolute inset-x-0 top-0 z-10 h-8 bg-gradient-to-b from-card to-transparent"
              />
            )}
            {edges.bottom && (
              <div
                aria-hidden="true"
                data-testid="gate-scroll-cue-bottom"
                className="pointer-events-none absolute inset-x-0 bottom-0 z-10 h-8 bg-gradient-to-t from-card to-transparent"
              />
            )}
          </div>
          {footer ? (
            <div data-testid="gate-footer" className={`${PINNED_FOOTER_CLASS} shrink-0 border-t border-border px-6 pt-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:px-10 sm:pb-4`}>{footer}</div>
          ) : null}
        </section>
      </div>
    </main>
  )
}

/**
 * A start URL the shown command may name. The same shape the gateway accepts for
 * a recorded one (`enterprise_sign_in._START_URL_RE`), narrowed to characters
 * that need no quoting in any shell the command is rendered for, so appending it
 * to the gateway's command keeps that command runnable as pasted.
 */
const START_URL_RE = /^https:\/\/[A-Za-z0-9.-]+(?:\/[A-Za-z0-9_\-./~%=@:]*)?$/

/** *raw* as a start URL the command can name, or null. A console's trailing `#` is dropped. */
export function validStartUrl(raw: string): string | null {
  const url = raw.trim().replace(/\/?#$/, '')
  return START_URL_RE.test(url) ? url : null
}

/**
 * The owner's sign-in command. With the start URL known, the gateway's command is
 * complete and copied as is. Without it, a field comes first: it fills the shown
 * command in, in this browser only (nothing is sent to the gateway), and the copy
 * button stays off until the URL is a valid https URL.
 */
function OwnerSignInCommand({ command, needsStartUrl }: { command: string; needsStartUrl: boolean }) {
  const fieldId = useId()
  const [startUrl, setStartUrl] = useState('')
  const url = needsStartUrl ? validStartUrl(startUrl) : null
  const shown = needsStartUrl && url ? `${command} ${url}` : command
  const ready = !needsStartUrl || url !== null
  return (
    <>
      {needsStartUrl && (
        <div className="mt-3 max-w-lg">
          <label htmlFor={fieldId} className="block text-sm font-semibold text-text-strong">
            {i18nT('components.kiroPrerequisiteGate.enterprise_start_url_label')}
          </label>
          <input
            id={fieldId}
            data-testid="enterprise-start-url-input"
            aria-label={i18nT('components.kiroPrerequisiteGate.enterprise_start_url_label')}
            type="url"
            inputMode="url"
            autoComplete="url"
            spellCheck={false}
            placeholder="https://"
            value={startUrl}
            onChange={e => setStartUrl(e.target.value)}
            aria-describedby={`${fieldId}-hint`}
            className="mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 font-mono text-sm text-text-strong focus-ring"
          />
          <p id={`${fieldId}-hint`} data-testid="enterprise-start-url-hint" className="mt-1 text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.enterprise_start_url_hint')}
          </p>
        </div>
      )}
      <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
        {i18nT('components.kiroPrerequisiteGate.relock_run_in_terminal')}
      </p>
      <div className="mt-3" data-testid="enterprise-sign-in-command">
        <CopyCommand disabled={!ready}>
          <code>{shown}</code>
        </CopyCommand>
      </div>
    </>
  )
}

/**
 * The enterprise gate's sign-in screen: the gateway reads Crew's own vault sign-in,
 * so the owner is shown that one command (a code value from the gateway, never a
 * catalog string). A viewer who is not the owner is told to ask the owner.
 */
export function EnterpriseSignInRequired({
  command,
  needsStartUrl,
  owner,
  retrying,
  onRetry,
}: {
  command: string
  /** The command ends at `--start-url`; say where the start URL comes from. */
  needsStartUrl: boolean
  owner: boolean
  retrying: boolean
  onRetry: () => void
}) {
  // The default aside tells the user to install Kiro CLI, which this gate does not read.
  return (
    <SetupShell asideBody={i18nT('components.kiroPrerequisiteGate.enterprise_sign_in_aside')}>
      <div data-testid="enterprise-sign-in-required">
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent-subtle text-accent">
          <ShieldCheck className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-accent">
          {i18nT('components.kiroPrerequisiteGate.sign_in_required')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kiroPrerequisiteGate.sign_in_again_to_continue')}
        </h1>
        {owner ? (
          <OwnerSignInCommand command={command} needsStartUrl={needsStartUrl} />
        ) : (
          <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.relock_ask_owner')}
          </p>
        )}
        <div className="mt-6">
          <Btn type="button" disabled={retrying} onClick={onRetry}>
            <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />
            {i18nT('components.kiroPrerequisiteGate.check_again')}
          </Btn>
        </div>
      </div>
    </SetupShell>
  )
}
