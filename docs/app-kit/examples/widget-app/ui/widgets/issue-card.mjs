// The `issue-card` widget of the example app. Runs in the sandboxed app frame and is
// handed only the JSON payload of one `<mcwidget use="issue-cards:issue-card">` tag.
// The payload is agent output, so every value is set with textContent, never innerHTML.

const COLORS = {
  light: { fg: '#1f2328', muted: '#59636e', open: '#1a7f37', closed: '#8250df', chip: '#eaeef2' },
  dark: { fg: '#e6edf3', muted: '#9198a1', open: '#3fb950', closed: '#a371f7', chip: '#30363d' },
}

function text(tag, value, style) {
  const el = document.createElement(tag)
  el.textContent = String(value ?? '')
  Object.assign(el.style, style)
  return el
}

export function activate(ctx) {
  let root = null
  let last = null

  function draw(props, theme) {
    const c = COLORS[theme] || COLORS.light
    root?.remove()
    root = document.createElement('div')
    Object.assign(root.style, { font: '13px system-ui, sans-serif', color: c.fg, padding: '8px 12px' })
    const state = props.state === 'closed' ? 'closed' : 'open'
    const head = document.createElement('div')
    head.append(
      text('span', state, { color: c[state], fontWeight: '600', marginRight: '8px' }),
      text('span', `#${Number(props.number) || '?'}`, { color: c.muted, marginRight: '6px' }),
    )
    // Plain text: a link to another host opens nothing from an app widget.
    head.append(text('span', props.title || 'Untitled issue', { color: c.fg, fontWeight: '600' }))
    root.append(head)
    const labels = Array.isArray(props.labels) ? props.labels.slice(0, 8) : []
    if (labels.length) {
      const row = document.createElement('div')
      row.style.marginTop = '6px'
      for (const label of labels) {
        row.append(text('span', label, { background: c.chip, borderRadius: '10px', padding: '1px 8px', marginRight: '4px', fontSize: '11px' }))
      }
      root.append(row)
    }
    document.body.append(root)
  }

  return {
    render(props, view) {
      last = props
      draw(props, view.theme)
      view.resize({ height: document.body.scrollHeight })
    },
    theme(next) {
      if (last) draw(last, next)
    },
    dispose() {
      root?.remove()
    },
  }
}

// Behind an authenticating reverse proxy the frame runs this module inline, where no
// export can be read, so it also hands over its entry.
globalThis.kirocrew?.register(activate)
