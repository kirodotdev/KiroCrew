/**
 * `CliPanel`, loaded on demand: xterm and its addons stay off the dashboard's
 * first load and arrive with the first terminal pane that mounts, or earlier
 * through `preloadTerminal` / `prewarmTerminal` in `terminalSessions.ts`. The
 * boundary is local to the pane, so a loading terminal never blanks the panel
 * around it.
 *
 * While the chunk loads, a focus stand-in takes the focus as the pane shows
 * (as `CliPanel`'s own does while xterm is held), so keys typed meanwhile go
 * to the shell, in order and as typed, not to the control that opened the pane.
 *
 * Once the chunk has loaded, a pane that mounts renders `CliPanel` directly, in
 * the same commit as the tap that opened it. That matters on a phone: the focus
 * stand-in that raises the keyboard must take focus inside the opening tap, so
 * the dock preloads the chunk while the phone is idle.
 */
import { useLayoutEffect, useRef, useState, type ComponentProps } from 'react'
import { Loader2 } from 'lucide-react'
import type CliPanel from './CliPanel'
import { lazyRetry } from './lazyRetry'
import { loadTerminalModule, loadedTerminalModule } from './terminalSessions'
import { useStandInKeys } from '../utils/terminalStandInKeys'
import { willSkipFocusOnShow } from '../utils/terminalQuietShow'
import { i18nT } from '../i18n/t'

type CliPanelProps = ComponentProps<typeof CliPanel>

// No hand-off: a terminal pane sits beside the chat composer's unsent draft.
const LoadedCliPanel = lazyRetry<CliPanelProps>(loadTerminalModule, {
  layout: 'page',
  title: () => i18nT('components.cliPanel.load_failed'),
  scope: 'terminal',
  askAgent: false,
})

/** Stands in while the chunk loads: the pane's own frame and colour, so the
 *  terminal does not flash a blank box, hidden like a hidden pane, and a
 *  focus stand-in that sends each key to the shell's early-input buffer. */
function TerminalLoading({ sessionId, visible }: { sessionId: string; visible: boolean }) {
  const proxyRef = useRef<HTMLTextAreaElement>(null)
  const standIn = useStandInKeys(sessionId)
  // A show a Run-in-terminal action asked to be quiet leaves the focus alone;
  // the mark stays for the terminal itself to read.
  useLayoutEffect(() => {
    if (visible && !willSkipFocusOnShow(sessionId)) proxyRef.current?.focus({ preventScroll: true })
  }, [visible, sessionId])
  return (
    <div
      role="status"
      data-testid="cli-panel-loading"
      className="relative flex w-full h-full items-start gap-2 overflow-hidden px-3 pt-2 text-[12px] text-muted"
      style={{ background: 'var(--term-bg, var(--bg))', display: visible ? 'flex' : 'none' }}
    >
      <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
      {i18nT('components.cliPanel.loading')}
      {/* 16px so iOS does not zoom on focus; on screen and non-zero so Safari
          has a field to reveal above the keyboard (same as CliPanel's). */}
      <textarea
        ref={proxyRef}
        data-testid="terminal-loading-focus-proxy"
        aria-label={i18nT('app.terminal')}
        className="absolute left-0 top-0 w-px h-4 p-0 m-0 border-0 opacity-0 resize-none text-[16px]"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        {...standIn}
      />
    </div>
  )
}

export default function LazyCliPanel(props: CliPanelProps) {
  // Read once per mount: switching from the lazy boundary to the component
  // itself on a later render would remount the pane.
  const [Loaded] = useState(() => loadedTerminalModule()?.default ?? null)
  // Where the focus is as this pane starts waiting, so the terminal that
  // arrives later leaves alone a field the user has moved to meanwhile.
  // A hidden pane takes the focus later, when it is switched to, as any tab does.
  const [focusBeforeLoad] = useState(() => (Loaded || props.visible === false ? undefined : document.activeElement))
  if (Loaded) return <Loaded {...props} />
  return (
    <LoadedCliPanel
      {...props}
      focusBeforeLoad={focusBeforeLoad}
      loading={<TerminalLoading sessionId={props.sessionId} visible={props.visible ?? true} />}
    />
  )
}
