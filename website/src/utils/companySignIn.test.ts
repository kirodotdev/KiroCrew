import { beforeEach, describe, expect, it } from 'vitest'
import {
  COMPANY_SIGN_IN_REGION_KEY,
  COMPANY_SIGN_IN_URL_KEY,
  readCompanySignIn,
  rememberCompanySignIn,
  sameCompanyPortal,
} from './companySignIn'

describe('company sign-in persistence', () => {
  beforeEach(() => {
    localStorage.removeItem(COMPANY_SIGN_IN_URL_KEY)
    localStorage.removeItem(COMPANY_SIGN_IN_REGION_KEY)
  })

  it('remembers a scheme-less URL with implicit HTTPS', () => {
    rememberCompanySignIn(' example.awsapps.com/start ', ' eu-west-1 ')
    expect(localStorage.getItem(COMPANY_SIGN_IN_URL_KEY)).toBe('https://example.awsapps.com/start')
    expect(readCompanySignIn()).toEqual({
      startUrl: 'https://example.awsapps.com/start',
      region: 'eu-west-1',
    })
  })

  it.each([
    'https://example.awsapps.com/start',
    'http://example.awsapps.com/start',
    'HTTPS://example.awsapps.com/start',
    'custom+v1.2://example.awsapps.com/start',
  ])('leaves an explicit scheme unchanged: %s', startUrl => {
    rememberCompanySignIn(startUrl, '')
    expect(localStorage.getItem(COMPANY_SIGN_IN_URL_KEY)).toBe(startUrl)
    expect(readCompanySignIn().startUrl).toBe(startUrl)
  })

  it('reads legacy scheme-less URLs with implicit HTTPS', () => {
    localStorage.setItem(COMPANY_SIGN_IN_URL_KEY, 'example.awsapps.com/start')
    expect(readCompanySignIn().startUrl).toBe('https://example.awsapps.com/start')
  })

  it.each(['', '   '])('does not save an empty URL: %s', startUrl => {
    rememberCompanySignIn(startUrl, 'eu-west-1')
    expect(localStorage.getItem(COMPANY_SIGN_IN_URL_KEY)).toBeNull()
    expect(localStorage.getItem(COMPANY_SIGN_IN_REGION_KEY)).toBeNull()
    expect(readCompanySignIn()).toEqual({ startUrl: '', region: '' })
  })
})

const PORTAL_VARIANTS = [
  'example.awsapps.com',
  'https://example.awsapps.com',
  'https://example.awsapps.com/',
  'https://example.awsapps.com/start',
  ' HTTPS://EXAMPLE.awsapps.com/START/ ',
  'example.awsapps.com/start///',
]

describe('sameCompanyPortal', () => {
  it.each(PORTAL_VARIANTS)('matches equivalent portal forms for %s in both directions', portal => {
    for (const other of PORTAL_VARIANTS) {
      expect(sameCompanyPortal(portal, other)).toBe(true)
      expect(sameCompanyPortal(other, portal)).toBe(true)
    }
  })

  it.each([
    ['https://example.awsapps.com', 'https://other.awsapps.com/start'],
    ['https://example.awsapps.com/start', 'https://example.awsapps.com/custom'],
    ['', 'https://example.awsapps.com/start'],
    ['https://example.awsapps.com/start', ''],
    ['', ''],
    ['   ', '   '],
  ])('rejects distinct or empty portals: %s and %s', (a, b) => {
    expect(sameCompanyPortal(a, b)).toBe(false)
  })
})
