import path from 'path';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { closeTrackedResource, trackCloseable } from '../resource-registry.js';
import { getProfileDir } from './login-session-manager.js';

const CHECKIN_BUTTON = 'li.header-dropdown-toggle.checkin-icon button.checkin-button';

export function isNodeLocSite(site) {
  try {
    const hostname = new URL(site.url).hostname;
    return hostname === 'nodeloc.com' || hostname === 'www.nodeloc.com';
  } catch {
    return false;
  }
}

export async function nodeLocCheckin(site) {
  const startTime = Date.now();
  const config = site.config || {};
  let context = null;

  try {
    const { chromium } = await import('playwright');
    context = trackCloseable(await chromium.launchPersistentContext(getProfileDir(site.id), {
      channel: 'chrome',
      headless: config.headless !== false,
      viewport: { width: 1400, height: 900 },
      locale: 'zh-CN',
      args: [
        '--disable-blink-features=AutomationControlled',
        '--no-first-run',
        '--no-default-browser-check',
      ],
    }), `nodeloc:${site.id}`);
    await context.addInitScript(() => {
      Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    });

    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(30000);
    await page.goto(new URL('/', site.url).href, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForTimeout(5000);

    const title = await page.title().catch(() => '');
    const pageText = await page.locator('body').innerText().catch(() => '');
    if (/just a moment|checking your browser|验证您是真人|安全检查/i.test(`${title} ${pageText}`)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        'NodeLoc 触发了浏览器安全检查，需要稍后重试',
        startTime,
        screenshot,
      );
    }

    const button = page.locator(CHECKIN_BUTTON).first();
    if (!await button.isVisible().catch(() => false)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        '未找到 NodeLoc 签到按钮，登录状态可能已失效',
        startTime,
        screenshot,
      );
    }

    await button.hover().catch(() => {});
    await page.waitForTimeout(1000);
    let className = await button.getAttribute('class') || '';
    if (className.includes('checked-in') || await button.isDisabled().catch(() => false)) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result('skipped', 'NodeLoc 今日已签到', startTime, screenshot);
    }

    await button.evaluate(element => element.click());
    await page.waitForTimeout(3500);
    await button.hover().catch(() => {});
    await page.waitForTimeout(1000);

    className = await button.getAttribute('class') || '';
    if (className.includes('checked-in') || await button.isDisabled().catch(() => false)) {
      const screenshot = await takeScreenshot(page, site.id, 'success');
      return result('success', 'NodeLoc 签到成功', startTime, screenshot);
    }

    const screenshot = await takeScreenshot(page, site.id, 'failed');
    return result(
      'failed',
      'NodeLoc 签到按钮已点击，但页面未显示已签到状态',
      startTime,
      screenshot,
    );
  } catch (error) {
    const profileBusy = /user data directory is already in use|ProcessSingleton|profile.*in use/i
      .test(error.message);
    return result(
      'failed',
      profileBusy
        ? 'NodeLoc 专用 Chrome 正在使用中，请关闭登录窗口后重试'
        : `NodeLoc 签到异常：${error.message}`,
      startTime,
    );
  } finally {
    if (context) await closeTrackedResource(context).catch(() => {});
  }
}

function result(status, message, startTime, screenshot = null) {
  return {
    status,
    message,
    screenshot,
    duration_ms: Date.now() - startTime,
  };
}

async function takeScreenshot(page, siteId, status) {
  try {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const filename = `site${siteId}_nodeloc_${status}_${timestamp}.png`;
    await page.screenshot({
      path: path.join(SCREENSHOTS_DIR, filename),
      fullPage: false,
    });
    return filename;
  } catch {
    return null;
  }
}
