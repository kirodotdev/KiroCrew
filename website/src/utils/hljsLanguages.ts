// Shared highlight.js language registration. Imported by BOTH the main-thread
// hljs instance (utils/hljs.ts) and the highlight Web Worker (hljsWorker.ts),
// so the registered language set stays identical without duplicating the list.
import type { HLJSApi } from 'highlight.js'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import yaml from 'highlight.js/lib/languages/yaml'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import sql from 'highlight.js/lib/languages/sql'
import rust from 'highlight.js/lib/languages/rust'
import java from 'highlight.js/lib/languages/java'
import markdown from 'highlight.js/lib/languages/markdown'
import { reportSeamCollision } from '../apps/seamCollision'
import { HIGHLIGHT_LANGUAGES, type ResolvedHighlightLanguage } from './highlightLanguages'

export function registerHljsLanguages(
  hljs: HLJSApi,
  contributions: readonly ResolvedHighlightLanguage[] = HIGHLIGHT_LANGUAGES,
): void {
  hljs.registerLanguage('javascript', javascript)
  hljs.registerLanguage('js', javascript)
  hljs.registerLanguage('jsx', javascript)
  hljs.registerLanguage('typescript', typescript)
  hljs.registerLanguage('ts', typescript)
  hljs.registerLanguage('tsx', typescript)
  hljs.registerLanguage('python', python)
  hljs.registerLanguage('py', python)
  hljs.registerLanguage('bash', bash)
  hljs.registerLanguage('sh', bash)
  hljs.registerLanguage('shell', bash)
  hljs.registerLanguage('zsh', bash)
  hljs.registerLanguage('json', json)
  hljs.registerLanguage('yaml', yaml)
  hljs.registerLanguage('yml', yaml)
  hljs.registerLanguage('xml', xml)
  hljs.registerLanguage('html', xml)
  hljs.registerLanguage('css', css)
  hljs.registerLanguage('sql', sql)
  hljs.registerLanguage('rust', rust)
  hljs.registerLanguage('rs', rust)
  hljs.registerLanguage('java', java)
  hljs.registerLanguage('markdown', markdown)
  hljs.registerLanguage('md', markdown)

  // Edition languages (see highlightLanguages.ts). Registered after the core
  // set so a name the core already owns is detected and the core keeps it.
  for (const lang of contributions) {
    if (!lang.hljs) continue
    const taken = [lang.id, ...lang.aliases].find(name => hljs.getLanguage(name))
    if (taken !== undefined) {
      reportSeamCollision('highlightLanguages', `hljs language '${taken}' is a core language; ignoring '${lang.id}'`)
      continue
    }
    try {
      hljs.registerLanguage(lang.id, lang.hljs)
      if (lang.aliases.length > 0) hljs.registerAliases(lang.aliases, { languageName: lang.id })
    } catch {
      hljs.unregisterLanguage(lang.id)
      reportSeamCollision('highlightLanguages', `hljs language '${lang.id}' failed to register; ignoring it`)
    }
  }
}
