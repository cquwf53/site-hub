import path from 'path';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { closeTrackedResource, trackCloseable } from '../resource-registry.js';
import { getProfileDir } from './login-session-manager.js';

export function isXkoolSite(site) {
  try {
    return new URL(site.url).hostname === 'new.xkool.cfd';
  } catch {
    return false;
  }
}

export async function xkoolCheckin(site) {
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
      args: ['--no-first-run', '--no-default-browser-check'],
    }), `xkool:${site.id}`);

    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(30000);
    const profileUrl = new URL('/profile', site.url).href;

    site.markRemoteEffect?.();
    await page.goto(profileUrl, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForTimeout(4000);

    if (/\/(?:sign-in|signin|login)(?:\/|$)/i.test(new URL(page.url()).pathname)) {
      return result('failed', 'XKool 登录状态已失效，请重新建立登录状态', startTime);
    }

    const dailyCard = page.locator('body').getByText('每日签到', { exact: true }).first();
    if (!await dailyCard.isVisible().catch(() => false)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        '已进入 XKool，但未找到“每日签到”区域',
        startTime,
        screenshot,
      );
    }

    const alreadyButton = page.getByRole('button', { name: /已签到/ }).first();
    if (await alreadyButton.isVisible().catch(() => false)) {
      const awardText = await getDailyCardText(dailyCard);
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        awardText ? `今日已签到（${awardText}）` : '今日已签到',
        startTime,
        screenshot,
      );
    }

    const checkinButton = page.getByRole('button', {
      name: /^(?:立即)?签到$/,
    }).first();
    if (!await checkinButton.isVisible().catch(() => false)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result('failed', '未找到可用的 XKool 签到按钮', startTime, screenshot);
    }

    const responsePromise = page.waitForResponse(response => (
      response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/user/checkin'
    ), { timeout: 15000 }).catch(() => null);

    await checkinButton.click();
    const response = await responsePromise;
    const payload = response ? await response.json().catch(() => null) : null;

    if (payload?.success) {
      await page.getByRole('button', { name: /已签到/ })
        .waitFor({ state: 'visible', timeout: 10000 })
        .catch(() => {});
      const award = formatAward(payload.data?.quota_awarded);
      const screenshot = await takeScreenshot(page, site.id, 'success');
      return result(
        'success',
        award ? `XKool 签到成功，获得 ${award}` : 'XKool 签到成功',
        startTime,
        screenshot,
      );
    }

    const message = payload?.message || '';
    if (/已签到|already/i.test(message)) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result('skipped', '今日已签到', startTime, screenshot);
    }

    const pageText = await page.locator('body').innerText().catch(() => '');
    const screenshot = await takeScreenshot(page, site.id, 'failed');
    if (/Turnstile|验证码|驗證碼/i.test(`${message} ${pageText}`)) {
      return result(
        'failed',
        'XKool 本次签到要求验证码，需要手动处理',
        startTime,
        screenshot,
      );
    }

    return result(
      'failed',
      message || '点击签到后未收到明确的成功响应',
      startTime,
      screenshot,
    );
  } catch (error) {
    const profileBusy = /user data directory is already in use|ProcessSingleton|profile.*in use/i
      .test(error.message);
    return result(
      'failed',
      profileBusy
        ? 'XKool 专用 Chrome 正在使用中，请关闭登录窗口后重试'
        : `XKool 签到异常：${error.message}`,
      startTime,
    );
  } finally {
    if (context) await closeTrackedResource(context).catch(() => {});
  }
}

async function getDailyCardText(dailyTitle) {
  try {
    const text = await dailyTitle.locator('xpath=ancestor::*[.//button][1]').innerText();
    const match = text.replace(/\s+/g, ' ').match(/今天\s*([+＋]?\s*\$?[\d.]+)/);
    return match ? `今天 ${match[1].replace(/\s+/g, '')}` : '';
  } catch {
    return '';
  }
}

function formatAward(value) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '';
  return `$${amount.toFixed(2)}`;
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
    const filename = `site${siteId}_xkool_${status}_${timestamp}.png`;
    await page.screenshot({
      path: path.join(SCREENSHOTS_DIR, filename),
      fullPage: false,
    });
    return filename;
  } catch {
    return null;
  }
}
