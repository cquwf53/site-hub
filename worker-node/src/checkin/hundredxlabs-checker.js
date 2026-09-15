import path from 'path';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { closeTrackedResource, trackCloseable } from '../resource-registry.js';
import { getProfileDir } from './login-session-manager.js';

const STATUS_PATH = '/api/v1/check-in/status';
const CHECKIN_PATH = '/api/v1/check-in';

export function isHundredXLabsSite(site) {
  try {
    return new URL(site.url).hostname === 'sub.100xlabs.space';
  } catch {
    return false;
  }
}

export async function hundredXLabsCheckin(site) {
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
    }), `hundredxlabs:${site.id}`);

    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(30000);
    const checkinUrl = new URL('/check-in', site.url).href;

    const statusResponsePromise = waitForApiResponse(page, 'GET', STATUS_PATH, 20000);
    site.markRemoteEffect?.();
    await page.goto(checkinUrl, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    const statusResponse = await statusResponsePromise;
    await page.waitForTimeout(1500);

    if (/\/(?:sign-in|signin|login)(?:\/|$)/i.test(new URL(page.url()).pathname)) {
      return result(
        'failed',
        '100xLabs 登录状态已失效，请重新建立登录状态',
        startTime,
      );
    }

    const statusPayload = statusResponse
      ? await statusResponse.json().catch(() => null)
      : null;
    const status = unwrapPayload(statusPayload);

    if (!status) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        '已进入 100xLabs，但未能读取签到状态',
        startTime,
        screenshot,
      );
    }

    if (status.enabled === false) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result('failed', '100xLabs 当前未开放签到', startTime, screenshot);
    }

    if (status.checked_in_today) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        formatStatusMessage('100xLabs 今日已签到', status),
        startTime,
        screenshot,
      );
    }

    if (status.turnstile_required) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        '100xLabs 本次签到要求人机验证，需要手动处理',
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
        '未找到可用的 100xLabs 签到按钮',
        startTime,
        screenshot,
      );
    }

    const checkinResponsePromise = waitForApiResponse(page, 'POST', CHECKIN_PATH, 15000);
    await checkinButton.click();
    const checkinResponse = await checkinResponsePromise;
    const checkinPayload = checkinResponse
      ? await checkinResponse.json().catch(() => null)
      : null;
    const checkinResult = unwrapPayload(checkinPayload);

    if (checkinResult?.already_checked_in || checkinResult?.checked_in_today) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        formatStatusMessage('100xLabs 今日已签到', checkinResult),
        startTime,
        screenshot,
      );
    }

    if (isSuccessfulResponse(checkinResponse, checkinPayload, checkinResult)) {
      await page.getByRole('button', { name: /今日已签到/ })
        .waitFor({ state: 'visible', timeout: 10000 })
        .catch(() => {});
      const screenshot = await takeScreenshot(page, site.id, 'success');
      return result(
        'success',
        formatStatusMessage('100xLabs 签到成功', checkinResult),
        startTime,
        screenshot,
      );
    }

    const message = checkinPayload?.message || '';
    if (/已签到|already/i.test(message)) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result('skipped', '100xLabs 今日已签到', startTime, screenshot);
    }

    const screenshot = await takeScreenshot(page, site.id, 'failed');
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
        ? '100xLabs 专用 Chrome 正在使用中，请关闭登录窗口后重试'
        : `100xLabs 签到异常：${error.message}`,
      startTime,
    );
  } finally {
    if (context) await closeTrackedResource(context).catch(() => {});
  }
}

function waitForApiResponse(page, method, pathname, timeout) {
  return page.waitForResponse(response => {
    try {
      return response.request().method() === method
        && new URL(response.url()).pathname === pathname;
    } catch {
      return false;
    }
  }, { timeout }).catch(() => null);
}

function unwrapPayload(payload) {
  if (!payload || typeof payload !== 'object') return null;
  if (payload.data && typeof payload.data === 'object') return payload.data;
  return payload;
}

function isSuccessfulResponse(response, payload, data) {
  if (!response?.ok()) return false;
  if (payload && Object.hasOwn(payload, 'code') && payload.code !== 0) return false;
  return Boolean(data);
}

function formatStatusMessage(prefix, status) {
  const details = [];
  const reward = Number(status?.reward_amount ?? status?.today_reward);
  const streak = Number(status?.current_streak ?? status?.streak);

  if (Number.isFinite(reward)) details.push(`奖励 $${reward.toFixed(2)}`);
  if (Number.isFinite(streak)) details.push(`连续 ${streak} 天`);

  return details.length ? `${prefix}（${details.join('，')}）` : prefix;
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
    const filename = `site${siteId}_100xlabs_${status}_${timestamp}.png`;
    await page.screenshot({
      path: path.join(SCREENSHOTS_DIR, filename),
      fullPage: false,
    });
    return filename;
  } catch {
    return null;
  }
}
