import path from 'path';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { closeTrackedResource, trackCloseable } from '../resource-registry.js';
import { getProfileDir } from './login-session-manager.js';

const CHECKIN_PATH = '/api/user/checkin';

export function isApi123NhhSite(site) {
  try {
    return new URL(site.url).hostname === 'api.123nhh.com';
  } catch {
    return false;
  }
}

export async function api123NhhCheckin(site) {
  const startTime = Date.now();
  const config = site.config || {};
  let context = null;

  try {
    const { chromium } = await import('playwright');
    context = trackCloseable(await chromium.launchPersistentContext(getProfileDir(site.id), {
      channel: 'chrome',
      headless: config.headless !== false,
      viewport: { width: 1400, height: 1000 },
      locale: 'zh-CN',
      args: ['--no-first-run', '--no-default-browser-check'],
    }), `api123nhh:${site.id}`);

    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(30000);
    const personalUrl = new URL('/console/personal', site.url).href;

    const statusResponsePromise = waitForCheckinResponse(page, 'GET', 20000);
    site.markRemoteEffect?.();
    await page.goto(personalUrl, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    const statusResponse = await statusResponsePromise;
    await page.waitForTimeout(1500);

    if (/\/(?:sign-in|signin|login)(?:\/|$)/i.test(new URL(page.url()).pathname)) {
      return result(
        'failed',
        'api.123nhh.com 登录状态已失效，请重新建立登录状态',
        startTime,
      );
    }

    const statusPayload = statusResponse
      ? await statusResponse.json().catch(() => null)
      : null;
    if (!statusPayload?.success || !statusPayload.data) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        statusPayload?.message || '已进入 api.123nhh.com，但未能读取签到状态',
        startTime,
        screenshot,
      );
    }

    const checkinState = statusPayload.data;
    if (checkinState.enabled === false) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        'api.123nhh.com 当前未开放签到',
        startTime,
        screenshot,
      );
    }

    const publicConfig = await readPublicConfig(page);
    const stats = checkinState.stats || {};
    if (stats.checked_in_today) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        formatExistingStatus(stats, publicConfig),
        startTime,
        screenshot,
      );
    }

    const checkinButton = page.getByRole('button', {
      name: /^(?:立即)?签到$/,
    }).first();
    if (!await checkinButton.isVisible().catch(() => false)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        '未找到可用的 api.123nhh.com 签到按钮',
        startTime,
        screenshot,
      );
    }

    const checkinResponsePromise = waitForCheckinResponse(page, 'POST', 15000);
    await checkinButton.click();
    const checkinResponse = await checkinResponsePromise;
    const checkinPayload = checkinResponse
      ? await checkinResponse.json().catch(() => null)
      : null;

    if (checkinResponse?.ok() && checkinPayload?.success) {
      await page.getByRole('button', { name: /今日已签到/ })
        .waitFor({ state: 'visible', timeout: 10000 })
        .catch(() => {});
      const screenshot = await takeScreenshot(page, site.id, 'success');
      return result(
        'success',
        formatSuccessMessage(checkinPayload.data, publicConfig),
        startTime,
        screenshot,
      );
    }

    const message = checkinPayload?.message || '';
    if (/已签到|already/i.test(message)) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        'api.123nhh.com 今日已签到',
        startTime,
        screenshot,
      );
    }

    const screenshot = await takeScreenshot(page, site.id, 'failed');
    if (/turnstile|验证|captcha/i.test(message)) {
      return result(
        'failed',
        'api.123nhh.com 本次签到要求人机验证，需要手动处理',
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
        ? 'api.123nhh.com 专用 Chrome 正在使用中，请关闭登录窗口后重试'
        : `api.123nhh.com 签到异常：${error.message}`,
      startTime,
    );
  } finally {
    if (context) await closeTrackedResource(context).catch(() => {});
  }
}

function waitForCheckinResponse(page, method, timeout) {
  return page.waitForResponse(response => {
    try {
      return response.request().method() === method
        && new URL(response.url()).pathname === CHECKIN_PATH;
    } catch {
      return false;
    }
  }, { timeout }).catch(() => null);
}

async function readPublicConfig(page) {
  return await page.evaluate(async () => {
    try {
      const response = await fetch('/api/status', { credentials: 'include' });
      if (!response.ok) return {};
      const payload = await response.json();
      return payload?.data || {};
    } catch {
      return {};
    }
  });
}

function formatExistingStatus(stats, publicConfig) {
  const details = [];
  const today = shanghaiDate();
  const todayRecord = Array.isArray(stats.records)
    ? stats.records.find(record => record.checkin_date === today)
    : null;
  const reward = formatQuota(todayRecord?.quota_awarded, publicConfig);
  const total = Number(stats.total_checkins);

  if (reward) details.push(`奖励 ${reward}`);
  if (Number.isFinite(total)) details.push(`累计 ${total} 天`);

  const prefix = 'api.123nhh.com 今日已签到';
  return details.length ? `${prefix}（${details.join('，')}）` : prefix;
}

function formatSuccessMessage(data, publicConfig) {
  const reward = formatQuota(data?.quota_awarded, publicConfig);
  return reward
    ? `api.123nhh.com 签到成功，获得 ${reward}`
    : 'api.123nhh.com 签到成功';
}

function formatQuota(value, publicConfig) {
  const quota = Number(value);
  const unit = Number(publicConfig?.quota_per_unit);
  if (!Number.isFinite(quota)) return '';
  if (!Number.isFinite(unit) || unit <= 0) {
    return `额度 ${Math.round(quota).toLocaleString('en-US')}`;
  }

  const amount = quota / unit;
  return `$${amount.toFixed(2)}`;
}

function shanghaiDate(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
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
    const filename = `site${siteId}_api123nhh_${status}_${timestamp}.png`;
    await page.screenshot({
      path: path.join(SCREENSHOTS_DIR, filename),
      fullPage: false,
    });
    return filename;
  } catch {
    return null;
  }
}
