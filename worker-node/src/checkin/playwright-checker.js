import path from 'path';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { closeTrackedResource, trackCloseable } from '../resource-registry.js';
import { getProfileDir } from './login-session-manager.js';

const DEFAULT_BUTTON_SELECTOR = '.checkin-button, [class*="checkin"], [class*="sign"]';
const CONTEXT_OPTIONS = {
  args: [
    '--disable-blink-features=AutomationControlled',
    '--no-first-run',
    '--no-default-browser-check',
  ],
  userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  viewport: { width: 1400, height: 900 },
  locale: 'zh-CN',
};

async function loadChromium(injectedChromium) {
  if (injectedChromium) return injectedChromium;
  const playwright = await import('playwright');
  return playwright.chromium;
}

async function openAccountContext(site, chromium) {
  const config = site.config || {};
  return trackCloseable(await chromium.launchPersistentContext(getProfileDir(site.id), {
    ...CONTEXT_OPTIONS,
    headless: config.headless !== false,
  }), `playwright-generic:${site.id}`);
}

async function preparePage(context, site, { preActions = true } = {}) {
  const config = site.config || {};
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });

  if (config.cookie) {
    const cookies = parseCookies(config.cookie, site.url);
    if (cookies.length > 0) await context.addCookies(cookies);
  }

  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(30000);
  const targetUrl = preActions ? site.url : (config.reconcileUrl || site.url);
  await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });

  if (config.waitSelector) {
    await page.waitForSelector(config.waitSelector, { timeout: 15000 }).catch(() => {});
  } else {
    await page.waitForTimeout(2000);
  }

  if (preActions && Array.isArray(config.preActions)) {
    for (const action of config.preActions) {
      try {
        if (action.type === 'click') {
          await page.click(action.selector, { timeout: 5000 });
        } else if (action.type === 'wait') {
          await page.waitForTimeout(action.duration || 1000);
        } else if (action.type === 'goto') {
          await page.goto(action.url, { waitUntil: 'domcontentloaded' });
        }
      } catch {
        // Pre-actions are convenience steps and must not fail the whole run.
      }
      await page.waitForTimeout(300);
    }
  }
  return page;
}

function buttonSelector(config) {
  return config.checkinButtonSelector || DEFAULT_BUTTON_SELECTOR;
}

function alreadyCheckedText(config) {
  return config.alreadyCheckedText || '已签';
}

function looksLikeCheckinAction(text) {
  return /(签到|簽到|打卡|领取|領取|check\s*-?\s*in|claim)/i.test(String(text || ''));
}

/**
 * Read-only reconciliation for retry attempts. It never performs pre-actions
 * or clicks. A retry is allowed only when the page gives a conclusive signal.
 */
export async function playwrightCheckAlreadyDone(site, { chromium: injectedChromium } = {}) {
  let context = null;
  try {
    const chromium = await loadChromium(injectedChromium);
    context = await openAccountContext(site, chromium);
    const page = await preparePage(context, site, { preActions: false });
    const config = site.config || {};

    if (config.alreadyCheckedSelector) {
      const alreadyElement = await page.$(config.alreadyCheckedSelector);
      if (alreadyElement) {
        return { done: true, conclusive: true, message: '已确认今日签到已完成' };
      }
    }

    const selector = buttonSelector(config);
    const button = await page.waitForSelector(selector, { timeout: 10000 }).catch(() => null);
    if (!button) {
      return { done: false, conclusive: false, message: '无法确认今日签到状态' };
    }

    const text = String(await button.textContent().catch(() => '') || '').trim();
    if (text.includes(alreadyCheckedText(config))) {
      return { done: true, conclusive: true, message: '已确认今日签到已完成' };
    }

    // An explicit selector is an adapter-level assertion that this is the
    // action control. With the broad fallback selector, require check-in text.
    if (config.checkinButtonSelector || looksLikeCheckinAction(text)) {
      return { done: false, conclusive: true, message: '已确认今日尚未签到' };
    }

    return { done: false, conclusive: false, message: '签到按钮状态不明确' };
  } catch (error) {
    return {
      done: false,
      conclusive: false,
      message: `签到状态检查失败: ${error?.message || error}`,
    };
  } finally {
    if (context) await closeTrackedResource(context).catch(() => {});
  }
}

/**
 * Generic Playwright check-in using the persistent profile owned by the
 * account UUID. No ephemeral browser context is created.
 */
export async function playwrightCheckin(site, { chromium: injectedChromium } = {}) {
  const config = site.config || {};
  const startTime = Date.now();
  let chromium;
  try {
    chromium = await loadChromium(injectedChromium);
  } catch (error) {
    return {
      status: 'failed',
      message: `Playwright 未安装或加载失败: ${error.message}。请运行 npx playwright install chromium`,
      duration_ms: Date.now() - startTime,
    };
  }

  let context = null;
  try {
    context = await openAccountContext(site, chromium);
    const page = await preparePage(context, site);

    if (config.alreadyCheckedSelector) {
      const alreadyElement = await page.$(config.alreadyCheckedSelector);
      if (alreadyElement) {
        const screenshotPath = await takeScreenshot(page, site.id, 'skipped');
        return {
          status: 'skipped',
          message: '今日已签到（检测到已签到标识）',
          screenshot: screenshotPath,
          duration_ms: Date.now() - startTime,
        };
      }
    }

    const selector = buttonSelector(config);
    const button = await page.waitForSelector(selector, { timeout: 15000 });
    if (!button) {
      const screenshotPath = await takeScreenshot(page, site.id, 'failed');
      return {
        status: 'failed',
        message: '未找到签到按钮',
        screenshot: screenshotPath,
        duration_ms: Date.now() - startTime,
      };
    }

    const text = await button.textContent().catch(() => '');
    const alreadyText = alreadyCheckedText(config);
    if (text && text.includes(alreadyText)) {
      const screenshotPath = await takeScreenshot(page, site.id, 'skipped');
      return {
        status: 'skipped',
        message: `今日已签到（按钮文字: "${text.trim()}"）`,
        screenshot: screenshotPath,
        duration_ms: Date.now() - startTime,
      };
    }

    await button.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    await button.click();
    await page.waitForTimeout(2500);
    const screenshotPath = await takeScreenshot(page, site.id, 'success');
    const afterButtonText = await page.$(selector)
      .then(element => element?.textContent())
      .catch(() => '');

    if (afterButtonText && afterButtonText.includes(alreadyText)) {
      return {
        status: 'success',
        message: '浏览器签到成功',
        screenshot: screenshotPath,
        duration_ms: Date.now() - startTime,
      };
    }

    return {
      status: 'failed',
      message: '已点击签到按钮，但未检测到可验证的成功状态',
      screenshot: screenshotPath,
      duration_ms: Date.now() - startTime,
    };
  } catch (error) {
    return {
      status: 'failed',
      message: `Playwright 签到异常: ${error.message}`,
      duration_ms: Date.now() - startTime,
    };
  } finally {
    if (context) await closeTrackedResource(context).catch(() => {});
  }
}

async function takeScreenshot(page, siteId, status) {
  try {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const filename = `site${siteId}_${status}_${timestamp}.png`;
    const filepath = path.join(SCREENSHOTS_DIR, filename);
    await page.screenshot({ path: filepath, fullPage: false });
    return filename;
  } catch {
    return null;
  }
}

function parseCookies(cookieInput, siteUrl) {
  if (Array.isArray(cookieInput)) return cookieInput;
  if (typeof cookieInput !== 'string') return [];
  const url = new URL(siteUrl);
  return cookieInput.split(';').map(pair => {
    const [name, ...valueParts] = pair.trim().split('=');
    if (!name) return null;
    return {
      name: name.trim(),
      value: valueParts.join('=').trim(),
      domain: url.hostname,
      path: '/',
    };
  }).filter(Boolean);
}
