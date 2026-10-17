// Demo Kiro Crew session control — proves the composer-bar contribution works
// end-to-end. Loaded dynamically by SessionControlHost via
// import('/apps/demo-app/ui/session-control.mjs'), so the module's DEFAULT export
// must be a component: the host mounts it with React.lazy.
//
// Same host-module convention as index.mjs next door, and no JSX for the same
// reason — this file is served exactly as written, with no build step.

const React = window.__kirocrew_modules.react
const { Tag } = window.__kirocrew_modules['lucide-react']

const { createElement: h } = React

/** One labelled row. Values are rendered verbatim so a wrong one is visible. */
function row(label, value, mono) {
  return h('div', { className: 'flex items-baseline gap-2 py-0.5' },
    h('span', { className: 'text-[11px] text-muted min-w-[92px]' }, label),
    h('span', {
      className: 'text-[11px] text-text break-all' + (mono ? ' font-mono' : ''),
    }, value || h('span', { className: 'text-muted' }, 'not set')),
  )
}

/**
 * The whole point of the slot: an app page is routed and session-blind, so it
 * cannot report which chat the user is in. This control just shows what the host
 * handed it, which is what makes the contract observable.
 *
 * @param {{ session: { sessionKey: string, folderId?: string, folderName?: string, cwd: string }, onClose: () => void }} props
 */
export default function DemoSessionControl({ session, onClose }) {
  const s = session || {}
  return h('div', { className: 'p-3 min-w-[260px]' },
    h('div', { className: 'flex items-center gap-1.5 mb-2' },
      h(Tag, { size: 13 }),
      h('span', { className: 'text-[12px] font-medium text-text' }, 'Demo session control'),
    ),
    h('div', { className: 'mb-2 pb-2 border-b border-border' },
      row('chat', s.sessionKey, true),
      // folderName is the display half of the folder pair: folderId identifies,
      // folderName is what a person recognises, so a control that says where a
      // setting will apply needs both.
      row('folder', s.folderName, false),
      row('folder id', s.folderId, true),
      row('cwd', s.cwd, true),
    ),
    h('div', { className: 'text-[10px] text-muted mb-2' },
      'These four values come from the chat this chip was opened in. A routed app ' +
      'page cannot see any of them.',
    ),
    h('button', {
      onClick: () => onClose && onClose(),
      title: 'Close this control',
      'aria-label': 'Close this control',
      className:
        'text-[11px] text-muted hover:text-text px-2 py-1 rounded border ' +
        'border-border bg-transparent cursor-pointer',
    }, 'Close'),
  )
}
