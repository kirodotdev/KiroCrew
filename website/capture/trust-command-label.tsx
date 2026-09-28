/**
 * Evidence for the trust menu's exact-command label.
 *
 * THE PROBLEM: the label budget cut the command to a few words, so
 * `gh pr view 42 --repo owner/repo` and `gh pr merge 42 --repo owner/repo`
 * rendered as the same string. The grant is an exact-string match, so the one
 * line a user reads before consenting could not tell a read from a write.
 *
 * The scene mounts the REAL TrustDropdown, which calls the REAL
 * `truncateCommandLabel`, against the real stylesheet, theme tokens and live i18n
 * catalog. Reaching this state in the shell needs a seeded session, a live
 * websocket and an agent parked on a tool call; nothing here re-implements the
 * component, its classes, or its strings. The line above the control is harness
 * chrome, labelled as such, so the frame shows which command produced the label.
 *
 *   ?cmd=api_config|api_secrets|pipeline|over_budget|spaced|blob   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import TrustDropdown from '../src/components/TrustDropdown'
import { initI18n } from '../src/i18n'
import '../src/index.css'

/** Commands shaped like the ones in the customer report. The two `api` calls
 *  share a long prefix — the repo slug and `contents/` path segment — so a short
 *  budget truncates both to the same string and the menu offers to trust one of
 *  two commands the reader cannot tell apart. That collision is what the wider
 *  budget removes; `pipeline` is the piped form whose tail filters used to cost a
 *  prompt each. */
const COMMANDS = {
  api_config: 'gh api repos/owner/some-repository/contents/config.json --jq .sha',
  api_secrets: 'gh api repos/owner/some-repository/contents/secrets.json --jq .sha',
  pipeline: 'gh pr diff 42 --repo owner/some-repository | head -40 | wc -l',
  // Longer than the 256-char elision budget, which is where the residual gap
  // lived (#4700): the old label cut the middle out, and the `title` tooltip
  // that carried the rest never fires on touch. The frame shows the row
  // rendering it whole, wrapped inside the menu's width cap.
  over_budget:
    'gh api repos/owner/some-repository/contents/infrastructure/environments/production/'
    + 'us-east-1/services/checkout/config/secrets.enc.json --jq .sha '
    + '--header "Accept: application/vnd.github.v3.raw" '
    + '--hostname github.enterprise.example.com --paginate --cache 30s',
  // A quoted argument carrying a RUN of whitespace. HTML collapses runs by
  // default, so without `whitespace-pre-wrap` this renders as the one-space
  // command while granting different exact authority; the harness reads the
  // rendered innerText back and fails if the run collapsed.
  spaced: 'grep -r "two  spaces" /path/to/dir --include="*.tsx"',
  // Multi-KB and unspaced -- the scale the old 256-char budget named as what it
  // guarded ("a base64 blob, a megabyte one-liner"). Nothing shortens it now,
  // so the bound has to be the menu itself: the harness scrolls to the end and
  // asserts the family and session rows are still reachable.
  blob: `bash -c "echo ${'A1b2C3d4e5F6g7H8'.repeat(256)} | base64 -d | sh"`,
} as const

const params = new URLSearchParams(location.search)
const key = (params.get('cmd') ?? 'api_config') as keyof typeof COMMANDS
const cmd = COMMANDS[key] ?? COMMANDS.api_config
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

/** The classes ChatInput gives the control it renders, so the trigger is the size
 *  and weight it is in the chat approval row rather than an unstyled button. */
const BTN = 'px-2.5 py-1 rounded-md border border-border bg-transparent text-muted ' +
  'text-[13px] cursor-pointer font-body inline-flex items-center gap-1'

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="bg-bg text-text p-5 w-[720px] flex flex-col gap-3">
    {/* Harness chrome: names the command whose label is under test. Whitespace
        preserved for the same reason the row under test preserves it — a chrome
        line that collapses a run the row keeps makes the two disagree in the
        frame, and a reader cannot tell which one is wrong. */}
    <div className="text-[11px] text-muted font-mono break-all whitespace-pre-wrap">
      <span className="not-italic text-subtle">the agent wants to run: </span>{cmd}
    </div>
    <div>
      <TrustDropdown
        fullCommand={cmd}
        baseCommand={cmd.split(/\s+/)[0]}
        isShell
        className={BTN}
        onAction={() => {}}
      />
    </div>
  </div>,
)
