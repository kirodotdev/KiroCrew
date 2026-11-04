/**
 * SAIL (Appian expression language) syntax highlighting, ported from the token
 * rules of the Appian SAIL IntelliJ plugin's lexer (`sail.flex` in
 * sail-intellij-plugin): domains (`a!`, `fn!`, `rule!`, `type!`, `ri!`,
 * `local!`, ...), let-functions (`a!localVariables`, `with`, `load`, ...),
 * `/* *\/` comments, `""`-escaped strings, numbers, keyword args and operators.
 *
 * Two languages:
 *  - `sail`     -- bare expressions (`.sail` files, ```sail fences).
 *  - `sail-xml` -- Appian content XML (system rules, `SYSTEM_SYSRULES_*.xml`):
 *                  XML tags, with SAIL highlighted inside `<![CDATA[ ... ]]>`.
 *                  `.xml` is a core extension, so the file surfaces pick this
 *                  language by file name/content (see `isSailXmlFile`).
 *
 * Worker-safe data only (no React/DOM): this module is loaded by the highlight
 * worker bundles too.
 */
import type { LanguageRegistration } from '@pierre/diffs'
import type { HighlightLanguageContribution } from '../utils/highlightLanguages'

const IDENT = '[A-Za-z_][A-Za-z0-9_]*'

/** The plugin's FN_LET_FUNCTION | A_LET_FUNCTION | EVAL_FUNCTION sets. */
const LET_FNS = [
  'with', 'load', 'lambda_appian_internal', 'withsinglethreaded_appian_internal',
  'chain[rR]eaction', 'local[vV]ariables', 'eval[wW]ith[mM]ocks', 'eval[lL]exical',
  'eval[wW]ith[oO]bject[rR]eferences', 'with[eE]val[sS]tate', 'eval',
].join('|')

/** Variable-like domains from the plugin's DOMAIN list. */
const VAR_DOMAINS = 'ri|local|fv|rv|rf|rp|rsp|rvp|rip|rt|pv|pp|pvp|ac|tp|pm|bind|entry|save|env|item|index|context|data|record|grid|chart|flow'

const repository: LanguageRegistration['repository'] = {
  expr: {
    patterns: [
      { include: '#comment' },
      { include: '#string' },
      { include: '#number' },
      { include: '#letFunction' },
      { include: '#quotedDomain' },
      { include: '#ruleCall' },
      { include: '#functionCall' },
      { include: '#typeRef' },
      { include: '#constant' },
      { include: '#variable' },
      { include: '#genericDomain' },
      { include: '#keywordArg' },
      { include: '#literal' },
      { include: '#operator' },
      { include: '#punctuation' },
    ],
  },
  comment: { name: 'comment.block.sail', begin: '/\\*', end: '\\*/' },
  string: {
    name: 'string.quoted.double.sail',
    begin: '"',
    end: '"(?!")',
    patterns: [{ name: 'constant.character.escape.sail', match: '""' }],
  },
  number: { name: 'constant.numeric.sail', match: '\\b(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\\b' },
  letFunction: {
    match: `\\b(?:(a|fn)(!))?(${LET_FNS})\\b(?=\\s*\\()`,
    captures: {
      1: { name: 'constant.numeric.sail' },
      2: { name: 'constant.numeric.sail' },
      3: { name: 'keyword.control.let.sail' },
    },
  },
  quotedDomain: {
    match: `'(rule|a|fn|type|cons)(!)([^']+)'`,
    captures: {
      1: { name: 'constant.numeric.sail' },
      2: { name: 'constant.numeric.sail' },
      3: { name: 'support.function.sail' },
    },
  },
  ruleCall: {
    match: `\\b(rule)(!)(${IDENT})`,
    captures: {
      1: { name: 'constant.numeric.sail' },
      2: { name: 'constant.numeric.sail' },
      3: { name: 'support.function.rule.sail' },
    },
  },
  functionCall: {
    match: `\\b(a|fn)(!)(${IDENT})`,
    captures: {
      1: { name: 'constant.numeric.sail' },
      2: { name: 'constant.numeric.sail' },
      3: { name: 'support.function.sail' },
    },
  },
  typeRef: {
    match: `\\b(type)(!)(\\{[^}]*\\})?(${IDENT})`,
    captures: {
      1: { name: 'constant.numeric.sail' },
      2: { name: 'constant.numeric.sail' },
      3: { name: 'entity.name.type.sail' },
      4: { name: 'entity.name.type.sail' },
    },
  },
  constant: {
    match: `\\b(cons)(!)(${IDENT})`,
    captures: {
      1: { name: 'constant.numeric.sail' },
      2: { name: 'constant.numeric.sail' },
      3: { name: 'constant.other.sail' },
    },
  },
  variable: {
    match: `\\b(${VAR_DOMAINS})(!)(${IDENT})`,
    captures: {
      1: { name: 'constant.numeric.sail' },
      2: { name: 'constant.numeric.sail' },
      3: { name: 'variable.domain.sail' },
    },
  },
  genericDomain: {
    match: '\\b([A-Za-z]+)(!)',
    captures: {
      1: { name: 'constant.numeric.sail' },
      2: { name: 'constant.numeric.sail' },
    },
  },
  keywordArg: {
    match: `\\b(${IDENT})\\s*(:)(?!=)`,
    captures: {
      1: { name: 'meta.keyword-arg.sail' },
      2: { name: 'meta.keyword-arg.sail' },
    },
  },
  literal: { name: 'constant.language.sail', match: '\\b(?:true|false|null)\\b' },
  operator: { name: 'meta.operator.sail', match: '<=|>=|<>|\\.\\.|[+\\-*/^&=<>%~#]' },
  punctuation: { name: 'meta.operator.sail', match: '[,;]' },
}

const sailGrammar: LanguageRegistration = {
  name: 'sail',
  scopeName: 'source.sail',
  displayName: 'SAIL',
  patterns: [{ include: '#expr' }],
  repository,
} as LanguageRegistration

const sailXmlGrammar: LanguageRegistration = {
  name: 'sail-xml',
  scopeName: 'text.xml.sail',
  displayName: 'Appian content XML (SAIL)',
  patterns: [
    {
      name: 'meta.embedded.block.sail',
      begin: '(<!\\[CDATA\\[)',
      end: '(\\]\\]>)',
      beginCaptures: { 1: { name: 'punctuation.definition.tag.sail' } },
      endCaptures: { 1: { name: 'punctuation.definition.tag.sail' } },
      patterns: [{ include: '#expr' }],
    },
    { name: 'comment.block.xml', begin: '<!--', end: '-->' },
    { name: 'meta.tag.preprocessor.xml', begin: '<\\?', end: '\\?>', patterns: [{ include: '#xmlAttr' }] },
    {
      name: 'meta.tag.xml',
      begin: '(</?)([A-Za-z_][\\w:.-]*)',
      end: '(/?>)',
      beginCaptures: { 1: { name: 'punctuation.definition.tag.xml' }, 2: { name: 'entity.name.tag.xml' } },
      endCaptures: { 1: { name: 'punctuation.definition.tag.xml' } },
      patterns: [{ include: '#xmlAttr' }],
    },
    { name: 'constant.character.entity.xml', match: '&(?:[A-Za-z]+|#[0-9]+|#x[0-9a-fA-F]+);' },
  ],
  repository: {
    ...repository,
    xmlAttr: {
      patterns: [
        { name: 'entity.other.attribute-name.xml', match: '[A-Za-z_:][\\w:.-]*(?=\\s*=)' },
        { name: 'string.quoted.double.xml', begin: '"', end: '"' },
        { name: 'string.quoted.single.xml', begin: "'", end: "'" },
      ],
    },
  },
} as LanguageRegistration

export const SAIL_LANGUAGES: HighlightLanguageContribution[] = [
  { id: 'sail', aliases: ['appian'], extensions: ['.sail'], textmate: async () => ({ default: [sailGrammar] }) },
  { id: 'sail-xml', textmate: async () => ({ default: [sailXmlGrammar] }) },
]

/** An Appian content-XML rule file: highlight it as `sail-xml` rather than
 *  plain `xml`. By name (`SYSTEM_SYSRULES_*.xml`) or by a content-haul root. */
export function isSailXmlFile(filePath: string | undefined, content?: string): boolean {
  if (!filePath || !/\.xml$/i.test(filePath)) return false
  const name = filePath.split(/[\\/]/).pop() ?? ''
  if (/^SYSTEM_SYSRULES_/i.test(name)) return true
  return !!content && /<contentHaul[\s>]/.test(content.slice(0, 2000))
}
