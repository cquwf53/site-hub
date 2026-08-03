/**
 * jianzhile.vip 签到适配器
 *
 * 站点基于 New API 改版，个人中心签到可能弹出「安全验证」图形验证码：
 *   1. GET  /api/user/checkin?month=YYYY-MM  读取状态（含 captcha_enabled）
 *   2. POST /api/user/checkin/captcha        获取 captcha_id + captcha_image
 *   3. POST /api/user/checkin               { captcha_id, captcha_answer }
 *
 * 所有用户 API 请求需带 New-Api-User（= localStorage.user.id）。
 * 验证码仅做自动 OCR；识别失败则 skipped，继续后续站点。
 *
 * 验证码识别：优先 ddddocr（针对彩色扭曲验证码训练，单次命中率约 6 成），
 * 未装 Python/ddddocr 时自动降级 tesseract。每次尝试都向服务端换取新图；
 * 达到最大次数仍未通过则记为 skipped，由调度器继续下一个站点，不做人工干预。
 *
 * site.config 可选：
 *   headless              默认 true
 *   captchaMaxAttempts    OCR 最大尝试次数，默认 2（约 85% 成功率，失败即跳过）
 */

import path from 'path';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { closeTrackedResource, trackCloseable } from '../resource-registry.js';
import {
  preprocessCaptchaInPage,
  recognizeCaptcha,
} from './image-captcha-ocr.js';
import { getProfileDir } from './login-session-manager.js';

const CHECKIN_PATH = '/api/user/checkin';
const CAPTCHA_PATH = '/api/user/checkin/captcha';
const SITE_LABEL = 'jianzhile.vip';

export function isJianzhileSite(site) {
  try {
    const hostname = new URL(site.url).hostname;
    return hostname === 'jianzhile.vip' || hostname === 'www.jianzhile.vip';
  } catch {
    return false;
  }
}

export async function jianzhileCheckin(site) {
  const startTime = Date.now();
  const config = site.config || {};
  let context = null;

  try {
    context = await launchSiteContext(site, config.headless !== false);
    const page = await openPersonalPage(context, site);

    if (!await ensureLoggedIn(page)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        `${SITE_LABEL} 登录状态已失效，请重新建立登录状态`,
        startTime,
        screenshot,
      );
    }

    const userId = await readNewApiUserId(page);
    if (!userId) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        `${SITE_LABEL} 未读取到用户 ID（New-Api-User），请重新建立登录状态`,
        startTime,
        screenshot,
      );
    }

    const statusPayload = await fetchCheckinStatus(page, userId);
    if (!statusPayload?.success || !statusPayload.data) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        statusPayload?.message || `已进入 ${SITE_LABEL}，但未能读取签到状态`,
        startTime,
        screenshot,
      );
    }

    const checkinState = statusPayload.data;
    if (checkinState.enabled === false) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        `${SITE_LABEL} 当前未开放签到`,
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

    if (!checkinState.captcha_enabled) {
      return await submitWithoutCaptcha(page, site, publicConfig, startTime, userId);
    }

    const maxAttempts = Math.max(1, Number(config.captchaMaxAttempts) || 2);
    let lastError = '';

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      const captchaPayload = await requestCaptcha(page, userId);
      if (!captchaPayload?.success || !captchaPayload.data?.captcha_id
        || !captchaPayload.data?.captcha_image) {
        lastError = captchaPayload?.message || '获取验证码失败';
        continue;
      }

      const { captcha_id: captchaId, captcha_image: captchaImage } = captchaPayload.data;
      let answer = '';
      try {
        const preprocessed = await preprocessCaptchaInPage(page, captchaImage)
          || captchaImage;
        const recognized = await recognizeCaptcha(preprocessed);
        answer = recognized.text;
      } catch (error) {
        lastError = `OCR 失败：${error.message}`;
        continue;
      }

      if (!answer || answer.length < 3) {
        lastError = `OCR 识别结果不可用（第 ${attempt} 次：${answer || '空'}）`;
        continue;
      }

      const checkinPayload = await submitCheckin(page, userId, {
        captcha_id: captchaId,
        captcha_answer: answer,
      });

      if (checkinPayload?.success) {
        const screenshot = await takeScreenshot(page, site.id, 'success');
        return result(
          'success',
          formatSuccessMessage(checkinPayload.data, publicConfig, attempt),
          startTime,
          screenshot,
        );
      }

      const message = checkinPayload?.message || '';
      if (/已签到|already/i.test(message)) {
        const screenshot = await takeScreenshot(page, site.id, 'skipped');
        return result(
          'skipped',
          `${SITE_LABEL} 今日已签到`,
          startTime,
          screenshot,
        );
      }

      lastError = message || `验证码可能错误（识别为 ${answer}）`;
      console.log(
        `[签到] ${SITE_LABEL} 验证码第 ${attempt}/${maxAttempts} 次未通过：${lastError}`,
      );
    }

    const screenshot = await takeScreenshot(page, site.id, 'skipped');
    return result(
      'skipped',
      lastError
        ? `${SITE_LABEL} 验证码识别失败，已跳过：${lastError}`
        : `${SITE_LABEL} 验证码识别失败，已跳过`,
      startTime,
      screenshot,
    );
  } catch (error) {
    const profileBusy = /user data directory is already in use|ProcessSingleton|profile.*in use/i
      .test(error.message);
    return result(
      'failed',
      profileBusy
        ? `${SITE_LABEL} 专用 Chrome 正在使用中，请关闭登录窗口后重试`
        : `${SITE_LABEL} 签到异常：${error.message}`,
      startTime,
    );
  } finally {
    if (context) await closeTrackedResource(context).catch(() => {});
  }
}

async function launchSiteContext(site, headless) {
  const { chromium } = await import('playwright');
  return trackCloseable(await chromium.launchPersistentContext(getProfileDir(site.id), {
    channel: 'chrome',
    headless,
    viewport: { width: 1400, height: 1000 },
    locale: 'zh-CN',
    args: ['--no-first-run', '--no-default-browser-check'],
  }), `jianzhile:${site.id}`);
}

async function openPersonalPage(context, site) {
  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(30000);
  const personalUrl = new URL('/console/personal', site.url).href;
  const statusResponsePromise = waitForCheckinGet(page, 20000);
  await page.goto(personalUrl, {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  });
  await statusResponsePromise;
  await page.waitForTimeout(1500);
  return page;
}

async function ensureLoggedIn(page) {
  if (/\/(?:sign-in|signin|login)(?:\/|$)/i.test(new URL(page.url()).pathname)) {
    return false;
  }
  return await isLoggedIn(page);
}

async function submitWithoutCaptcha(page, site, publicConfig, startTime, userId) {
  const checkinPayload = await submitCheckin(page, userId, {});
  if (checkinPayload?.success) {
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
    return result('skipped', `${SITE_LABEL} 今日已签到`, startTime, screenshot);
  }

  if (/captcha|验证码|验证/i.test(message)) {
    const screenshot = await takeScreenshot(page, site.id, 'skipped');
    return result(
      'skipped',
      `${SITE_LABEL} 服务端要求验证码，已跳过：${message}`,
      startTime,
      screenshot,
    );
  }

  const screenshot = await takeScreenshot(page, site.id, 'failed');
  return result(
    'failed',
    message || '签到请求未收到明确的成功响应',
    startTime,
    screenshot,
  );
}

async function isLoggedIn(page) {
  if (/^\/(?:sign-in|signin|login)(?:\/|$)/i.test(new URL(page.url()).pathname)) {
    return false;
  }

  const userId = await readNewApiUserId(page);
  if (userId) return true;

  return await page.locator('a[href="/console/personal"]').count() > 0;
}

function waitForCheckinGet(page, timeout) {
  return page.waitForResponse(response => {
    try {
      if (response.request().method() !== 'GET') return false;
      return new URL(response.url()).pathname === CHECKIN_PATH;
    } catch {
      return false;
    }
  }, { timeout }).catch(() => null);
}

async function readNewApiUserId(page) {
  return await page.evaluate(() => {
    try {
      const user = JSON.parse(localStorage.getItem('user') || 'null');
      if (user?.id == null) return null;
      const userId = String(user.id);
      return Number(userId) > 0 ? userId : null;
    } catch {
      return null;
    }
  }).catch(() => null);
}

async function fetchCheckinStatus(page, userId) {
  const month = shanghaiMonth();
  return await page.evaluate(async ({ checkinPath, monthValue, newApiUser }) => {
    try {
      const response = await fetch(`${checkinPath}?month=${monthValue}`, {
        credentials: 'include',
        headers: { 'New-Api-User': newApiUser },
      });
      return await response.json();
    } catch (error) {
      return { success: false, message: error.message };
    }
  }, { checkinPath: CHECKIN_PATH, monthValue: month, newApiUser: userId });
}

async function requestCaptcha(page, userId) {
  return await page.evaluate(async ({ captchaPath, newApiUser }) => {
    try {
      const response = await fetch(captchaPath, {
        method: 'POST',
        credentials: 'include',
        headers: { 'New-Api-User': newApiUser },
      });
      return await response.json();
    } catch (error) {
      return { success: false, message: error.message };
    }
  }, { captchaPath: CAPTCHA_PATH, newApiUser: userId });
}

async function submitCheckin(page, userId, body) {
  return await page.evaluate(async ({ checkinPath, newApiUser, payload }) => {
    try {
      const response = await fetch(checkinPath, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'New-Api-User': newApiUser,
        },
        body: JSON.stringify(payload || {}),
      });
      return await response.json();
    } catch (error) {
      return { success: false, message: error.message };
    }
  }, { checkinPath: CHECKIN_PATH, newApiUser: userId, payload: body });
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

  const prefix = `${SITE_LABEL} 今日已签到`;
  return details.length ? `${prefix}（${details.join('，')}）` : prefix;
}

function formatSuccessMessage(data, publicConfig, attempt = null) {
  const reward = formatQuota(data?.quota_awarded, publicConfig);
  const base = reward
    ? `${SITE_LABEL} 签到成功，获得 ${reward}`
    : `${SITE_LABEL} 签到成功`;
  if (attempt && attempt > 1) return `${base}（验证码第 ${attempt} 次识别）`;
  return base;
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

function shanghaiMonth(now = new Date()) {
  return shanghaiDate(now).slice(0, 7);
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
    const filename = `site${siteId}_jianzhile_${status}_${timestamp}.png`;
    await page.screenshot({
      path: path.join(SCREENSHOTS_DIR, filename),
      fullPage: false,
    });
    return filename;
  } catch {
    return null;
  }
}
