/**
 * A minimal LaTeX/BibTeX tokenizer registered with Monaco.
 *
 * Monaco bundles ~60 language grammars but none for TeX, so a `.tex` file would
 * otherwise render as undifferentiated plaintext — in a document that is mostly
 * backslash commands and braces, that is the difference between readable and
 * not. This is deliberately a Monarch tokenizer rather than a full grammar: it
 * colours the four things a writer scans for (commands, environment names,
 * comments, math) and leaves prose alone.
 *
 * Registration is idempotent and defensive: if it throws for any reason the
 * editor still mounts, because `monacoLanguage()` resolving to an unregistered
 * id makes Monaco fall back to plaintext rather than fail.
 */
import type { Monaco } from '@monaco-editor/react'
import type { languages } from 'monaco-editor'
import { LATEX_LANGUAGE_ID } from './lib'

let registered = false

export function registerLatexLanguage(monaco: Monaco): void {
  if (registered) return
  registered = true
  try {
    const existing = monaco.languages
      .getLanguages()
      .some((language: languages.ILanguageExtensionPoint) => language.id === LATEX_LANGUAGE_ID)
    if (existing) return

    monaco.languages.register({
      id: LATEX_LANGUAGE_ID,
      extensions: ['.tex', '.sty', '.cls', '.bib'],
      aliases: ['LaTeX', 'latex', 'BibTeX'],
    })

    monaco.languages.setLanguageConfiguration(LATEX_LANGUAGE_ID, {
      comments: { lineComment: '%' },
      brackets: [
        ['{', '}'],
        ['[', ']'],
        ['(', ')'],
      ],
      autoClosingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '$', close: '$' },
      ],
      surroundingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '$', close: '$' },
      ],
    })

    monaco.languages.setMonarchTokensProvider(LATEX_LANGUAGE_ID, {
      defaultToken: '',
      tokenizer: {
        root: [
          // `% comment` — but not an escaped `\%`, which is a literal percent
          // sign and extremely common in a results table.
          [/(^|[^\\])(%.*$)/, ['', 'comment']],
          // \begin{env} / \end{env} — the environment name is the useful part.
          [/(\\(?:begin|end))(\s*)(\{)([^}]*)(\})/, ['keyword', '', 'delimiter.curly', 'type', 'delimiter.curly']],
          // Sectioning commands read as structure, so they get their own colour.
          [/\\(?:part|chapter|(?:sub){0,2}section|paragraph|subparagraph)\*?/, 'keyword.control'],
          // Any other command.
          [/\\[a-zA-Z@]+\*?/, 'keyword'],
          // An escaped character (\%, \&, \_, \$) is literal text, not a command.
          [/\\./, 'string.escape'],
          // Display and inline math.
          [/\$\$/, { token: 'string', next: '@displayMath' }],
          [/\$/, { token: 'string', next: '@inlineMath' }],
          [/[{}[\]()]/, 'delimiter'],
          // BibTeX entry types, so a .bib file is legible in the same editor.
          [/@[a-zA-Z]+/, 'type'],
        ],
        inlineMath: [
          [/[^$\\]+/, 'string'],
          [/\\./, 'string.escape'],
          [/\$/, { token: 'string', next: '@pop' }],
        ],
        displayMath: [
          [/[^$\\]+/, 'string'],
          [/\\./, 'string.escape'],
          [/\$\$/, { token: 'string', next: '@pop' }],
          [/\$/, 'string'],
        ],
      },
    })
  } catch {
    // Monaco falls back to plaintext for an unregistered id, so a failure here
    // costs highlighting and nothing else. Never let it break the editor mount.
    registered = false
  }
}
