/**
 * The Add secret hint for a managed name (`settings.secrets.managed_name_hint`)
 * tells the user which group saving updates, so in every shipped locale it names
 * that group by the heading the Secrets panel renders for it
 * (`settings.secrets.managed_title`), and the product name appears only inside
 * that heading, never tacked on as ` — {{productName}}`. The generated
 * pseudolocale pads every value, so it is skipped.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))

describe('secrets managed-name hint names the managed group by its heading', () => {
  const shipped = Object.keys(CATALOGS).filter(code => !GENERATED.has(code))

  it.each(shipped)('%s: managed_name_hint names managed_title, the only product name in it', code => {
    const settings = CATALOGS[code].translation.settings as Record<string, Record<string, string>>
    const secrets = settings.secrets
    expect(secrets.managed_name_hint).toContain(secrets.managed_title)
    expect(secrets.managed_name_hint.replace(secrets.managed_title, '')).not.toContain('{{productName}}')
  })
})
