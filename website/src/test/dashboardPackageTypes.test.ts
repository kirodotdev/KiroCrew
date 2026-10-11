/**
 * The dashboard package's TS types, pinned to the schema they were written from,
 * and that schema pinned to the python catalogs that generate it.
 *
 * A TS union is erased at runtime, so nothing about `DashboardFieldType` can
 * fail at run time when python grows a sixth data type -- the browser simply
 * starts calling a valid package unreadable, silently, forever. These cases are
 * the only thing standing between that and a shipped build, which is why they
 * pin BOTH joins of the chain rather than the end of it:
 *
 *     python catalogs -> package_json_schema() -> dashboardPackage.schema.json -> dashboardPackage.ts
 *                    (a)                     (b)                            (c)
 *
 * (a) is the package line's own python test: `test_dashboard_package` pins that
 *     the schema's enums ARE `data_type_catalog()` / `view_block_catalog()`.
 * (b) is `the committed schema` below. The .json beside the types is a
 *     GENERATED file, so it can go stale against the generator. Regenerate it
 *     with
 *
 *       PYTHONPATH=src python -c "import json;\
 *         from kiro_crew.artifact_store.dashboard_package import package_json_schema;\
 *         print(json.dumps(package_json_schema(), indent=2))" \
 *         > website/src/types/dashboardPackage.schema.json
 *
 *     and the two catalog enums, the four grammars and the three bounds are
 *     checked against the literal values in the python source here -- so a
 *     catalog edit without a regenerate is named by a red test rather than by a
 *     wrong page.
 * (c) is `the TS unions` below, checked in both directions: a name in the schema
 *     with no TS member AND a TS member the schema does not have are both
 *     failures, because the second is how a union acquires a value nothing
 *     accepts.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

import {
  DASHBOARD_BLOCK_ID_PATTERN,
  DASHBOARD_BLOCK_TYPES,
  DASHBOARD_BOUND_TO_PATTERN,
  DASHBOARD_FIELD_NAME_PATTERN,
  DASHBOARD_FIELD_TYPES,
  DASHBOARD_FOLD_NAMES,
  DASHBOARD_MAX_MODEL_FIELDS,
  DASHBOARD_MAX_THEME_TOKENS,
  DASHBOARD_MAX_VIEW_BLOCKS,
  DASHBOARD_THEME_TOKEN_PATTERN,
} from '../types/dashboardPackage'
import { KIND_BADGE } from '../components/library/LibraryTable'

const SRC = resolve(__dirname, '..')
const SCHEMA_PATH = join(SRC, 'types', 'dashboardPackage.schema.json')
const PY_GATE = join(SRC, '../../src/kiro_crew/artifact_store/dashboard_package.py')
const ARTIFACTS_PAGE = join(SRC, 'pages', 'ArtifactsPage.tsx')

type SchemaEnum = { enum: string[] }
type Schema = {
  $id: string
  required: string[]
  additionalProperties: boolean
  properties: {
    kind: { const: string }
    bound_to: { pattern: string }
    model: {
      properties: {
        types: {
          maxProperties: number
          propertyNames: { pattern: string }
          additionalProperties: {
            required: string[]
            additionalProperties?: unknown
            properties: {
              type: SchemaEnum
              source: { oneOf: { required: string[]; properties: Record<string, SchemaEnum | undefined> }[] }
            }
          }
        }
      }
    }
    view: {
      properties: {
        blocks: {
          maxItems: number
          items: {
            required: string[]
            additionalProperties?: unknown
            properties: { id: { pattern: string }; type: SchemaEnum }
          }
        }
      }
    }
    theme: { properties: { tokens: { maxProperties: number; propertyNames: { pattern: string } } } }
  }
}

const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf-8')) as Schema
const field = schema.properties.model.properties.types.additionalProperties
const block = schema.properties.view.properties.blocks.items

/**
 * Every `name="..."` inside ONE python table literal.
 *
 * Scoped to the slice between two markers rather than scanning the file,
 * because `FieldType` and `BlockType` are spelled the same way and a whole-file
 * scan would merge the two catalogs into one list that matches neither. Both
 * markers are asserted present, so a renamed table is a named failure rather
 * than an empty list quietly comparing equal to another empty list.
 */
function pyNames(source: string, from: string, to: string): string[] {
  const start = source.indexOf(from)
  const end = source.indexOf(to, start + 1)
  expect(start, `marker ${from} is gone from the python gate`).toBeGreaterThan(-1)
  expect(end, `marker ${to} is gone from the python gate`).toBeGreaterThan(start)
  const names = [...source.slice(start, end).matchAll(/name="([a-z_]+)"/g)].map(m => m[1])
  // A control on the SCAN itself: an empty result would make every comparison
  // below pass against an equally empty enum.
  expect(names.length, `no names scraped between ${from} and ${to}`).toBeGreaterThan(0)
  return names.sort()
}

describe('the committed schema is the generator\'s own output', () => {
  const py = readFileSync(PY_GATE, 'utf-8')

  it('its data-type enum is data_type_catalog()\'s names', () => {
    // The names live in `_STUB_DATA_TYPES`, which `data_type_catalog()` returns
    // keyed by `name`. A type added there without regenerating the .json is what
    // this case names.
    expect(field.properties.type.enum.slice().sort()).toEqual(
      pyNames(py, '_STUB_DATA_TYPES', '_BLOCK_TYPES'),
    )
  })

  it('its block-type enum is view_block_catalog()\'s names', () => {
    expect(block.properties.type.enum.slice().sort()).toEqual(
      pyNames(py, '_BLOCK_TYPES', 'def data_type_catalog'),
    )
  })

  it('its grammars are the gate\'s own regexes', () => {
    // Python anchors with `\A`/`\Z` and JSON Schema with `^`/`$`, which is a real
    // difference rather than a formatting one: python's `$` also matches before a
    // trailing newline. So the BODY is compared and the anchors are not.
    const body = (pattern: string) => pattern.replace(/^\^/, '').replace(/\$$/, '')
    const pyBody = (name: string) => {
      const match = py.match(new RegExp(`${name} = re\\.compile\\(\\s*r"\\\\A(.*)\\\\Z"`))
      expect(match, `${name} is gone from the python gate`).not.toBeNull()
      return match![1]
    }
    expect(body(schema.properties.bound_to.pattern)).toBe(pyBody('_BOUND_TO_RE'))
    expect(body(schema.properties.model.properties.types.propertyNames.pattern)).toBe(
      pyBody('_FIELD_NAME_RE'),
    )
    expect(body(block.properties.id.pattern)).toBe(pyBody('_BLOCK_ID_RE'))
    expect(body(schema.properties.theme.properties.tokens.propertyNames.pattern)).toBe(
      pyBody('_THEME_TOKEN_RE'),
    )
  })

  it('its bounds are the gate\'s own constants', () => {
    const constant = (name: string) => {
      const match = py.match(new RegExp(`^${name}[^=]*= ([0-9 *]+)`, 'm'))
      expect(match, `${name} is gone from the python gate`).not.toBeNull()
      // `MAX_THEME_CSS_BYTES = 32 * 1024` and friends, so the value is evaluated
      // rather than parsed as a single integer.
      return match![1]
        .split('*')
        .map(part => Number(part.trim()))
        .reduce((a, b) => a * b, 1)
    }
    expect(schema.properties.model.properties.types.maxProperties).toBe(constant('MAX_MODEL_FIELDS'))
    expect(schema.properties.view.properties.blocks.maxItems).toBe(constant('MAX_VIEW_BLOCKS'))
    expect(schema.properties.theme.properties.tokens.maxProperties).toBe(constant('MAX_THEME_TOKENS'))
  })

  it('is the document the types were written from, not some other schema', () => {
    // The `$id` and the top-level shape, so a file replaced wholesale by a
    // different (even valid) schema cannot sit here passing the enum cases above
    // because it happens to share the same two catalogs.
    expect(schema.$id).toBe('https://kirocrew.dev/schemas/artifact-dashboard-package.json')
    expect(schema.required).toEqual(['kind', 'bound_to', 'model', 'view', 'theme'])
    expect(schema.additionalProperties).toBe(false)
    expect(schema.properties.kind.const).toBe('dashboard')
  })
})

describe('the TS unions are the schema\'s enums', () => {
  it('both directions for a data type', () => {
    // BOTH directions, because the two failures are different bugs. A schema name
    // with no TS member makes the browser call a valid package unreadable; a TS
    // member the schema does not have is a value no package can carry, which some
    // consumer will eventually write a branch for.
    expect([...DASHBOARD_FIELD_TYPES].sort()).toEqual(field.properties.type.enum.slice().sort())
  })

  it('both directions for a block type', () => {
    expect([...DASHBOARD_BLOCK_TYPES].sort()).toEqual(block.properties.type.enum.slice().sort())
  })

  it('both directions for a fold name', () => {
    const fold = field.properties.source.oneOf
      .map(branch => branch.properties.fold?.enum)
      .find(Boolean)
    expect(fold, 'the schema no longer has a fold branch on source').toBeTruthy()
    expect([...DASHBOARD_FOLD_NAMES].sort()).toEqual(fold!.slice().sort())
  })

  it('the grammars and bounds match', () => {
    expect(DASHBOARD_BOUND_TO_PATTERN).toBe(schema.properties.bound_to.pattern)
    expect(DASHBOARD_FIELD_NAME_PATTERN).toBe(
      schema.properties.model.properties.types.propertyNames.pattern,
    )
    expect(DASHBOARD_BLOCK_ID_PATTERN).toBe(block.properties.id.pattern)
    expect(DASHBOARD_THEME_TOKEN_PATTERN).toBe(
      schema.properties.theme.properties.tokens.propertyNames.pattern,
    )
    expect(DASHBOARD_MAX_MODEL_FIELDS).toBe(schema.properties.model.properties.types.maxProperties)
    expect(DASHBOARD_MAX_VIEW_BLOCKS).toBe(schema.properties.view.properties.blocks.maxItems)
    expect(DASHBOARD_MAX_THEME_TOKENS).toBe(schema.properties.theme.properties.tokens.maxProperties)
  })

  it('`source` is one branch or the other, and required', () => {
    // The required `source` is the amendment the package line landed, and it is
    // the reason a page can tell a folded number from a crewmate's own claim. A
    // schema that made it optional, or that merged the two branches, would let a
    // consumer default it -- and the only available default marks every field on
    // every page agent-written.
    expect(field.required).toEqual(['type', 'source'])
    expect(field.properties.source.oneOf.map(b => b.required.slice().sort())).toEqual([
      ['fold', 'path'],
      ['agentic'],
    ])
  })

  it('a field and a block stay OPEN, which is why the TS index signature is right', () => {
    // The per-type keys (`unit`, `choices`, `span`) are the catalog's, and the
    // schema deliberately does not close either object over them. The TS types
    // mirror that with an index signature; if the schema ever closed them, the
    // index signature would become a lie and the keys would have to be enumerated.
    expect(field.additionalProperties).toBeUndefined()
    expect(block.additionalProperties).toBeUndefined()
    expect(block.required).toEqual(['id', 'type', 'fields'])
  })
})

describe('kind="dashboard" in the browser\'s artifact vocabulary', () => {
  it('has a badge, so the library can list it', () => {
    // `KIND_BADGE` is a TOTAL record over `Artifact['kind']`, so this is really a
    // compile-time pin that a runtime case makes visible: adding the kind to the
    // union without a badge is a tsc error, and this says why the badge exists.
    expect(KIND_BADGE.dashboard).toBeTruthy()
  })

  it('is NOT offered in the kind picker', () => {
    // The backend keeps `dashboard` out of `USER_SELECTABLE_KINDS` so nobody can
    // mint one by hand -- the agent's dashboard tool creates it through the
    // validated store path. A kind offered here that the server refuses is a dead
    // menu entry, and this is the pin that keeps the two agreeing.
    //
    // Read off the SOURCE rather than imported, because `KIND_OPTIONS` is module
    // private and exporting it only for a test would make the test the reason the
    // seam exists.
    const page = readFileSync(ARTIFACTS_PAGE, 'utf-8')
    const match = page.match(/const KIND_OPTIONS = \[(.*?)\] as const/s)
    expect(match, 'KIND_OPTIONS is gone from ArtifactsPage').not.toBeNull()
    const options = [...match![1].matchAll(/'([a-z]*)'/g)].map(m => m[1])
    // The control first: the list was found and is populated, so the absence
    // asserted next is about this kind and not about a regex that matched nothing.
    expect(options).toContain('widget')
    expect(options).toContain('webapp')
    expect(options).not.toContain('dashboard')
  })
})
