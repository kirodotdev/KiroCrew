import type { editor } from 'monaco-editor'

/**
 * One Dark palette matching the highlight.js theme in index.css.
 * Monaco's Monarch tokenizer emits a limited set of tokens per language
 * (e.g. Java: keyword, string, number, comment, annotation, delimiter).
 * Tokens like 'identifier', 'type', 'function' are NOT emitted by most
 * Monarch grammars — they fall through to the default foreground.
 */

export const kirocrewDark: editor.IStandaloneThemeData = {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: 'keyword', foreground: 'c678dd' },
    { token: 'keyword.non-sealed', foreground: 'c678dd' },
    { token: 'string', foreground: '98c379' },
    { token: 'string.escape', foreground: '56b6c2' },
    { token: 'comment', foreground: '7f848e', fontStyle: 'italic' },
    { token: 'comment.doc', foreground: '7f848e', fontStyle: 'italic' },
    { token: 'number', foreground: 'd19a66' },
    { token: 'number.float', foreground: 'd19a66' },
    { token: 'number.hex', foreground: 'd19a66' },
    { token: 'number.binary', foreground: 'd19a66' },
    { token: 'number.octal', foreground: 'd19a66' },
    { token: 'annotation', foreground: 'e06c75' },
    { token: 'tag', foreground: 'e06c75' },
    { token: 'metatag', foreground: 'e06c75' },
    { token: 'attribute.name', foreground: 'd19a66' },
    { token: 'attribute.value', foreground: '98c379' },
    { token: 'delimiter', foreground: 'abb2bf' },
    { token: 'operator', foreground: 'abb2bf' },
    { token: 'type', foreground: 'e5c07b' },
    { token: 'type.identifier', foreground: 'e5c07b' },
    { token: '', foreground: 'abb2bf' },
  ],
  colors: {
    'editor.background': '#00000000',
    'editor.foreground': '#abb2bf',
  },
}

export const kirocrewLight: editor.IStandaloneThemeData = {
  base: 'vs',
  inherit: true,
  rules: [
    { token: 'keyword', foreground: 'a626a4' },
    { token: 'keyword.non-sealed', foreground: 'a626a4' },
    { token: 'string', foreground: '50a14f' },
    { token: 'string.escape', foreground: '0184bc' },
    { token: 'comment', foreground: 'a0a1a7', fontStyle: 'italic' },
    { token: 'comment.doc', foreground: 'a0a1a7', fontStyle: 'italic' },
    { token: 'number', foreground: '986801' },
    { token: 'number.float', foreground: '986801' },
    { token: 'number.hex', foreground: '986801' },
    { token: 'number.binary', foreground: '986801' },
    { token: 'number.octal', foreground: '986801' },
    { token: 'annotation', foreground: 'e45649' },
    { token: 'tag', foreground: 'e45649' },
    { token: 'metatag', foreground: 'e45649' },
    { token: 'attribute.name', foreground: '986801' },
    { token: 'attribute.value', foreground: '50a14f' },
    { token: 'delimiter', foreground: '383a42' },
    { token: 'operator', foreground: '383a42' },
    { token: 'type', foreground: 'c18401' },
    { token: 'type.identifier', foreground: 'c18401' },
    { token: '', foreground: '383a42' },
  ],
  colors: {
    'editor.background': '#00000000',
    'editor.foreground': '#383a42',
  },
}
