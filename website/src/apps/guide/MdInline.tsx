// MdInline — renders the guide's lightweight markdown as React nodes.
//
// Ports the Artifactory renderer's mdInline/autolinkIds without ever injecting a
// raw HTML string (the frontend-security lint forbids dangerouslySetInnerHTML):
// every token becomes a real element. Supports fenced code blocks (dark block +
// Copy), `inline code`, **bold**, [text](url) and bare-URL links, numbered
// lists, and entry-id autolinking (a slug that is a real entry id becomes an
// in-page cross-reference; unknown slugs stay plain text, so no dead links).
import { Fragment, type ReactNode } from 'react'
import CodeBlock from './CodeBlock'

interface Ctx {
  ids: Set<string>
  onSelect: (id: string) => void
}

const RE = {
  link: /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
  code: /`([^`]+)`/g,
  bold: /\*\*([^*]+)\*\*/g,
  url: /https?:\/\/[^\s<]+[^\s<.,;:!?)\]]/g,
  id: /[a-z][a-z0-9]*(?:-[a-z0-9]+)+/g,
  numbered: /^\s*\d+\.\s+/,
}

function firstMatch(re: RegExp, text: string, from: number): RegExpExecArray | null {
  re.lastIndex = from
  return re.exec(text)
}

/** Inline tokens within one line: links, code, bold, bare URLs, entry-id xrefs. */
function inlineNodes(text: string, ctx: Ctx, keyBase: string): ReactNode[] {
  const out: ReactNode[] = []
  let i = 0
  let k = 0
  while (i < text.length) {
    const link = firstMatch(RE.link, text, i)
    const code = firstMatch(RE.code, text, i)
    const bold = firstMatch(RE.bold, text, i)
    const url = firstMatch(RE.url, text, i)
    // Nearest entry-id token that is actually a real id (skip non-ids).
    let idm: RegExpExecArray | null = null
    RE.id.lastIndex = i
    let cand: RegExpExecArray | null
    while ((cand = RE.id.exec(text))) {
      if (ctx.ids.has(cand[0])) {
        idm = cand
        break
      }
    }

    const cands = [link, code, bold, url, idm].filter(Boolean) as RegExpExecArray[]
    if (!cands.length) {
      out.push(<Fragment key={`${keyBase}-t${k++}`}>{text.slice(i)}</Fragment>)
      break
    }
    const next = cands.reduce((a, b) => (b.index < a.index ? b : a))
    if (next.index > i) {
      out.push(<Fragment key={`${keyBase}-t${k++}`}>{text.slice(i, next.index)}</Fragment>)
    }
    const kk = `${keyBase}-n${k++}`
    if (next === link) {
      out.push(
        <a key={kk} href={next[2]} target="_blank" rel="noopener noreferrer"
           className="underline focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
           style={{ color: 'var(--accent)' }}>{next[1]}</a>,
      )
    } else if (next === code) {
      out.push(
        <code key={kk} className="px-1 py-0.5 rounded text-[0.85em] font-mono"
              style={{ background: 'var(--card)', border: '1px solid var(--border)' }}>{next[1]}</code>,
      )
    } else if (next === bold) {
      out.push(<strong key={kk}>{next[1]}</strong>)
    } else if (next === url) {
      out.push(
        <a key={kk} href={next[0]} target="_blank" rel="noopener noreferrer"
           className="underline break-all focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
           style={{ color: 'var(--accent)' }}>{next[0]}</a>,
      )
    } else {
      const id = next[0]
      out.push(
        <button key={kk} type="button" onClick={() => ctx.onSelect(id)}
                className="underline font-mono text-[0.9em] focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
                style={{ color: 'var(--accent)' }}>{id}</button>,
      )
    }
    i = next.index + next[0].length
  }
  return out
}

/** One text block: numbered runs become <ol>, everything else a paragraph. */
function blockNodes(block: string, ctx: Ctx, keyBase: string): ReactNode[] {
  const lines = block.split('\n')
  const parts: ReactNode[] = []
  let para: string[] = []
  let list: string[] = []
  let p = 0
  const flushPara = () => {
    if (!para.length) return
    parts.push(
      <p key={`${keyBase}-p${p++}`} className="text-sm my-1">
        {para.map((ln, idx) => (
          <Fragment key={idx}>
            {idx > 0 && <br />}
            {inlineNodes(ln, ctx, `${keyBase}-p${p}-${idx}`)}
          </Fragment>
        ))}
      </p>,
    )
    para = []
  }
  const flushList = () => {
    if (!list.length) return
    parts.push(
      <ol key={`${keyBase}-o${p++}`} className="list-decimal ml-5 text-sm my-1 flex flex-col gap-1">
        {list.map((ln, idx) => (
          <li key={idx}>{inlineNodes(ln.replace(RE.numbered, ''), ctx, `${keyBase}-o${p}-${idx}`)}</li>
        ))}
      </ol>,
    )
    list = []
  }
  for (const ln of lines) {
    if (RE.numbered.test(ln)) {
      flushPara()
      list.push(ln)
    } else {
      flushList()
      para.push(ln)
    }
  }
  flushList()
  flushPara()
  return parts
}

const FENCE = /```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n([\s\S]*?)```/g

export default function MdInline({
  text,
  ids,
  onSelect,
}: {
  text: string | undefined
  ids: Set<string>
  onSelect: (id: string) => void
}) {
  const src = text == null ? '' : String(text)
  if (!src.trim()) return null
  const ctx: Ctx = { ids, onSelect }
  const out: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  let k = 0
  FENCE.lastIndex = 0
  const emitText = (chunk: string) => {
    for (const block of chunk.split(/\n{2,}/)) {
      if (block.replace(/\s/g, '')) out.push(...blockNodes(block, ctx, `b${k++}`))
    }
  }
  while ((m = FENCE.exec(src))) {
    if (m.index > last) emitText(src.slice(last, m.index))
    out.push(<CodeBlock key={`c${k++}`} code={m[1].replace(/\r?\n$/, '')} />)
    last = m.index + m[0].length
  }
  if (last < src.length) emitText(src.slice(last))
  return <>{out}</>
}
