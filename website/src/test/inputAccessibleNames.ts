/**
 * Shared scan for text inputs that show only a placeholder.
 *
 * A placeholder is not a label: it vanishes once the field has text, and
 * assistive tech does not reliably announce it. Each app's own test calls
 * `unnamedTextInputs` on its files, so this core module imports no app.
 */
import fs from 'node:fs'

/** Every `<input …>` opening tag in a source file, comments stripped first. */
export function inputTags(absPath: string): string[] {
  const s = fs.readFileSync(absPath, 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
  const tags: string[] = []
  let i = 0
  while ((i = s.indexOf('<input', i)) >= 0) {
    let j = i + 6
    let depth = 0
    for (; j < s.length; j++) {
      const c = s[j]
      if (c === '{') depth++
      else if (c === '}') depth--
      else if (c === '>' && depth === 0) break
    }
    tags.push(s.slice(i, j + 1))
    i = j
  }
  return tags
}

/** Text-input tags with no `aria-label`, `aria-labelledby` or `id` (for a `<label htmlFor>`). */
export function unnamedTextInputs(absPath: string): string[] {
  return inputTags(absPath).filter(tag =>
    !/type=["'](hidden|checkbox|radio|file|submit|range|color)["']/.test(tag)
    // A spread of the caller's props may carry the name; a handler spread
    // such as `{...ime.bindEnter(...)}` cannot.
    && !/aria-label|aria-labelledby|\bid=|\{\.\.\.(props|rest|inputProps)\b/.test(tag))
}
