import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, cleanup } from '@testing-library/react'
import { LexicalComposer } from '@lexical/react/LexicalComposer'
import { PlainTextPlugin } from '@lexical/react/LexicalPlainTextPlugin'
import { ContentEditable } from '@lexical/react/LexicalContentEditable'
import { LexicalErrorBoundary } from '@lexical/react/LexicalErrorBoundary'
import { HistoryPlugin } from '@lexical/react/LexicalHistoryPlugin'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  $getRoot,
  $createParagraphNode,
  $createTextNode,
  $isTextNode,
  UNDO_COMMAND,
  type LexicalEditor,
} from 'lexical'

import PillDragPlugin, { PILL_KEY_MIME } from '../composer/plugins/PillDragPlugin'
import {
  PasteBlockNode,
  $createPasteBlockNode,
  $isPasteBlockNode,
} from '../composer/nodes/PasteBlockNode'
import { DropGapNode, $isDropGapNode } from '../composer/nodes/DropGapNode'

/**
 * Ported drag behaviour for the paste pills (see the spike at
 * `~/.kiro/crew/workspace/ime-pill-spike/lexical.html`). happy-dom (the repo's
 * test DOM) has no layout, so every scenario stubs the three geometry APIs the
 * plugin probes — `document.elementFromPoint`, `document.caretRangeFromPoint`,
 * and `Element.prototype.getBoundingClientRect` — so a synthetic point maps to a
 * chosen text offset or pill half.
 *
 * Seed layout (one paragraph): `'AAAA ' + pill#1 + ' BBBB' + pill#2`.
 */

// --- harness -----------------------------------------------------------------

let editorRef: LexicalEditor
function CaptureEditor() {
  const [editor] = useLexicalComposerContext()
  editorRef = editor
  return null
}

function Harness() {
  return (
    <LexicalComposer
      initialConfig={{
        namespace: 'pill-drag-test',
        nodes: [PasteBlockNode, DropGapNode],
        onError: (e) => {
          throw e
        },
        theme: { paragraph: 'm-0' },
      }}
    >
      <PlainTextPlugin
        contentEditable={<ContentEditable data-composer-input="" />}
        placeholder={null}
        ErrorBoundary={LexicalErrorBoundary}
      />
      <HistoryPlugin />
      <PillDragPlugin />
      <CaptureEditor />
    </LexicalComposer>
  )
}

/** Seed the paragraph and return the two pill node keys + the text node keys. */
function seed(): {
  pill1: string
  pill2: string
  aaaaKey: string
  paraKey: string
} {
  let pill1 = ''
  let pill2 = ''
  let aaaaKey = ''
  let paraKey = ''
  act(() => {
    editorRef.update(
      () => {
        const root = $getRoot()
        root.clear()
        const p = $createParagraphNode()
        const a = $createTextNode('AAAA ')
        const b = $createTextNode(' BBBB')
        const n1 = $createPasteBlockNode({ seq: 1, lines: 10, content: 'one' })
        const n2 = $createPasteBlockNode({ seq: 2, lines: 20, content: 'two' })
        p.append(a, n1, b, n2)
        root.append(p)
        pill1 = n1.getKey()
        pill2 = n2.getKey()
        aaaaKey = a.getKey()
        paraKey = p.getKey()
      },
      { discrete: true },
    )
  })
  return { pill1, pill2, aaaaKey, paraKey }
}

/** The root contenteditable element the plugin bound its listeners to. */
function rootEl(): HTMLElement {
  const el = editorRef.getRootElement()
  if (!el) throw new Error('no root element')
  return el
}

/**
 * Mount the harness and wait for Lexical to attach its root element (async act
 * flushes the `setRootElement` effect) so `seed()` and `getElementByKey` work.
 */
async function mount(): Promise<void> {
  await act(async () => {
    render(<Harness />)
  })
}

function gapCount(): number {
  return rootEl().querySelectorAll('[data-testid="drop-gap"]').length
}

/** Order of node types in the first paragraph, e.g. ['text','paste','text','paste']. */
function paragraphOrder(): Array<'text' | 'paste' | 'gap' | 'other'> {
  return editorRef.read(() => {
    const p = $getRoot().getFirstChild()
    if (!p || !('getChildren' in p)) return []
    const kids = (p as unknown as { getChildren(): ReturnType<typeof $getRoot>['getChildren'] }).getChildren()
    return (kids as unknown as Array<Parameters<typeof $isTextNode>[0]>).map((c) => {
      if ($isTextNode(c)) return 'text'
      if ($isPasteBlockNode(c)) return 'paste'
      if ($isDropGapNode(c)) return 'gap'
      return 'other'
    })
  })
}

/** Concatenated placeholder string: text content + `#seq` markers, gaps skipped. */
function orderString(): string {
  return editorRef.read(() => {
    const p = $getRoot().getFirstChild()
    if (!p || !('getChildren' in p)) return ''
    const kids = (p as unknown as { getChildren(): unknown[] }).getChildren()
    let out = ''
    for (const c of kids as Array<Parameters<typeof $isTextNode>[0]>) {
      if ($isTextNode(c)) out += c.getTextContent()
      else if ($isPasteBlockNode(c)) out += `#${c.getSeq()}`
      else if ($isDropGapNode(c)) continue
    }
    return out
  })
}

// --- geometry stubbing -------------------------------------------------------

let rafQueue: FrameRequestCallback[] = []

function fakeRange(container: Node, offset: number): Range {
  return {
    startContainer: container,
    startOffset: offset,
  } as unknown as Range
}

/** Point over a text DOM node: caretRangeFromPoint returns that container/offset. */
function stubOverText(textDom: Node, offset: number) {
  ;(document as unknown as { elementFromPoint: unknown }).elementFromPoint = vi.fn(() => {
    // A text node's DOM parent — not inside a `.pill-host`.
    return textDom.parentNode as Element | null
  })
  ;(document as unknown as { caretRangeFromPoint: unknown }).caretRangeFromPoint = vi.fn(
    () => fakeRange(textDom, offset),
  )
}

/** Point over a pill host: elementFromPoint returns the `.pill-host`, half via rect. */
function stubOverPill(pillHost: HTMLElement, _half: 'left' | 'right') {
  ;(document as unknown as { elementFromPoint: unknown }).elementFromPoint = vi.fn(() => pillHost)
  ;(document as unknown as { caretRangeFromPoint: unknown }).caretRangeFromPoint = vi.fn(() => null)
  // rect centre at x=100; left half -> x=90 < 100, right half -> x=110 > 100.
  vi.spyOn(pillHost, 'getBoundingClientRect').mockReturnValue({
    left: 80,
    right: 120,
    width: 40,
    top: 0,
    bottom: 20,
    height: 20,
    x: 80,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect)
}

/** Find the `.pill-host` DOM element for a given pill node key. */
function pillHostFor(nodeKey: string): HTMLElement {
  const el = editorRef.getElementByKey(nodeKey)
  if (!el) throw new Error('no dom for ' + nodeKey)
  return el as HTMLElement
}

/** The DOM text node inside the 'AAAA ' text node. */
function textDomFor(nodeKey: string): Node {
  const el = editorRef.getElementByKey(nodeKey)
  if (!el) throw new Error('no dom for ' + nodeKey)
  // Lexical wraps text in a <span> (or renders a bare text node child).
  const child = el.firstChild
  return child && child.nodeType === 3 ? child : el
}

function makeDragEvent(type: string, x: number, y: number, target: EventTarget): DragEvent {
  const e = new Event(type, { bubbles: true, cancelable: true }) as unknown as DragEvent
  Object.defineProperty(e, 'clientX', { value: x })
  Object.defineProperty(e, 'clientY', { value: y })
  Object.defineProperty(e, 'dataTransfer', {
    value: {
      effectAllowed: '',
      dropEffect: '',
      setData: vi.fn(),
      getData: vi.fn(() => ''),
    },
  })
  // Allow overriding the target so dispatching on the root still resolves
  // e.target to the intended element.
  Object.defineProperty(e, 'target', { value: target, configurable: true })
  return e
}

function flushRaf() {
  const q = rafQueue
  rafQueue = []
  act(() => {
    for (const cb of q) cb(0)
  })
}

/**
 * Enable fake timers WITHOUT letting them take over `requestAnimationFrame`.
 * vitest's `useFakeTimers()` fakes rAF by default, which would swallow the
 * callbacks the plugin schedules and defeat `flushRaf()`. We drive rAF manually
 * through the `rafQueue` stub, so exclude it from the faked set and re-install
 * the queue stub (useFakeTimers replaces globals before we can stop it).
 */
function useFakeTimersKeepRaf() {
  vi.useFakeTimers({
    toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
  })
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback): number => {
    rafQueue.push(cb)
    return rafQueue.length
  })
  vi.stubGlobal('cancelAnimationFrame', () => {})
}

// --- setup / teardown --------------------------------------------------------

beforeEach(() => {
  rafQueue = []
  // The plugin (and DropGapNode's own mount effect) call the bare global
  // `requestAnimationFrame`, which resolves to globalThis — stub it there so a
  // deterministic queue backs both. Return a numeric handle.
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback): number => {
    rafQueue.push(cb)
    return rafQueue.length
  })
  vi.stubGlobal('cancelAnimationFrame', () => {})
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

// -----------------------------------------------------------------------------

describe('PillDragPlugin', () => {
  it('opens a gap inside text at the resolved index without changing serialized text', async () => {
    await mount()
    const { pill1, aaaaKey } = seed()
    const before = orderString()

    // Start dragging pill#1.
    const host1 = pillHostFor(pill1)
    act(() => {
      host1.dispatchEvent(makeDragEvent('dragstart', 90, 10, host1))
    })
    expect(host1.classList.contains('dragging')).toBe(true)

    // Drag over the middle of 'AAAA ' (offset 2) — a gap should open there.
    const textDom = textDomFor(aaaaKey)
    stubOverText(textDom, 2)
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('dragover', 30, 10, textDom.parentNode as EventTarget))
    })
    flushRaf()

    expect(gapCount()).toBe(1)
    // A gap in the tree does not change the serialized text/pill order.
    expect(orderString()).toBe(before)
    expect(paragraphOrder()).toContain('gap')
  })

  it('drop moves the node and the paragraph order changes accordingly', async () => {
    await mount()
    const { pill2, aaaaKey } = seed()
    // start: AAAA #1 BBBB #2
    expect(orderString()).toBe('AAAA #1 BBBB#2')

    const host2 = pillHostFor(pill2)
    act(() => {
      host2.dispatchEvent(makeDragEvent('dragstart', 110, 10, host2))
    })

    // Drop at offset 0 of 'AAAA ' — pill#2 should move to the very front.
    const textDom = textDomFor(aaaaKey)
    stubOverText(textDom, 0)
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('drop', 10, 10, textDom.parentNode as EventTarget))
    })

    expect(gapCount()).toBe(0)
    expect(orderString()).toBe('#2AAAA #1 BBBB')
  })

  it('undo after a drop restores the original document without a transient gap', async () => {
    await mount()
    const { pill2, aaaaKey } = seed()
    const original = orderString()

    const host2 = pillHostFor(pill2)
    act(() => {
      host2.dispatchEvent(makeDragEvent('dragstart', 110, 10, host2))
    })

    const textDom = textDomFor(aaaaKey)
    stubOverText(textDom, 0)
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('dragover', 10, 10, textDom.parentNode as EventTarget))
    })
    flushRaf()
    expect(paragraphOrder()).toContain('gap')

    act(() => {
      rootEl().dispatchEvent(makeDragEvent('drop', 10, 10, textDom.parentNode as EventTarget))
    })
    expect(orderString()).toBe('#2AAAA #1 BBBB')
    expect(paragraphOrder()).not.toContain('gap')

    act(() => {
      editorRef.dispatchCommand(UNDO_COMMAND, undefined)
    })
    expect(orderString()).toBe(original)
    expect(paragraphOrder()).not.toContain('gap')
  })

  it('drop onto the other pill right half lands after it with no nested .pill-host', async () => {
    await mount()
    const { pill1, pill2 } = seed()
    // Drag pill#1 onto the RIGHT half of pill#2 -> lands after pill#2.
    const host1 = pillHostFor(pill1)
    act(() => {
      host1.dispatchEvent(makeDragEvent('dragstart', 90, 10, host1))
    })

    const host2 = pillHostFor(pill2)
    stubOverPill(host2, 'right')
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('drop', 110, 10, host2))
    })

    expect(gapCount()).toBe(0)
    // #1 now follows #2.
    expect(orderString()).toBe('AAAA  BBBB#2#1')
    // No pill-host nested inside another pill-host.
    expect(rootEl().querySelectorAll('.pill-host .pill-host').length).toBe(0)
  })

  it('same-task dragover->drop race leaves zero gaps and still lands the node', async () => {
    await mount()
    const { pill2, aaaaKey } = seed()

    const host2 = pillHostFor(pill2)
    act(() => {
      host2.dispatchEvent(makeDragEvent('dragstart', 110, 10, host2))
    })

    // dragover schedules rAF work but drop fires BEFORE the frame runs (the
    // Chrome race). Drop resolves synchronously; the queued rAF must then bail.
    const textDom = textDomFor(aaaaKey)
    stubOverText(textDom, 0)
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('dragover', 10, 10, textDom.parentNode as EventTarget))
    })
    // Do NOT flush rAF yet — drop first.
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('drop', 10, 10, textDom.parentNode as EventTarget))
    })
    // Now the stale rAF fires: the race guard must drop it (dragKey is null).
    flushRaf()

    expect(gapCount()).toBe(0)
    expect(orderString()).toBe('#2AAAA #1 BBBB')
  })

  it('a cancelled drag (dragend without drop) removes the gap', async () => {
    useFakeTimersKeepRaf()
    // rAF is mocked to a queue; fake timers cover the 190ms gap-removal setTimeout.
    await mount()
    const { pill1, aaaaKey } = seed()

    const host1 = pillHostFor(pill1)
    act(() => {
      host1.dispatchEvent(makeDragEvent('dragstart', 90, 10, host1))
    })
    const textDom = textDomFor(aaaaKey)
    stubOverText(textDom, 2)
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('dragover', 30, 10, textDom.parentNode as EventTarget))
    })
    flushRaf()
    expect(gapCount()).toBe(1)

    // Cancel: dragend on the root, no drop.
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('dragend', 30, 10, host1))
    })
    // closeGap schedules removal after 190ms.
    act(() => {
      vi.advanceTimersByTime(200)
    })
    expect(gapCount()).toBe(0)
    expect(host1.classList.contains('dragging')).toBe(false)
  })

  it('undo after a cancelled drag does not restore the removed transient gap', async () => {
    useFakeTimersKeepRaf()
    await mount()
    const { pill1, aaaaKey } = seed()
    const original = orderString()

    const host1 = pillHostFor(pill1)
    act(() => {
      host1.dispatchEvent(makeDragEvent('dragstart', 90, 10, host1))
    })
    const textDom = textDomFor(aaaaKey)
    stubOverText(textDom, 2)
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('dragover', 30, 10, textDom.parentNode as EventTarget))
    })
    flushRaf()
    expect(paragraphOrder()).toContain('gap')

    act(() => {
      rootEl().dispatchEvent(makeDragEvent('dragend', 30, 10, host1))
      vi.advanceTimersByTime(200)
    })
    expect(orderString()).toBe(original)
    expect(paragraphOrder()).not.toContain('gap')

    act(() => {
      editorRef.dispatchCommand(UNDO_COMMAND, undefined)
    })
    expect(orderString()).toBe(original)
    expect(paragraphOrder()).not.toContain('gap')
  })

  it('drop dispatched on document.body (outside the editor) cleans up', async () => {
    useFakeTimersKeepRaf()
    await mount()
    const { pill1, aaaaKey } = seed()

    const host1 = pillHostFor(pill1)
    act(() => {
      host1.dispatchEvent(makeDragEvent('dragstart', 90, 10, host1))
    })
    const textDom = textDomFor(aaaaKey)
    stubOverText(textDom, 2)
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('dragover', 30, 10, textDom.parentNode as EventTarget))
    })
    flushRaf()
    expect(gapCount()).toBe(1)

    // Drop happens on body, outside the editor root: document capture handler
    // ends the drag.
    act(() => {
      document.body.dispatchEvent(makeDragEvent('drop', 5, 5, document.body))
    })
    act(() => {
      vi.advanceTimersByTime(200)
    })
    expect(gapCount()).toBe(0)
    expect(host1.classList.contains('dragging')).toBe(false)
  })

  it('dragstart puts the paste BODY in text/plain (what copy writes) and the node key only in a private type', async () => {
    await mount()
    const { pill1, aaaaKey } = seed()

    const host1 = pillHostFor(pill1)
    const ev = makeDragEvent('dragstart', 90, 10, host1)
    act(() => {
      host1.dispatchEvent(ev)
    })

    const setData = ev.dataTransfer!.setData as unknown as ReturnType<typeof vi.fn>
    const written = new Map<string, string>(setData.mock.calls as Array<[string, string]>)
    // A drop OUTSIDE this editor (another composer, a search box, an external
    // editor) only ever reads text/plain, so it must carry the paste body — the
    // same expansion COPY_COMMAND performs — not the token or a Lexical key.
    expect(written.get('text/plain')).toBe('one')
    // The node key is an internal handle: private flavour only, never leaked
    // through any flavour as `pill:<key>`.
    expect(written.get(PILL_KEY_MIME)).toBe(pill1)
    for (const v of written.values()) expect(v).not.toMatch(/^pill:/)

    // The in-editor drop path never consults dataTransfer: even a transfer that
    // hands back garbage lands the node at the resolved point.
    const textDom = textDomFor(aaaaKey)
    stubOverText(textDom, 0)
    const drop = makeDragEvent('drop', 10, 10, textDom.parentNode as EventTarget)
    ;(drop.dataTransfer!.getData as unknown as ReturnType<typeof vi.fn>).mockReturnValue('pill:garbage')
    act(() => {
      rootEl().dispatchEvent(drop)
    })
    expect(orderString()).toBe('#1AAAA  BBBB#2')
    // (Lexical core's own drop handler probes `application/x-lexical-drag`;
    // the flavours THIS plugin writes are never read back.)
    const readTypes = (drop.dataTransfer!.getData as unknown as ReturnType<typeof vi.fn>).mock.calls.map(c => c[0])
    expect(readTypes).not.toContain('text/plain')
    expect(readTypes).not.toContain(PILL_KEY_MIME)
  })

  it('watchdog cleans up a stranded gap after 900ms of no dragover', async () => {
    useFakeTimersKeepRaf()
    await mount()
    const { pill1, aaaaKey } = seed()

    const host1 = pillHostFor(pill1)
    act(() => {
      host1.dispatchEvent(makeDragEvent('dragstart', 90, 10, host1))
    })
    const textDom = textDomFor(aaaaKey)
    stubOverText(textDom, 2)
    act(() => {
      rootEl().dispatchEvent(makeDragEvent('dragover', 30, 10, textDom.parentNode as EventTarget))
    })
    flushRaf()
    expect(gapCount()).toBe(1)

    // No further dragover for 900ms -> watchdog fires endDrag, which closeGap()s
    // (removes after another 190ms).
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    act(() => {
      vi.advanceTimersByTime(200)
    })
    expect(gapCount()).toBe(0)
    expect(host1.classList.contains('dragging')).toBe(false)
  })
})

describe('setGhostDragImage', () => {
  it('hangs a translucent clone BELOW the cursor so the drop point is never covered', async () => {
    const { setGhostDragImage, GHOST_CLEARANCE_PX } = await import('../composer/plugins/PillDragPlugin')
    const host = document.createElement('span')
    host.className = 'pill-host'
    const chip = document.createElement('span')
    chip.setAttribute('data-paste-seq', '1')
    chip.textContent = 'Pasted text · 6 lines'
    chip.getBoundingClientRect = () => ({ width: 160, height: 24, top: 0, left: 0, right: 160, bottom: 24, x: 0, y: 0, toJSON() {} }) as DOMRect
    host.appendChild(chip)
    document.body.appendChild(host)
    const setDragImage = vi.fn()
    setGhostDragImage({ setDragImage } as unknown as DataTransfer, host)
    expect(setDragImage).toHaveBeenCalledTimes(1)
    const [img, x, y] = setDragImage.mock.calls[0] as [HTMLElement, number, number]
    // cursor anchored at the top-centre of a transparent band → chip starts below the pointer
    expect(x).toBe(80)
    expect(y).toBe(0)
    expect(img.style.paddingTop).toBe(`${GHOST_CLEARANCE_PX}px`)
    const clone = img.firstElementChild as HTMLElement
    expect(clone.textContent).toBe('Pasted text · 6 lines')
    expect(clone.hasAttribute('data-paste-seq')).toBe(false) // never a second real pill
    expect(Number(clone.style.opacity)).toBeCloseTo(0.72)
    expect(document.body.contains(img)).toBe(true) // must be in the DOM while the snapshot is taken
    await new Promise((r) => setTimeout(r, 5))
    expect(document.body.contains(img)).toBe(false) // then cleaned up
    host.remove()
  })
})
