import { useCallback, useLayoutEffect, useState, type ComponentPropsWithoutRef, type KeyboardEvent, type ReactNode } from 'react'
import { useScrollEdgesY, type ScrollEdgesY } from '../../hooks/useScrollEdges'
import { DropdownMenuContent, DropdownMenuItem } from '../ui/dropdown-menu'
import { MORE_BELOW_MASK } from '../ModelEffortDropdown'

/* A bounded, scrollable box that says when it is clipping. It holds text a
   person consents to (an exact command, a call's arguments), and a box whose
   overlay scrollbar fades at rest reads as complete when it is not: a clean cut
   at a newline looks like the end of the command. So the edge that hides
   content fades, and `data-more-above` / `data-more-below` name which. */

// Literal class strings, so Tailwind sees each one. The fade ends at a third
// of full opacity, as `MORE_BELOW_MASK` does, so the cut line stays readable;
// the edge below is that same mask, imported rather than copied.
const MASK_BELOW = MORE_BELOW_MASK
const MASK_ABOVE = '[mask-image:linear-gradient(to_bottom,rgba(0,0,0,0.35),black_20px)] [-webkit-mask-image:linear-gradient(to_bottom,rgba(0,0,0,0.35),black_20px)]'
const MASK_BOTH = '[mask-image:linear-gradient(to_bottom,rgba(0,0,0,0.35),black_20px,black_calc(100%-20px),rgba(0,0,0,0.35))] [-webkit-mask-image:linear-gradient(to_bottom,rgba(0,0,0,0.35),black_20px,black_calc(100%-20px),rgba(0,0,0,0.35))]'

export function edgeMask(edges: ScrollEdgesY): string {
  return edges.top && edges.bottom ? MASK_BOTH : edges.top ? MASK_ABOVE : edges.bottom ? MASK_BELOW : ''
}

export function edgeAttrs(edges: ScrollEdgesY) {
  return { 'data-more-above': edges.top ? '' : undefined, 'data-more-below': edges.bottom ? '' : undefined }
}

/** A scrolling `div` with the edge cues. A caller that needs the element (a
 *  `<pre>` the menu measures, say) uses the hook with `edgeMask` directly.
 *  `data-cued-scroll` is what `pageScrollBox` finds it by. */
export function CuedScrollBox({ className, testId, children }: { className: string; testId?: string; children: ReactNode }) {
  const [attach, edges, , attachContent] = useScrollEdgesY<HTMLDivElement>()
  const ref = useCallback((node: HTMLDivElement | null) => { attach(node) }, [attach])
  // The content is observed too: a box already at its cap does not resize when
  // what it holds changes (a Raw/Formatted flip), and its cue must follow.
  const contentRef = useCallback((node: HTMLDivElement | null) => { attachContent(node) }, [attachContent])
  return (
    <div ref={ref} data-testid={testId} data-cued-scroll="" {...edgeAttrs(edges)} className={`${className} overflow-y-auto ${edgeMask(edges)}`}>
      <div ref={contentRef}>{children}</div>
    </div>
  )
}

/** PageUp / PageDown on a menu row scroll the `CuedScrollBox` inside it while
 *  it has more that way to show. A Radix menu takes Tab and every navigation
 *  key for moving between rows, so without this a keyboard user could choose a
 *  row whose consent text sits past its box's edge and never bring it on
 *  screen. At the box's end the keys go back to the menu. */
export function pageScrollBox(e: KeyboardEvent<HTMLElement>) {
  if (e.key !== 'PageDown' && e.key !== 'PageUp') return
  const down = e.key === 'PageDown'
  const room = (b: HTMLElement) => (down ? b.scrollHeight - b.clientHeight - b.scrollTop : b.scrollTop)
  // A row may hold more than one box (a command, then the rest of the input):
  // PageDown pages the first that still has more below, PageUp the last that
  // has more above, so every box is reachable in reading order.
  const boxes = [...e.currentTarget.querySelectorAll<HTMLElement>('[data-cued-scroll]')]
  const box = (down ? boxes : boxes.reverse()).find(b => room(b) > 1)
  if (!box) return
  e.preventDefault()
  e.stopPropagation()
  box.scrollTop += (down ? 1 : -1) * Math.max(box.clientHeight - 20, 20)
}

/** The approval card's menu content (Trust, Override): its own scroll fades
 *  the edge that hides rows, as a `CuedScrollBox` does. The fades are keyed on
 *  the `data-more-*` attributes in one static class string, since the
 *  design-system lint cannot read a class built at render on a ui/ component;
 *  both edges is the more specific selector, so it wins. The menu is attached
 *  through state, not as the ref itself: Radix composes the content's ref anew
 *  each render, and a scroll hook re-attached on every one re-renders without
 *  end. Its content is observed and re-measured on every render, because a menu
 *  at its height cap does not resize when a row grows (an armed row's longer
 *  label, a lifetime's clock time). */
export function CuedMenuContent({ children, ...props }: Omit<ComponentPropsWithoutRef<typeof DropdownMenuContent>, 'className'>) {
  const [attach, edges, remeasure, attachContent] = useScrollEdgesY<HTMLDivElement>()
  const [el, setEl] = useState<HTMLDivElement | null>(null)
  useLayoutEffect(() => { attach(el) }, [attach, el])
  useLayoutEffect(() => { remeasure() })
  const contentRef = useCallback((node: HTMLDivElement | null) => { attachContent(node) }, [attachContent])
  return (
    <DropdownMenuContent ref={setEl} {...props} {...edgeAttrs(edges)} className="min-w-[240px] max-w-[min(420px,calc(100vw-2rem))] data-[more-below]:[mask-image:linear-gradient(to_bottom,black_calc(100%-20px),rgba(0,0,0,0.35))] data-[more-below]:[-webkit-mask-image:linear-gradient(to_bottom,black_calc(100%-20px),rgba(0,0,0,0.35))] data-[more-above]:[mask-image:linear-gradient(to_bottom,rgba(0,0,0,0.35),black_20px)] data-[more-above]:[-webkit-mask-image:linear-gradient(to_bottom,rgba(0,0,0,0.35),black_20px)] [&[data-more-above][data-more-below]]:[mask-image:linear-gradient(to_bottom,rgba(0,0,0,0.35),black_20px,black_calc(100%-20px),rgba(0,0,0,0.35))] [&[data-more-above][data-more-below]]:[-webkit-mask-image:linear-gradient(to_bottom,rgba(0,0,0,0.35),black_20px,black_calc(100%-20px),rgba(0,0,0,0.35))]">
      <div ref={contentRef}>{children}</div>
    </DropdownMenuContent>
  )
}

/** The pending call as the card shows it: its command (if any), the rest of
 *  its input, and, for a call with no command, the tool it calls. */
export interface PendingCall { command: string; input: string; tool?: { kind: string; name: string } }

/** The call every row of an approval menu also runs, printed at the menu's top
 *  because the open menu covers the card. A menu row that chooses nothing, so a
 *  keyboard user can reach it and page through a call longer than its box, and
 *  the first Enter after opening the menu lands on it, not on a grant. */
export function PendingCallItem({ label, call, testId }: { label: string; call: PendingCall; testId: string }) {
  const tool = call.command ? undefined : call.tool
  const box = 'max-h-[20vh] bg-bg-elevated px-1.5 py-0.5 rounded font-mono whitespace-break-spaces break-all border border-border'
  return (
    <DropdownMenuItem
      data-testid={testId}
      textValue={label}
      onSelect={e => { e.preventDefault() }}
      onKeyDown={pageScrollBox}
      className="flex-col items-stretch"
    >
      {/* The row's look lives here, not on the primitive; `cursor-default`
          because choosing it does nothing. */}
      <div className="flex flex-col gap-1 min-w-0 cursor-default text-[12px]">
        <span className="text-[11px] text-muted">{label}</span>
        {tool && (tool.kind || tool.name) && (
          <span className="flex items-center gap-1.5 flex-wrap min-w-0">
            {tool.kind && <span className="text-[11px] text-text">{tool.kind}</span>}
            {tool.name && <span data-testid={`${testId}-tool`} className="min-w-0 bg-bg-elevated px-1.5 py-0.5 rounded font-mono break-all border border-border">{tool.name}</span>}
          </span>
        )}
        {/* The command and the rest of the input in two blocks, as on the
            card, so a working directory or a timeout never reads as a second
            command line. */}
        {call.command && <CuedScrollBox testId={`${testId}-command`} className={box}>{call.command}</CuedScrollBox>}
        {call.input && <CuedScrollBox testId={`${testId}-text`} className={box}>{call.input}</CuedScrollBox>}
      </div>
    </DropdownMenuItem>
  )
}
