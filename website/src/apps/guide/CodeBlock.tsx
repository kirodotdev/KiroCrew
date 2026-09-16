// A dark, monospace code block with a Copy button — for fenced blocks and step cmd.
import CopyButton from './CopyButton'

export default function CodeBlock({ code }: { code: string }) {
  return (
    <div
      className="my-2 rounded-lg overflow-hidden"
      style={{ background: 'var(--panel-strong, var(--bg))', border: '1px solid var(--border)' }}
    >
      <div className="flex items-start justify-between gap-2 p-2.5">
        <pre className="text-xs font-mono whitespace-pre-wrap overflow-x-auto flex-1 m-0" style={{ color: 'var(--text)' }}>{code}</pre>
        <CopyButton text={code} className="shrink-0" />
      </div>
    </div>
  )
}
