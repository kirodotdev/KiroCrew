import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import * as ts from 'typescript'
import * as reactQuery from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'

const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

function uiExports(): string[] {
  const file = ts.createSourceFile('index.ts', read('../kirocrew-ui/index.ts'), ts.ScriptTarget.Latest)
  return file.statements.flatMap(statement => {
    if (!ts.isExportDeclaration(statement) || statement.isTypeOnly
      || !statement.exportClause || !ts.isNamedExports(statement.exportClause)) return []
    return statement.exportClause.elements.filter(element => !element.isTypeOnly).map(element => element.name.text)
  }).sort()
}

function stubExports(source: string): string[] {
  const file = ts.createSourceFile('stub.js', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
  return file.statements.flatMap(statement => {
    if (!ts.isVariableStatement(statement)
      || !statement.modifiers?.some(modifier => modifier.kind === ts.SyntaxKind.ExportKeyword)) return []
    return statement.declarationList.declarations.flatMap(declaration =>
      ts.isObjectBindingPattern(declaration.name)
        ? declaration.name.elements.map(element => element.name.getText(file)) : [],
    )
  }).sort()
}

// Execute the checked-in stub without its ESM declaration modifier. This exercises
// the host lookup and binding identities without Vite rewriting the vendor module.
function loadStub(source: string, key: string, host: object): Record<string, unknown> {
  const names = stubExports(source)
  return runInNewContext(`${source.replace(/export const/g, 'const')}\n;({${names.join(',')}})`, {
    window: { __kirocrew_modules: { [key]: host } },
  })
}

describe('app shared-module boundaries', () => {
  it('exports exactly the UI barrel values, without phantom or missing components', () => {
    const source = read('../../public/vendor/kirocrew-ui.mjs')
    const names = uiExports()
    expect(names.length).toBeGreaterThan(10)
    expect(stubExports(source)).toEqual(names)
    const host = Object.fromEntries(names.map(name => [name, () => name]))
    const exported = loadStub(source, '@kirocrew/ui', host)
    for (const name of names) expect(exported[name]).toBe(host[name])
  })

  it('maps the documented UI specifier and React Query to checked-in vendor stubs', () => {
    const config = read('../../vite.config.ts')
    for (const [specifier, filename] of [
      ['@kirocrew/app-sdk/ui', 'kirocrew-ui.mjs'],
      ['@tanstack/react-query', 'tanstack-react-query.mjs'],
    ]) {
      expect(config).toContain(`'${specifier}': '/vendor/${filename}'`)
      expect(read(`../../public/vendor/${filename}`)).toContain('window.__kirocrew_modules')
    }
  })

  it('shares every React Query runtime export with the host, including its context', () => {
    const source = read('../../public/vendor/tanstack-react-query.mjs')
    expect(stubExports(source)).toEqual(Object.keys(reactQuery).sort())
    const exported = loadStub(source, '@tanstack/react-query', reactQuery)
    for (const [name, value] of Object.entries(reactQuery)) expect(exported[name]).toBe(value)
  })

  it('fails clearly if the host query registry has not initialized', () => {
    const source = read('../../public/vendor/tanstack-react-query.mjs')
    expect(() => runInNewContext(source.replace(/export const/g, 'const'), { window: {} }))
      .toThrow('Host modules not initialized.')
  })

  it('documents the UI names that an external module can actually import', () => {
    const guide = read('../../../docs/app-kit/getting-started.md')
    const section = guide.split('## Shared UI Components')[1].split('\n## ')[0]
    expect([...section.matchAll(/`(\w+)`/g)].map(match => match[1]).sort()).toEqual(uiExports())
    expect(read('../kirocrew-ui/index.ts')).toContain("from '@kirocrew/app-sdk/ui'")
  })
})
