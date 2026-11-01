/**
 * Turn a CodeMirror-style style spec (`{ selector: { camelCase: value } }`) into
 * plain CSS under a scope, so a surface outside the editor (the whiteboard) wears
 * exactly the styles the editor's theme gives the same markup.
 */
export type StyleSpec = Record<string, Record<string, string>>

const kebab = (prop: string) => prop.replace(/[A-Z]/g, c => `-${c.toLowerCase()}`)

export function specToCss(spec: StyleSpec, scope: string): string {
  return Object.entries(spec)
    .filter(([selector]) => !selector.startsWith('@'))
    .map(([selector, rules]) => {
      const scoped = selector.split(',').map(s => `${scope} ${s.trim()}`).join(', ')
      const body = Object.entries(rules).map(([k, v]) => `${kebab(k)}:${v}`).join(';')
      return `${scoped}{${body}}`
    })
    .join('\n')
}
