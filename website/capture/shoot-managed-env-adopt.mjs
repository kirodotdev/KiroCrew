// Screenshots for adding a Kiro Managed Environment another client made: the setup
// tab's "environments from elsewhere" list, and an added crew row (Stop, no Delete).
//   node capture/shoot-managed-env-adopt.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/managed-env-adopt-shots'
const PORT = Number(process.argv[3] || 5193)
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] })

const status = (id) => ({ instance_id: id, state: 'disconnected' })
const row = (id, name, env) => ({
  id, name, ssh_host: `crew-sbx-${env}`, remote_port: 18790, local_port: 0, ttl: '8h', remote_bin: 'kirocrew',
  connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '', ssm_run_as: '',
  provisioner_id: 'kiro_managed_env', was_connected: false, status: status(id),
})
const INSTANCES = [row('r1', 'my-crew-01', 'env-own'), row('r2', 'web-crew', 'env-added')]
const LIST = {
  sandboxes: [
    { sandbox_id: 'env-own', status: 'RUNNING', own: true, display_name: 'my-crew-01-kc-0a1b2c', ssh_host: 'crew-sbx-env-own' },
    { sandbox_id: 'env-added', status: 'RUNNING', own: false, adopted: true, display_name: 'web-crew', ssh_host: 'crew-sbx-env-added' },
    { sandbox_id: 'env-web', status: 'RUNNING', own: false, display_name: 'my-crew-02-kc-cde482' },
    { sandbox_id: 'env-ide', status: 'STOPPED', own: false, display_name: 'ide-sandbox' },
  ],
}
const PROVISIONERS = {
  provisioners: [{ id: 'kiro_managed_env', kind: 'kiro_managed_env', label: 'Kiro Managed Environments', posix_only: false, steps: [] }],
}
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

for (const theme of ['light', 'dark']) {
  for (const scene of ['setup', 'row']) {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 }, deviceScaleFactor: 2, reducedMotion: 'reduce' })
    await page.route((u) => new URL(u).pathname.startsWith('/api/'), (route) => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/instances') return json(route, { active: true, instances: INSTANCES, warm_set_cap: 3, sso: { state: 'ok', seconds_remaining: null, expires_at: null, reason: '' } })
      if (path === '/api/cloud/provisioners') return json(route, PROVISIONERS)
      if (path === '/api/cloud/launch') return json(route, { jobs: [] })
      if (path === '/api/managed-environments/list') return json(route, LIST)
      if (path === '/api/managed-environments/ssh-config') return json(route, { present: true, path: '/home/me/.ssh/config' })
      return json(route, {})
    })
    await page.goto(`http://127.0.0.1:${PORT}/capture/managed-env.html?theme=${theme}`)
    if (scene === 'setup') {
      await page.getByRole('button', { name: 'Add Crew' }).first().click({ timeout: 45000 })
      await page.getByText('Set up a new Crew').first().click({ timeout: 45000 })
      await page.getByTestId('managed-env-others').waitFor({ timeout: 45000 })
      await page.getByRole('textbox').first().fill('my-crew-02')
    } else {
      await page.getByText('my-crew-01').first().waitFor({ timeout: 45000 })
      // Expand both rows: the added one (web-crew) and the lane's own one, side by side.
      await page.locator('button[aria-expanded]').nth(0).click({ timeout: 45000 })
      await page.locator('button[aria-expanded]').nth(1).click({ timeout: 45000 })
      await page.getByText('Made outside this Crew').first().waitFor({ timeout: 45000 })
    }
    await page.mouse.move(1, 1) // no hover tooltip in the shot
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(OUT, `${scene}-${theme}.png`), fullPage: true })
    console.log('shot', scene, theme)
    await page.close()
  }
}
await browser.close()
await server.close()
