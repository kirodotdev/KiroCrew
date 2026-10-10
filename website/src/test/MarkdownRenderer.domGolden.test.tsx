import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, cleanup } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'

/**
 * Golden DOM for the markdown pipeline as a whole.
 *
 * The other `MarkdownRenderer.*` specs each pin one behaviour with targeted
 * queries. This one pins the COMPOSITION: which element override renders which
 * markup, in which order the remark/rehype passes ran, which block renderer a
 * fence dispatches to, and what every prop variant adds. A change to any of
 * those moves bytes here, so an exact `innerHTML` match is the evidence that a
 * restructuring of the renderer left its output alone.
 *
 * The block components the renderer dispatches to (code, diff, excalidraw,
 * widgets, the resize wrapper) are replaced by inert markers: their own markup
 * is theirs to pin, and some of it settles asynchronously. What stays real is
 * everything the renderer itself decides — the dispatch, the props it hands
 * over, and all of the inline markdown.
 */

vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: true }),
}))
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(), copyCode: vi.fn() }))
vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(() => new Promise(() => {})) },
}))

function marker(name: string) {
  return function Marker(props: Record<string, unknown>) {
    const attrs: Record<string, string> = {}
    for (const [k, v] of Object.entries(props)) {
      if (k === 'children' || typeof v === 'function' || v === undefined) continue
      attrs[`data-${k.toLowerCase()}`] = String(v)
    }
    return React.createElement('div', { 'data-mock': name, ...attrs }, props.children as React.ReactNode)
  }
}
vi.mock('../components/CodeBlock', () => ({ CodeBlock: marker('CodeBlock') }))
vi.mock('../components/EditableCodeBlock', () => ({ default: marker('EditableCodeBlock') }))
vi.mock('../components/DiffBlock', () => ({ default: marker('DiffBlock') }))
vi.mock('../components/FoldableDiffBlock', () => ({ default: marker('FoldableDiffBlock'), resetExpandedDiffFences: () => {} }))
vi.mock('../components/ExcalidrawBlock', () => ({ ExcalidrawBlock: marker('ExcalidrawBlock'), default: marker('ExcalidrawBlock') }))
vi.mock('../components/WidgetFrame', () => ({ default: marker('WidgetFrame') }))
vi.mock('../components/WidgetPlaceholder', () => ({ default: marker('WidgetPlaceholder') }))
vi.mock('../components/SmoothResize', () => ({ SmoothResize: marker('SmoothResize') }))

const realFetch = globalThis.fetch

/** Every probe answers "missing", so path chips settle unconfirmed. */
function stubMissing() {
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: false, status: 404, headers: new Headers() } as Response),
  ) as unknown as typeof fetch
}

/** Every probe confirms a file. */
function stubFile() {
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, status: 200, headers: new Headers({ 'X-Path-Kind': 'file' }) } as Response),
  ) as unknown as typeof fetch
}

beforeEach(() => {
  __resetPathKindCache()
  stubMissing()
})

afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
})

/** An icon is reduced to what the renderer chose: its lucide class list, size
 *  and whether it is hidden from assistive tech. Its path data belongs to
 *  lucide-react. */
function iconMarker(_: string, attrs: string): string {
  const cls = /class="([^"]*)"/.exec(attrs)?.[1] ?? ''
  const size = /width="([^"]*)"/.exec(attrs)?.[1] ?? ''
  const hidden = /aria-hidden="true"/.test(attrs) ? ' aria-hidden' : ''
  return `[icon ${size} ${cls}${hidden}]`
}

/** `useId` values depend on how many ids earlier renders in the file minted, so
 *  they are masked; icons are reduced by `iconMarker`; nothing else changes. */
function html(container: HTMLElement): string {
  return container.innerHTML
    .replace(/:r[0-9a-z]+:/g, ':id:')
    .replace(/<svg\b([^>]*)>[\s\S]*?<\/svg>/g, iconMarker)
}

const PROSE = [
  '# Title *em*',
  '## Sub **bold**',
  '### Three',
  '#### Four',
  '##### Five',
  '###### Six',
  '',
  'Para with `npm test`, `src/main.py:12`, `~/notes/`, `chat-1380-1789049480` and a [link](https://example.com/x).',
  'A forge ref https://github.com/o/r/pull/12 and an artifact [doc](/artifacts/my%20doc) and ~~strike~~ text.',
  '',
  '> quoted *line*',
  '',
  '---',
  '',
  '- a',
  '- [ ] task',
  '- [x] done',
  '',
  '3. three',
  '4. four',
  '',
  '<ol type="a" start="2"><li>x</li></ol>',
  '',
  '| h1 | h2 |',
  '|:--|--:|',
  '| `c` | 2 |',
  '',
  '![alt text](/tmp/shot.png)',
  '',
  '![remote](https://img.example/a.png)',
  '',
  '<video controls src="https://v.example/a.mp4"></video>',
  '',
  '<details><summary>more</summary>body</details>',
  '',
  '<unknownTag attr="1">kept verbatim</unknownTag>',
  '',
  '<div onclick="x()" style="color:red" data-message-edit="1" data-ok="1">raw div</div>',
  '',
  '<code class="language-js">inline classed</code> and <script>alert(1)</script>',
  '',
  '$$x^2$$ and $9.99 plus $5',
  '',
  'Footnote[^1] and （https://example.com/pull/1，`abc`）：`ready`',
  '',
  '[q](https://x.example/new?title=a b&labels=bug)',
  '',
  '[^1]: the note',
].join('\n')

const FENCES = [
  'Created /repo/src/a.ts:',
  '',
  '```diff',
  '@@ -1 +1 @@',
  '-a',
  '+b',
  '```',
  '',
  '```js',
  'const a = 1',
  '```',
  '',
  '```markdown',
  '# card',
  '```',
  '',
  '```excalidraw',
  '{"elements":[]}',
  '```',
  '',
  'tail text',
].join('\n')

describe('MarkdownRenderer golden DOM', () => {
  it('renders the prose document', async () => {
    const { container } = render(<MarkdownRenderer content={PROSE} />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(html(container)).toMatchInlineSnapshot(`
      "<div class="group" data-image-scope="" data-tip-flow="" data-guide-untrusted=""><div style="display: contents;"><h1 id="title-em" class="text-xl font-bold mt-4 mb-2 text-text-strong">Title <em class="italic">em</em></h1></div>
      <div style="display: contents;"><h2 id="sub-bold" class="text-lg font-bold mt-3 mb-2 text-text-strong">Sub <strong class="font-semibold text-text-strong">bold</strong></h2></div>
      <div style="display: contents;"><h3 id="three" class="text-base font-semibold mt-3 mb-1.5 text-text-strong">Three</h3></div>
      <div style="display: contents;"><h4 id="four" class="text-sm font-semibold mt-2 mb-1 text-text-strong">Four</h4></div>
      <div style="display: contents;"><h5 id="five" class="text-sm font-medium mt-2 mb-1 text-text-strong">Five</h5></div>
      <div style="display: contents;"><h6 id="six" class="text-[13px] font-medium mt-2 mb-1 text-muted">Six</h6></div>
      <div style="display: contents;"><p class="my-1 leading-6">Para with <code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy npm test" data-chip-action="copy" aria-describedby=":id:">npm test</code><span role="status" aria-live="polite" class="sr-only"></span>, <code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy src/main.py:12" data-chip-action="copy" aria-describedby=":id:">[icon 12 lucide lucide-file-code inline align-middle mr-1 opacity-0 aria-hidden]src/main.py:12</code><span role="status" aria-live="polite" class="sr-only"></span>, <code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy ~/notes/" data-chip-action="copy" aria-describedby=":id:">[icon 12 lucide lucide-file inline align-middle mr-1 opacity-0 aria-hidden]~/notes/</code><span role="status" aria-live="polite" class="sr-only"></span>, <code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy chat-1380-1789049480" data-chip-action="copy" aria-describedby=":id:">chat-1380-1789049480</code><span role="status" aria-live="polite" class="sr-only"></span> and a <a href="https://example.com/x" target="_blank" rel="noopener noreferrer" class="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent">link</a>.
      A forge ref <span class="group inline-flex max-w-full items-center gap-1 rounded-md border border-border/60 bg-accent/10 px-1.5 py-px align-baseline text-[13px] mc-md-ref-chip transition-colors hover:border-border hover:bg-accent/20 focus-within:border-border"><a href="https://github.com/o/r/pull/12" target="_blank" rel="noopener noreferrer" title="https://github.com/o/r/pull/12" class="inline-flex min-w-0 items-center gap-1.5 text-text no-underline focus-ring"><span aria-hidden="true" class="inline-block shrink-0" data-provider-mark="github" style="width: 12px; height: 12px; background-color: currentcolor;"></span><span class="truncate max-w-[32ch]">o/r#12</span></a></span> and an artifact <a href="/artifacts/my%20doc" target="_blank" rel="noopener noreferrer" class="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent">doc</a> and <del>strike</del> text.</p></div>
      <div style="display: contents;"><blockquote class="border-l-[3px] border-accent pl-3 my-2 text-muted italic">
      <p class="my-1 leading-6">quoted <em class="italic">line</em></p>
      </blockquote></div>
      <div style="display: contents;"><hr class="border-border my-4"></div>
      <div style="display: contents;"><ul class="list-none pl-4 my-2 space-y-1">
      <li class="text-sm leading-relaxed">a</li>
      <li class="text-sm leading-relaxed break-words pl-5 -indent-5 [&amp;_input[type=checkbox]]:mr-1.5 [&amp;_input[type=checkbox]]:align-middle [&amp;>ul]:indent-0 [&amp;>ol]:indent-0 [&amp;>p:not(:first-child)]:indent-0 [&amp;>ul]:mt-1 [&amp;>ol]:mt-1"><input type="checkbox" disabled=""> task</li>
      <li class="text-sm leading-relaxed break-words pl-5 -indent-5 [&amp;_input[type=checkbox]]:mr-1.5 [&amp;_input[type=checkbox]]:align-middle [&amp;>ul]:indent-0 [&amp;>ol]:indent-0 [&amp;>p:not(:first-child)]:indent-0 [&amp;>ul]:mt-1 [&amp;>ol]:mt-1"><input type="checkbox" disabled="" checked=""> done</li>
      </ul></div>
      <div style="display: contents;"><ol start="3" class="list-decimal pl-8 my-2 space-y-1 marker:text-muted">
      <li class="text-sm leading-relaxed">three</li>
      <li class="text-sm leading-relaxed">four</li>
      </ol></div>
      <div style="display: contents;"><ol type="a" start="2" style="list-style-type: lower-alpha;" class="pl-8 my-2 space-y-1 marker:text-muted"><li class="text-sm leading-relaxed">x</li></ol></div>













      <div style="display: contents;"><div class="markdown-table my-3 group/table" data-testid="markdown-table"><div data-testid="table-scroller" data-overflow="" class="relative overflow-x-auto"><table class="min-w-full border-collapse text-sm [overflow-wrap:normal] [word-break:normal]"><thead><tr><th class="text-left text-muted text-[13px] font-medium px-3 py-2 border-b border-border bg-bg-elevated whitespace-nowrap">h1</th><th class="text-left text-muted text-[13px] font-medium px-3 py-2 border-b border-border bg-bg-elevated whitespace-nowrap">h2</th></tr></thead><tbody><tr><td class="px-3 py-2 border-b border-border text-sm"><code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy c" data-chip-action="copy" aria-describedby=":id:">c</code><span role="status" aria-live="polite" class="sr-only"></span></td><td class="px-3 py-2 border-b border-border text-sm">2</td></tr></tbody></table></div><div class="mt-0.5 flex items-center justify-end gap-1 select-none opacity-0 group-hover/table:opacity-100 group-focus-within/table:opacity-100 transition-opacity [@media(hover:none)]:opacity-100 [@media(hover:none)]:flex-wrap [@media(hover:none)]:[&amp;_button]:p-3 [@media(hover:none)]:[&amp;_svg]:h-4 [@media(hover:none)]:[&amp;_svg]:w-4"><button type="button" data-testid="table-expand" class="markdown-table-expand flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" aria-expanded="false" title="Widen table" aria-label="Widen table">[icon 13 lucide lucide-unfold-horizontal aria-hidden]<span aria-hidden="true">Widen</span></button><button type="button" data-testid="table-copy-markdown" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as Markdown" aria-label="Copy table as Markdown">[icon 13 lucide lucide-copy aria-hidden]<span aria-hidden="true">Copy Markdown</span></button><button type="button" data-testid="table-copy-csv" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as CSV" aria-label="Copy table as CSV">[icon 13 lucide lucide-file-spreadsheet aria-hidden]<span aria-hidden="true">Copy CSV</span></button></div></div></div>
      <div style="display: contents;"><p class="my-1 leading-6"><span class="relative block my-2" tabindex="-1"><span aria-hidden="true" class="pointer-events-none absolute top-0 start-0 flex items-center justify-center overflow-hidden rounded-md border border-border bg-bg-accent max-w-[min(100%,760px)] max-h-[60vh]" style="width: 420px; height: 236px;"><span class="absolute inset-0 animate-pulse bg-bg-hover"></span>[icon 28 lucide lucide-image relative animate-pulse text-muted aria-hidden]</span><img src="/api/file-raw?path=%2Ftmp%2Fshot.png" alt="alt text" loading="lazy" class="max-w-[min(100%,760px)] max-h-[60vh] object-contain rounded-md border border-border cursor-pointer hover:opacity-90 transition-opacity" style="width: 420px; height: 236px;" data-lightbox-image="" title="alt text"></span></p></div>
      <div style="display: contents;"><p class="my-1 leading-6"><button type="button" title="https://img.example/a.png" class="group/remote-media inline-flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-border-strong bg-bg-hover px-2.5 py-1.5 text-sm text-muted cursor-pointer transition-colors hover:border-accent hover:bg-bg-elevated">[icon 14 lucide lucide-image shrink-0 aria-hidden]<span class="font-medium text-text transition-colors group-hover/remote-media:text-accent">External image blocked — click to load</span><span class="mt-0.5 block basis-full text-start text-[11px] leading-relaxed text-muted">Site: <span class="break-all font-mono text-[12px] font-medium text-text">img.example</span></span><span class="block basis-full text-start text-[11px] leading-relaxed text-muted">For privacy, loads only this file from the site shown, one time.</span><span class="block basis-full text-start text-[11px] leading-relaxed text-muted">Described in the message as: “remote”</span></button></p></div>
      <div style="display: contents;"><p class="my-1 leading-6"><button type="button" title="https://v.example/a.mp4" class="group/remote-media inline-flex max-w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-border-strong bg-bg-hover px-2.5 py-1.5 text-sm text-muted cursor-pointer transition-colors hover:border-accent hover:bg-bg-elevated">[icon 14 lucide lucide-film shrink-0 aria-hidden]<span class="font-medium text-text transition-colors group-hover/remote-media:text-accent">External video blocked — click to load</span><span class="mt-0.5 block basis-full text-start text-[11px] leading-relaxed text-muted">Site: <span class="break-all font-mono text-[12px] font-medium text-text">v.example</span></span><span class="block basis-full text-start text-[11px] leading-relaxed text-muted">For privacy, loads only this file from the site shown, one time.</span></button></p></div>
      <div style="display: contents;"><details><summary>more</summary>body</details></div>
      <div style="display: contents;"><p class="my-1 leading-6">&lt;unknownTag attr="1"&gt;kept verbatim&lt;/unknownTag&gt;</p></div>
      <div style="display: contents;"><div data-ok="1">raw div</div></div>
      <div style="display: contents;"><p class="my-1 leading-6"><code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy inline classed" data-chip-action="copy" aria-describedby=":id:">inline classed</code><span role="status" aria-live="polite" class="sr-only"></span> and <span class="escaped-tag">[unsupported: script]</span></p></div>
      <div style="display: contents;"><p class="my-1 leading-6"><span class="katex"><span class="katex-mathml"><math xmlns="http://www.w3.org/1998/Math/MathML"><semantics><mrow><msup><mi>x</mi><mn>2</mn></msup></mrow><annotation encoding="application/x-tex">x^2</annotation></semantics></math></span><span class="katex-html" aria-hidden="true"><span class="base"><span class="strut" style="height: 0.8141em;"></span><span class="mord"><span class="mord mathnormal">x</span><span class="msupsub"><span class="vlist-t"><span class="vlist-r"><span class="vlist" style="height: 0.8141em;"><span style="top: -3.063em; margin-right: 0.05em;"><span class="pstrut" style="height: 2.7em;"></span><span class="sizing reset-size6 size3 mtight"><span class="mord mtight">2</span></span></span></span></span></span></span></span></span></span></span> and $9.99 plus $5</p></div>
      <div style="display: contents;"><p class="my-1 leading-6">Footnote<sup><a href="#user-content-fn-1" class="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent">1</a></sup> and （<a href="https://example.com/pull/1" target="_blank" rel="noopener noreferrer" class="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent">https://example.com/pull/1</a>，<code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy abc" data-chip-action="copy" aria-describedby=":id:">abc</code><span role="status" aria-live="polite" class="sr-only"></span>）：<code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy ready" data-chip-action="copy" aria-describedby=":id:">ready</code><span role="status" aria-live="polite" class="sr-only"></span></p></div>
      <div style="display: contents;"><p class="my-1 leading-6"><a href="https://x.example/new?title=a%20b&amp;labels=bug" target="_blank" rel="noopener noreferrer" class="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent">q</a></p></div>
      <div style="display: contents;"><section data-footnotes="" class="footnotes"><h2 id="footnotes" class="text-lg font-bold mt-3 mb-2 text-text-strong">Footnotes</h2>
      <ol class="list-decimal pl-8 my-2 space-y-1 marker:text-muted">
      <li class="text-sm leading-relaxed">
      <p class="my-1 leading-6">the note <a href="#user-content-fnref-1" class="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent">↩</a></p>
      </li>
      </ol>
      </section></div></div>"
    `)
  })

  it('renders fences through their block renderers', () => {
    const { container } = render(<MarkdownRenderer content={FENCES} />)
    expect(html(container)).toMatchInlineSnapshot(`
      "<div class="group" data-image-scope="" data-tip-flow="" data-guide-untrusted=""><div style="display: contents;"><p class="my-1 leading-6">Created /repo/src/a.ts:</p></div><div data-mock="DiffBlock" data-code="@@ -1 +1 @@
      -a
      +b" data-complete="true" data-pathhint="/repo/src/a.ts"></div><div data-mock="EditableCodeBlock" data-code="const a = 1" data-lang="js" data-complete="true"></div><div data-mock="EditableCodeBlock" data-code="# card" data-lang="markdown" data-complete="true"></div><div data-mock="ExcalidrawBlock" data-code="{&quot;elements&quot;:[]}"></div><div style="display: contents;"><p class="my-1 leading-6">tail text</p></div></div>"
    `)
  })

  it('renders the transcript-only fence options', () => {
    const { container } = render(
      <MarkdownRenderer content={FENCES} mdCardToggle collapseDiffs readOnlyCode slotKey="slot-1" messageTs="2026-09-27T10:00:00Z" smooth />,
    )
    expect(html(container)).toMatchInlineSnapshot(`
      "<div class="group ft-anim-smooth" data-image-scope="" data-tip-flow="" data-guide-untrusted=""><div style="display: contents;"><p class="my-1 leading-6">Created /repo/src/a.ts:</p></div><div data-mock="SmoothResize" data-enabled="false"><div data-mock="FoldableDiffBlock" data-code="@@ -1 +1 @@
      -a
      +b" data-complete="true" data-pathhint="/repo/src/a.ts" data-foldkey="slot-1:2026-09-27T10:00:00Z:4"></div></div><div data-mock="SmoothResize" data-enabled="false"><div data-mock="CodeBlock" data-code="const a = 1" data-lang="js" data-complete="true"></div></div><div data-mock="SmoothResize" data-enabled="false"><div class="my-2"><div class="flex items-center justify-end mb-1"><div role="radiogroup" class="isolate inline-flex rounded-lg bg-bg-elevated border border-border p-0.5 gap-0.5 "><button type="button" role="radio" aria-checked="true" tabindex="0" title="Render the markdown - headings, lists, tables, links" class="relative flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium border-none transition-colors  text-accent cursor-pointer"><div class="absolute inset-0 bg-card rounded-md shadow-sm border border-border"></div><span class="relative z-[1] overflow-hidden whitespace-nowrap" style="width: 0px;">Formatted</span></button><button type="button" role="radio" aria-checked="false" tabindex="-1" title="Show the exact markdown source" class="relative flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium border-none transition-colors  text-muted hover:text-text hover:bg-bg-hover cursor-pointer"><span class="relative z-[1] overflow-hidden whitespace-nowrap" style="width: 0px;">Raw</span></button></div></div><div><div style="display: contents;"><h1 id="card" class="text-xl font-bold mt-4 mb-2 text-text-strong">card</h1></div></div><div class="hidden"><div data-mock="EditableCodeBlock" data-code="# card" data-lang="markdown" data-complete="true"></div></div></div></div><div data-mock="ExcalidrawBlock" data-code="{&quot;elements&quot;:[]}"></div><div style="display: contents;"><p class="my-1 leading-6">tail text</p></div></div>"
    `)
  })

  it('renders a streaming tail with glow', () => {
    const { container } = render(<MarkdownRenderer content={'Intro **bold** and `code`\n\n| a | b |'} streaming glow />)
    expect(html(container)).toMatchInlineSnapshot(`"<div class="group" data-image-scope="" data-tip-flow="" data-guide-untrusted=""><div style="display: contents;"><p class="my-1 leading-6">Intro <strong class="font-semibold text-text-strong">bold</strong><span class="streaming-glow"> and </span><code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy code" data-chip-action="copy" aria-describedby=":id:">code</code><span role="status" aria-live="polite" class="sr-only"></span><span class="streaming-caret" aria-hidden="true"></span></p></div></div>"`)
  })

  it('renders a streaming tail with the smooth reveal', () => {
    const { container } = render(<MarkdownRenderer content={'Settled paragraph.\n\nThe newest words arrive here now'} streaming glow smooth />)
    expect(html(container)).toMatchInlineSnapshot(`
      "<div class="group ft-anim-smooth ft-streaming" data-image-scope="" data-tip-flow="" data-guide-untrusted=""><div style="display: contents;"><p class="my-1 leading-6">Settled paragraph.</p></div>
      <div style="display: contents;"><p class="my-1 leading-6"><span class="ft-word" style="--ft-o: 1;">T</span><span class="ft-word" style="--ft-o: 0.99;">h</span><span class="ft-word" style="--ft-o: 0.97;">e</span><span class="ft-word" style="--ft-o: 0.96;"> </span><span class="ft-word" style="--ft-o: 0.95;">n</span><span class="ft-word" style="--ft-o: 0.94;">e</span><span class="ft-word" style="--ft-o: 0.92;">w</span><span class="ft-word" style="--ft-o: 0.91;">e</span><span class="ft-word" style="--ft-o: 0.9;">s</span><span class="ft-word" style="--ft-o: 0.88;">t</span><span class="ft-word" style="--ft-o: 0.87;"> </span><span class="ft-word" style="--ft-o: 0.86;">w</span><span class="ft-word" style="--ft-o: 0.85;">o</span><span class="ft-word" style="--ft-o: 0.83;">r</span><span class="ft-word" style="--ft-o: 0.82;">d</span><span class="ft-word" style="--ft-o: 0.81;">s</span><span class="ft-word" style="--ft-o: 0.79;"> </span><span class="ft-word" style="--ft-o: 0.78;">a</span><span class="ft-word" style="--ft-o: 0.77;">r</span><span class="ft-word" style="--ft-o: 0.75;">r</span><span class="ft-word" style="--ft-o: 0.74;">i</span><span class="ft-word" style="--ft-o: 0.73;">v</span><span class="ft-word" style="--ft-o: 0.72;">e</span><span class="ft-word" style="--ft-o: 0.7;"> </span><span class="ft-word" style="--ft-o: 0.69;">h</span><span class="ft-word" style="--ft-o: 0.68;">e</span><span class="ft-word" style="--ft-o: 0.66;">r</span><span class="ft-word" style="--ft-o: 0.65;">e</span><span class="ft-word" style="--ft-o: 0.64;"> </span><span class="ft-word" style="--ft-o: 0.63;">n</span><span class="ft-word" style="--ft-o: 0.61;">o</span><span class="ft-word" style="--ft-o: 0.6;">w</span><span class="streaming-caret" aria-hidden="true"></span></p></div></div>"
    `)
  })

  it('renders source positions', () => {
    const { container } = render(<MarkdownRenderer content={'# H\n\ntext with https://example.com/a（b）\n\n- item'} sourcePos />)
    expect(html(container)).toMatchInlineSnapshot(`
      "<div class="group" data-image-scope="" data-tip-flow="" data-guide-untrusted=""><div data-block-start="1"><div style="display: contents;"><h1 data-sourcepos="1:1-1:4" id="h" class="text-xl font-bold mt-4 mb-2 text-text-strong">H</h1></div>
      <div style="display: contents;"><p data-sourcepos="3:1-3:35" class="my-1 leading-6">text with <a data-sourcepos="3:11-3:35" href="https://example.com/a%EF%BC%88b%EF%BC%89" target="_blank" rel="noopener noreferrer" class="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent">https://example.com/a（b）</a></p></div>
      <div style="display: contents;"><ul data-sourcepos="5:1-5:7" class="list-disc pl-8 my-2 space-y-1 marker:text-muted">
      <li data-sourcepos="5:1-5:7" class="text-sm leading-relaxed">item</li>
      </ul></div></div></div>"
    `)
  })

  it('renders a user message with soft breaks and compact images', () => {
    const { container } = render(<MarkdownRenderer content={'line one\nline two\n![shot](/tmp/a.png)\n![two](/tmp/b.png)'} softBreaks compactImages messageTs="1789049480" />)
    expect(html(container)).toMatchInlineSnapshot(`
      "<div class="group" data-image-scope="" data-tip-flow="" data-guide-untrusted=""><div style="display: contents;"><p class="my-1 leading-6">line one<br>
      line two<span class="relative block my-2" tabindex="-1"><span aria-hidden="true" class="pointer-events-none absolute top-0 end-0 flex items-center justify-center overflow-hidden rounded-md border border-border bg-bg-accent max-w-[240px] max-h-[180px]" style="width: 240px; height: 180px;"><span class="absolute inset-0 animate-pulse bg-bg-hover"></span>[icon 28 lucide lucide-image relative animate-pulse text-muted aria-hidden]</span><img src="/api/file-raw?path=%2Ftmp%2Fa.png&amp;v=1789049480" alt="shot" loading="lazy" class="ms-auto max-w-[240px] max-h-[180px] object-contain rounded-md border border-border cursor-pointer hover:opacity-90 transition-opacity" style="width: 240px; height: 180px;" data-lightbox-image="" title="shot"></span><span class="relative block my-2" tabindex="-1"><span aria-hidden="true" class="pointer-events-none absolute top-0 end-0 flex items-center justify-center overflow-hidden rounded-md border border-border bg-bg-accent max-w-[240px] max-h-[180px]" style="width: 240px; height: 180px;"><span class="absolute inset-0 animate-pulse bg-bg-hover"></span>[icon 28 lucide lucide-image relative animate-pulse text-muted aria-hidden]</span><img src="/api/file-raw?path=%2Ftmp%2Fb.png&amp;v=1789049480" alt="two" loading="lazy" class="ms-auto max-w-[240px] max-h-[180px] object-contain rounded-md border border-border cursor-pointer hover:opacity-90 transition-opacity" style="width: 240px; height: 180px;" data-lightbox-image="" title="two"></span></p></div></div>"
    `)
  })

  it('renders raw mode as preformatted text', () => {
    const { container } = render(<MarkdownRenderer content={'# not a heading'} rawMode />)
    expect(html(container)).toMatchInlineSnapshot(`"<pre class="text-[13px] font-mono whitespace-pre-wrap break-words leading-relaxed text-muted"># not a heading</pre>"`)
  })

  it('renders confirmed path and session chips', async () => {
    stubFile()
    const sessions = new Map([['chat-1380-1789049480', 'Other chat']])
    const { container } = render(
      <MarkdownRenderer
        content={'Open `src/main.py:12` or `chat-1380-1789049480` or [s](/chat?sid=chat-1380-1789049480).'}
        onFileOpen={() => {}}
        onSessionOpen={() => {}}
        sessions={sessions}
        activeSession="chat-1-1"
      />,
    )
    await waitFor(() => expect(container.querySelector('code[data-path]')).not.toBeNull())
    expect(html(container)).toMatchInlineSnapshot(`
      "<div class="group" data-image-scope="" data-tip-flow="" data-guide-untrusted=""><div style="display: contents;"><p class="my-1 leading-6">Open <code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono text-accent cursor-pointer hover:underline" role="button" tabindex="0" node="[object Object]" data-path="src/main.py:12" data-path-kind="file" data-chip-action="navigate" aria-label="Open src/main.py:12" title="src/main.py:12
      Click to open / Shift+click to show in file manager
      Ctrl/Cmd+click to copy" data-state="closed">[icon 12 lucide lucide-file inline align-middle mr-1 opacity-70 aria-hidden]src/main.py:12</code> or <code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono text-accent cursor-pointer hover:underline" role="button" tabindex="0" node="[object Object]" data-session-key="chat-1380-1789049480" data-chip-action="navigate" aria-label="Switch to session chat-1380-1789049480" title="Other chat
      Click to switch to this session
      Ctrl/Cmd+click to copy">[icon 12 lucide lucide-message-square inline align-middle mr-1 opacity-70 aria-hidden]chat-1380-1789049480</code> or <a href="/chat?sid=chat-1380-1789049480" title="Other chat
      Click to switch to this session" class="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent">s</a>.</p></div></div>"
    `)
  })
})
