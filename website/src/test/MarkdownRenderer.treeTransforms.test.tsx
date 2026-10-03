import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import type { Root, Element, RootContent } from 'hast'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { rehypeMarkFencedCode, rehypeUnwrapBlocks } from '../components/markdown/treeTransforms'

/**
 * The structural tree passes on inputs that reach their less common branches.
 *
 * Rendered through the whole pipeline, a block written inside a paragraph never
 * reaches `rehypeUnwrapBlocks` still inside its `<p>`: the HTML parser behind
 * `rehype-raw` hoists it first, and the rendered markup below pins that outcome
 * (recorded against the renderer before its pipeline was split into modules).
 * The pass's own split and splice paths are exercised on trees built directly,
 * as is `rehypeMarkFencedCode`'s stripping of a forged `data-fenced` marker.
 */

/** Icons are reduced to their lucide class (their path data is lucide-react's),
 *  and `useId` values, which depend on earlier renders in the file, are masked. */
function html(content: string, sourcePos = false): string {
  const { container } = render(<MarkdownRenderer content={content} sourcePos={sourcePos} />)
  return container.innerHTML
    .replace(/<svg\b[^>]*class="([^"]*)"[^>]*>[\s\S]*?<\/svg>/g, '[icon $1]')
    .replace(/:r[0-9a-z]+:/g, ':id:')
}

describe('MarkdownRenderer structural tree passes', () => {
  it('renders a table written inside a paragraph outside any <p>', () => {
    const out = html('Hello <table><tr><td>cell</td></tr></table> world')
    expect(out).toMatchInlineSnapshot(`"<div class="group" data-image-scope="" data-tip-flow=""><div style="display: contents;"><p class="my-1 leading-6">Hello </p></div><div style="display: contents;"><div class="markdown-table my-3 group/table" data-testid="markdown-table"><div data-testid="table-scroller" data-overflow="" class="relative overflow-x-auto"><table class="min-w-full border-collapse text-sm [overflow-wrap:normal] [word-break:normal]"><tbody><tr><td class="px-3 py-2 border-b border-border text-sm">cell</td></tr></tbody></table></div><div class="mt-0.5 flex items-center justify-end gap-1 select-none opacity-0 group-hover/table:opacity-100 group-focus-within/table:opacity-100 transition-opacity [@media(hover:none)]:opacity-100 [@media(hover:none)]:flex-wrap [@media(hover:none)]:[&amp;_button]:p-3 [@media(hover:none)]:[&amp;_svg]:h-4 [@media(hover:none)]:[&amp;_svg]:w-4"><button type="button" data-testid="table-copy-markdown" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as Markdown" aria-label="Copy table as Markdown">[icon lucide lucide-copy]<span aria-hidden="true">Copy Markdown</span></button><button type="button" data-testid="table-copy-csv" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as CSV" aria-label="Copy table as CSV">[icon lucide lucide-file-spreadsheet]<span aria-hidden="true">Copy CSV</span></button></div></div></div> world<div style="display: contents;"><p class="my-1 leading-6"></p></div></div>"`)
  })

  it('keeps source positions on the paragraph and table the parser splits apart', () => {
    const out = html('Hello <table><tr><td>cell</td></tr></table> world', true)
    expect(out).toMatchInlineSnapshot(`"<div class="group" data-image-scope="" data-tip-flow=""><div data-block-start="1"><div style="display: contents;"><p data-sourcepos="1:1-1:7" class="my-1 leading-6">Hello </p></div><div style="display: contents;"><div class="markdown-table my-3 group/table" data-testid="markdown-table"><div data-testid="table-scroller" data-overflow="" class="relative overflow-x-auto"><table data-sourcepos="1:7-1:44" class="min-w-full border-collapse text-sm [overflow-wrap:normal] [word-break:normal]"><tbody><tr data-sourcepos="1:14-1:36"><td data-sourcepos="1:18-1:31" class="px-3 py-2 border-b border-border text-sm">cell</td></tr></tbody></table></div><div class="mt-0.5 flex items-center justify-end gap-1 select-none opacity-0 group-hover/table:opacity-100 group-focus-within/table:opacity-100 transition-opacity [@media(hover:none)]:opacity-100 [@media(hover:none)]:flex-wrap [@media(hover:none)]:[&amp;_button]:p-3 [@media(hover:none)]:[&amp;_svg]:h-4 [@media(hover:none)]:[&amp;_svg]:w-4"><button type="button" data-testid="table-copy-markdown" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as Markdown" aria-label="Copy table as Markdown">[icon lucide lucide-copy]<span aria-hidden="true">Copy Markdown</span></button><button type="button" data-testid="table-copy-csv" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as CSV" aria-label="Copy table as CSV">[icon lucide lucide-file-spreadsheet]<span aria-hidden="true">Copy CSV</span></button></div></div></div> world<div style="display: contents;"><p class="my-1 leading-6"></p></div></div></div>"`)
  })

  it('renders a whitespace-wrapped table as a raw block with no paragraph', () => {
    const out = html(' <table><tr><td>only</td></tr></table> ')
    expect(out).toMatchInlineSnapshot(`"<div class="group" data-image-scope="" data-tip-flow=""> <div style="display: contents;"><div class="markdown-table my-3 group/table" data-testid="markdown-table"><div data-testid="table-scroller" data-overflow="" class="relative overflow-x-auto"><table class="min-w-full border-collapse text-sm [overflow-wrap:normal] [word-break:normal]"><tbody><tr><td class="px-3 py-2 border-b border-border text-sm">only</td></tr></tbody></table></div><div class="mt-0.5 flex items-center justify-end gap-1 select-none opacity-0 group-hover/table:opacity-100 group-focus-within/table:opacity-100 transition-opacity [@media(hover:none)]:opacity-100 [@media(hover:none)]:flex-wrap [@media(hover:none)]:[&amp;_button]:p-3 [@media(hover:none)]:[&amp;_svg]:h-4 [@media(hover:none)]:[&amp;_svg]:w-4"><button type="button" data-testid="table-copy-markdown" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as Markdown" aria-label="Copy table as Markdown">[icon lucide lucide-copy]<span aria-hidden="true">Copy Markdown</span></button><button type="button" data-testid="table-copy-csv" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as CSV" aria-label="Copy table as CSV">[icon lucide lucide-file-spreadsheet]<span aria-hidden="true">Copy CSV</span></button></div></div></div> </div>"`)
  })

  it('renders several tables written inside one list item outside any <p>', () => {
    const out = html('- a <table><tr><td>1</td></tr></table> b <table><tr><td>2</td></tr></table> c')
    expect(out).toMatchInlineSnapshot(`
      "<div class="group" data-image-scope="" data-tip-flow=""><div style="display: contents;"><ul class="list-disc pl-8 my-2 space-y-1 marker:text-muted">
      <li class="text-sm leading-relaxed">a <div class="markdown-table my-3 group/table" data-testid="markdown-table"><div data-testid="table-scroller" data-overflow="" class="relative overflow-x-auto"><table class="min-w-full border-collapse text-sm [overflow-wrap:normal] [word-break:normal]"><tbody><tr><td class="px-3 py-2 border-b border-border text-sm">1</td></tr></tbody></table></div><div class="mt-0.5 flex items-center justify-end gap-1 select-none opacity-0 group-hover/table:opacity-100 group-focus-within/table:opacity-100 transition-opacity [@media(hover:none)]:opacity-100 [@media(hover:none)]:flex-wrap [@media(hover:none)]:[&amp;_button]:p-3 [@media(hover:none)]:[&amp;_svg]:h-4 [@media(hover:none)]:[&amp;_svg]:w-4"><button type="button" data-testid="table-copy-markdown" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as Markdown" aria-label="Copy table as Markdown">[icon lucide lucide-copy]<span aria-hidden="true">Copy Markdown</span></button><button type="button" data-testid="table-copy-csv" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as CSV" aria-label="Copy table as CSV">[icon lucide lucide-file-spreadsheet]<span aria-hidden="true">Copy CSV</span></button></div></div> b <div class="markdown-table my-3 group/table" data-testid="markdown-table"><div data-testid="table-scroller" data-overflow="" class="relative overflow-x-auto"><table class="min-w-full border-collapse text-sm [overflow-wrap:normal] [word-break:normal]"><tbody><tr><td class="px-3 py-2 border-b border-border text-sm">2</td></tr></tbody></table></div><div class="mt-0.5 flex items-center justify-end gap-1 select-none opacity-0 group-hover/table:opacity-100 group-focus-within/table:opacity-100 transition-opacity [@media(hover:none)]:opacity-100 [@media(hover:none)]:flex-wrap [@media(hover:none)]:[&amp;_button]:p-3 [@media(hover:none)]:[&amp;_svg]:h-4 [@media(hover:none)]:[&amp;_svg]:w-4"><button type="button" data-testid="table-copy-markdown" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as Markdown" aria-label="Copy table as Markdown">[icon lucide lucide-copy]<span aria-hidden="true">Copy Markdown</span></button><button type="button" data-testid="table-copy-csv" class="flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" title="Copy table as CSV" aria-label="Copy table as CSV">[icon lucide lucide-file-spreadsheet]<span aria-hidden="true">Copy CSV</span></button></div></div> c</li>
      </ul></div></div>"
    `)
  })

  it('strips a forged fenced marker from inline raw HTML in every casing', () => {
    const out = html('x <code data-fenced class="language-js">a</code> <code dataFenced class="language-js">b</code> <code data-Fenced="" class="language-js">c</code>')
    expect(out).toMatchInlineSnapshot(`"<div class="group" data-image-scope="" data-tip-flow=""><div style="display: contents;"><p class="my-1 leading-6">x <code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy a" data-chip-action="copy" aria-describedby=":id:">a</code><span role="status" aria-live="polite" class="sr-only"></span> <code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy b" data-chip-action="copy" aria-describedby=":id:">b</code><span role="status" aria-live="polite" class="sr-only"></span> <code class="bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono cursor-copy underline decoration-dotted decoration-muted underline-offset-2" node="[object Object]" role="button" tabindex="0" aria-label="Copy c" data-chip-action="copy" aria-describedby=":id:">c</code><span role="status" aria-live="polite" class="sr-only"></span></p></div></div>"`)
  })
})

const el = (tagName: string, children: RootContent[] = [], properties: Record<string, unknown> = {}): Element =>
  ({ type: 'element', tagName, properties, children } as Element)
const text = (value: string) => ({ type: 'text' as const, value })

/** A compact spelling of a tree: `p(Hello |div(x)| world)`. */
function shape(nodes: RootContent[]): string {
  return nodes.map(n => (n.type === 'text' ? n.value : n.type === 'element' ? `${n.tagName}(${shape(n.children as RootContent[])})` : n.type)).join('|')
}

describe('rehypeUnwrapBlocks on a paragraph that still holds a block', () => {
  // The HTML parser hoists raw blocks out of a <p> before this pass runs, so the
  // split only happens on a tree some other pass built; it is exercised here on
  // such a tree directly.
  const run = (tree: Root) => { (rehypeUnwrapBlocks() as (t: Root) => void)(tree); return shape(tree.children) }

  it('splits the text on either side into their own paragraphs', () => {
    const position = { start: { line: 1, column: 1 }, end: { line: 1, column: 20 } }
    const p = el('p', [text('Hello '), el('div', [text('x')]), text(' world')], { className: ['lead'] })
    p.position = position
    const tree: Root = { type: 'root', children: [p] }
    expect(run(tree)).toBe('p(Hello )|div(x)|p( world)')
    const [before, , after] = tree.children as Element[]
    expect(before.properties).toEqual({ className: ['lead'] })
    expect(after.position).toBe(position)
  })

  it('drops a whitespace-only bucket and keeps element-only ones', () => {
    const tree: Root = { type: 'root', children: [el('p', [text('  '), el('div', [text('x')]), el('em', [text('y')]), el('section', []), text('\n')])] }
    expect(run(tree)).toBe('div(x)|p(em(y))|section()')
  })

  it('splices replacements at every depth without skipping a sibling', () => {
    const inner = el('p', [text('a'), el('ul', [el('li', [text('i')])]), text('b')])
    const sibling = el('p', [el('div', [text('d')]), text('plain')])
    const tree: Root = { type: 'root', children: [el('blockquote', [inner, sibling]), el('p', [el('hr'), text('tail')])] }
    expect(run(tree)).toBe('blockquote(p(a)|ul(li(i))|p(b)|div(d)|p(plain))|hr()|p(tail)')
  })

  it('leaves a paragraph with only inline children alone', () => {
    const tree: Root = { type: 'root', children: [el('p', [text('a'), el('strong', [text('b')])])] }
    expect(run(tree)).toBe('p(a|strong(b))')
  })
})

describe('rehypeMarkFencedCode on a tree built directly', () => {
  const run = (tree: Root) => { (rehypeMarkFencedCode() as (t: Root) => void)(tree); return tree }

  it('marks only a code element whose parent is a pre, and strips every forged marker', () => {
    const fenced = el('code', [text('a')], { dataFenced: 'x' })
    const inline = el('code', [text('b')], { 'data-fenced': '', 'data-Fenced': '1', datafenced: true })
    const tree = run({ type: 'root', children: [el('pre', [fenced]), el('p', [inline])] })
    expect(fenced.properties).toEqual({ 'data-fenced': '' })
    expect(inline.properties).toEqual({})
    expect(shape(tree.children)).toBe('pre(code(a))|p(code(b))')
  })
})
