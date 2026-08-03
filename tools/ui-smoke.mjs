#!/usr/bin/env node

import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import { chromium } from 'playwright'

const ROUTE_TIMEOUT_MS = 20_000

function fail(message) {
  throw new Error(message)
}

function parseArguments(argv) {
  let url = ''
  let screenshot = ''

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === '--url' || argument === '--screenshot') {
      const value = argv[index + 1]
      if (!value || value.startsWith('--')) fail(`Missing value for ${argument}.`)
      if (argument === '--url') url = value
      else screenshot = value
      index += 1
      continue
    }
    if (argument.startsWith('--url=')) {
      url = argument.slice('--url='.length)
      continue
    }
    if (argument.startsWith('--screenshot=')) {
      screenshot = argument.slice('--screenshot='.length)
      continue
    }
    fail(`Unknown argument: ${argument}`)
  }

  if (!url) fail('Usage: node tools/ui-smoke.mjs --url http://127.0.0.1:8686 [--screenshot path.png]')

  let parsedUrl
  try {
    parsedUrl = new URL(url)
  } catch {
    fail(`Invalid --url value: ${url}`)
  }
  if (!['http:', 'https:'].includes(parsedUrl.protocol)) fail('--url must use HTTP or HTTPS.')
  if (parsedUrl.username || parsedUrl.password) fail('--url must not contain credentials.')

  return { baseUrl: parsedUrl, screenshot }
}

function safeUrl(value) {
  try {
    const parsed = new URL(value)
    parsed.username = ''
    parsed.password = ''
    parsed.search = ''
    parsed.hash = ''
    return parsed.toString()
  } catch {
    return '<invalid URL>'
  }
}

function formatIssues(issues) {
  return issues.map((issue, index) => `${index + 1}. ${issue.kind}: ${issue.detail}`).join('\n')
}

async function main() {
  const { baseUrl, screenshot } = parseArguments(process.argv.slice(2))
  const origin = baseUrl.origin
  const browser = await chromium.launch({ headless: true })
  let closing = false

  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    const page = await context.newPage()
    const issues = []

    page.setDefaultTimeout(ROUTE_TIMEOUT_MS)
    page.on('pageerror', (error) => {
      if (!closing) issues.push({ kind: 'pageerror', detail: error?.stack || error?.message || String(error) })
    })
    page.on('console', (message) => {
      if (!closing && message.type() === 'error') {
        issues.push({ kind: 'console.error', detail: message.text() })
      }
    })
    page.on('requestfailed', (request) => {
      if (!closing) {
        const reason = request.failure()?.errorText || 'unknown network error'
        issues.push({ kind: 'requestfailed', detail: `${request.method()} ${safeUrl(request.url())} (${reason})` })
      }
    })

    async function assertClean(label) {
      await page.waitForTimeout(100)
      const globalError = page.locator('.global-banner.error-banner:visible')
      const pageError = page.locator('.state-panel.error-state:visible')
      if (await globalError.count()) issues.push({ kind: 'global error banner', detail: `${label}: ${await globalError.first().innerText()}` })
      if (await pageError.count()) issues.push({ kind: 'page error state', detail: `${label}: ${await pageError.first().innerText()}` })
      if (issues.length) fail(`${label} produced browser errors:\n${formatIssues(issues)}`)
    }

    async function waitForPage(selector, label) {
      await page.locator(selector).first().waitFor({ state: 'visible' })
      await page.locator('[aria-busy="true"]').waitFor({ state: 'detached' })
      await assertClean(label)
    }

    async function navigate(linkSelector, expectedPath, contentSelector, label) {
      const link = page.locator(linkSelector).first()
      await link.waitFor({ state: 'visible' })
      await Promise.all([
        page.waitForURL((url) => url.pathname === expectedPath),
        link.click(),
      ])
      await waitForPage(contentSelector, label)
    }

    await page.goto(new URL('/overview', origin).toString(), { waitUntil: 'domcontentloaded' })
    await waitForPage('.overview-metrics', 'overview')

    const shell = page.locator('.app-shell')
    if (!await shell.isVisible()) fail('The AppShell is not visible at 1440px.')

    const expectedNavigation = ['/overview', '/sites', '/products', '/inbox', '/automations/jobs', '/settings/general']
    const navigation = page.locator('.primary-nav')
    if (!await navigation.isVisible()) fail('The primary navigation is not visible at 1440px.')
    for (const href of expectedNavigation) {
      const count = await navigation.locator(`a[href="${href}"]`).count()
      if (count !== 1) fail(`Expected exactly one primary navigation link for ${href}; found ${count}.`)
    }

    await page.locator('.sidebar-foot').waitFor({ state: 'visible' })
    await page.locator('.sidebar-foot .presence-dot.online').waitFor({ state: 'visible' })

    await navigate('.primary-nav a[href="/sites"]', '/sites', '.sites-layout', 'sites')
    await navigate('.primary-nav a[href="/settings/general"]', '/settings/general', '.settings-layout', 'settings')
    await navigate('.section-tabs a[href="/settings/notifications"]', '/settings/notifications', '.settings-content section.card', 'notification settings')
    await navigate('.primary-nav a[href="/automations/jobs"]', '/automations/jobs', '.automation-tabs + .toolbar', 'automation jobs')

    if (screenshot) {
      await navigate('.primary-nav a[href="/overview"]', '/overview', '.overview-metrics', 'overview screenshot')
      const screenshotPath = path.resolve(screenshot)
      await mkdir(path.dirname(screenshotPath), { recursive: true })
      await page.screenshot({ path: screenshotPath, fullPage: true })
    }

    await page.setViewportSize({ width: 900, height: 800 })
    await navigate('.primary-nav a[href="/sites"]', '/sites', '.sites-layout', 'sites at 900px')

    const narrowLayout = await page.evaluate(() => {
      const surface = document.querySelector('.page-surface')?.getBoundingClientRect()
      const content = document.querySelector('.sites-content')?.getBoundingClientRect()
      return {
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        surfaceWidth: surface?.width || 0,
        surfaceHeight: surface?.height || 0,
        contentWidth: content?.width || 0,
      }
    })
    if (narrowLayout.scrollWidth > narrowLayout.clientWidth + 1) {
      fail(`The 900px layout overflows horizontally (${narrowLayout.scrollWidth}px > ${narrowLayout.clientWidth}px).`)
    }
    if (narrowLayout.surfaceWidth < 300 || narrowLayout.surfaceHeight < 300 || narrowLayout.contentWidth < 200) {
      fail(`The 900px page is not usable: ${JSON.stringify(narrowLayout)}.`)
    }
    await assertClean('900px responsive layout')

    closing = true
    await context.close()
    process.stdout.write('[SiteHub UI smoke] PASS: shell, navigation, Worker state, routes, browser errors, and 900px layout verified.\n')
  } finally {
    closing = true
    await browser.close()
  }
}

main().catch((error) => {
  process.stderr.write(`[SiteHub UI smoke] FAIL: ${error?.stack || error}\n`)
  process.exitCode = 1
})
