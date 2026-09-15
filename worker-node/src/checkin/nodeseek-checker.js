import path from 'path';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { humanClick } from './human-mouse.js';
import { getProfileDir } from './login-session-manager.js';
import { launchSystemChromeCdp } from './system-chrome-cdp.js';

const SIGN_STATUS = '.head-info > div';

export function normalizeSignMode(value) {
  return value === 'luck' || value === 'lucky' ? 'lucky' : 'chicken';
}

export function isNodeSeekSite(site) {
  try {
    const hostname = new URL(site.url).hostname;
    return hostname === 'nodeseek.com' || hostname === 'www.nodeseek.com';
  } catch {
    return false;
  }
}

export async function nodeSeekCheckin(site) {
  const startTime = Date.now();
  const config = site.config || {};
  const signMode = normalizeSignMode(config.signMode);
  let browserSession = null;

  try {
    browserSession = await launchSystemChromeCdp(getProfileDir(site.id));
    const { context } = browserSession;
    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(30000);
    site.markRemoteEffect?.();
    await page.goto(site.url, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForTimeout(5000);

    const title = await page.title().catch(() => '');
    const pageText = await page.locator('body').innerText().catch(() => '');
    if (/just a moment|checking your browser|验证您是真人|安全检查|403 forbidden/i
      .test(`${title} ${pageText}`)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        'NodeSeek 触发了浏览器安全检查，需要稍后重试',
        startTime,
        screenshot,
      );
    }

    if (!await page.locator('a.Username').first().isVisible().catch(() => false)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        'NodeSeek 登录状态已失效，请重新建立登录状态',
        startTime,
        screenshot,
      );
    }

    site.markRemoteEffect?.();
    await page.goto(new URL('/board', site.url).href, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForTimeout(5000);

    const status = page.locator(SIGN_STATUS).first();
    await status.waitFor({ state: 'visible', timeout: 30000 });
    await page.waitForFunction(selector => {
      const element = document.querySelector(selector);
      return element && element.textContent.trim() !== 'Loading';
    }, SIGN_STATUS, { timeout: 30000 });

    if (await status.locator('button').count() === 0) {
      const statusText = compactStatus(await status.innerText().catch(() => ''));
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        statusText ? `NodeSeek 今日已签到（${statusText}）` : 'NodeSeek 今日已签到',
        startTime,
        screenshot,
      );
    }

    const buttonName = signMode === 'lucky' ? '试试手气' : '鸡腿 x 5';
    const button = status.getByRole('button', {
      name: buttonName,
      exact: true,
    }).first();
    if (!await button.isVisible().catch(() => false)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        `未找到 NodeSeek“${buttonName}”签到按钮`,
        startTime,
        screenshot,
      );
    }

    await humanClick(page, button);
    await page.waitForFunction(selector => {
      const element = document.querySelector(selector);
      return element && element.querySelectorAll('button').length === 0;
    }, SIGN_STATUS, { timeout: 15000 }).catch(() => {});

    const remainingButtons = await status.locator('button').count();
    const statusText = compactStatus(await status.innerText().catch(() => ''));
    const screenshot = await takeScreenshot(
      page,
      site.id,
      remainingButtons === 0 ? 'success' : 'failed',
    );

    if (remainingButtons === 0) {
      return result(
        'success',
        statusText
          ? `NodeSeek 签到成功（${statusText}）`
          : `NodeSeek 签到成功（${buttonName}）`,
        startTime,
        screenshot,
      );
    }

    return result(
      'failed',
      'NodeSeek 签到按钮已点击，但页面未显示签到结果',
      startTime,
      screenshot,
    );
  } catch (error) {
    const profileBusy = /user data directory is already in use|ProcessSingleton|profile.*in use/i
      .test(error.message);
    return result(
      'failed',
      profileBusy
        ? 'NodeSeek 专用 Chrome 正在使用中，请关闭登录窗口后重试'
        : `NodeSeek 签到异常：${error.message}`,
      startTime,
    );
  } finally {
    if (browserSession) await browserSession.close().catch(() => {});
  }
}

function compactStatus(value) {
  return String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120);
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
    const filename = `site${siteId}_nodeseek_${status}_${timestamp}.png`;
    await page.screenshot({
      path: path.join(SCREENSHOTS_DIR, filename),
      fullPage: false,
    });
    return filename;
  } catch {
    return null;
  }
}
