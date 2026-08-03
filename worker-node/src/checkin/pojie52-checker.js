import path from 'path';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { humanClick } from './human-mouse.js';
import { getProfileDir } from './login-session-manager.js';
import { launchSystemChromeCdp } from './system-chrome-cdp.js';

const CHECKIN_LINK = [
  'a#rewards[href*="home.php?mod=task"][href*="do=apply"][href*="id=2"]',
  '#res-sign a[href*="home.php?mod=task"][href*="do=apply"][href*="id=2"]',
  'a[href*="home.php?mod=task"][href*="do=apply"][href*="id=2"]',
].join(',');

export function is52PojieSite(site) {
  try {
    const hostname = new URL(site.url).hostname;
    return hostname === '52pojie.cn' || hostname === 'www.52pojie.cn';
  } catch {
    return false;
  }
}

export async function poJie52Checkin(site) {
  const startTime = Date.now();
  let browserSession = null;

  try {
    browserSession = await launchSystemChromeCdp(getProfileDir(site.id));
    const { context } = browserSession;
    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(30000);

    await page.goto(site.url, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForTimeout(4000);

    if (!await isLoggedIn(page)) {
      const screenshot = await takeScreenshot(page, site.id, 'failed');
      return result(
        'failed',
        '吾爱破解登录状态已失效，请重新建立登录状态',
        startTime,
        screenshot,
      );
    }

    const checkinLink = page.locator(CHECKIN_LINK).first();
    if (await checkinLink.count() === 0) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        '吾爱破解今日已签到（签到入口已隐藏）',
        startTime,
        screenshot,
      );
    }

    const responsePromise = page.waitForResponse(taskResponse => {
      try {
        const url = new URL(taskResponse.url());
        return url.pathname.endsWith('/home.php')
          && url.searchParams.get('mod') === 'task'
          && url.searchParams.get('do') === 'apply';
      } catch {
        return false;
      }
    }, { timeout: 30000 }).catch(() => null);

    if (await checkinLink.isVisible().catch(() => false)) {
      await humanClick(page, checkinLink);
    } else {
      // 部分皮肤下入口存在但不可见，退回直接访问任务链接
      const href = await checkinLink.getAttribute('href');
      await page.goto(new URL(href, page.url()).href, {
        waitUntil: 'domcontentloaded',
        timeout: 30000,
      });
    }

    const response = await responsePromise;
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(3000);

    const responseText = cleanTaskMessage(
      await page.locator(
        '#messagetext, .messagetext, .alert_info, .alert_right, #ct',
      ).first().innerText().catch(() => ''),
    );

    await page.goto(site.url, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForTimeout(3000);

    const stillAvailable = await page.locator(CHECKIN_LINK).count() > 0;
    const verdict = classifyTaskMessage(responseText);
    // waitForResponse 不跟随重定向，3xx 也视为请求送达
    const responseOk = Boolean(response
      && (response.ok() || (response.status() >= 300 && response.status() < 400)));
    if ((!stillAvailable || verdict === 'success') && responseOk) {
      const detail = extractResultDetail(responseText);
      const screenshot = await takeScreenshot(page, site.id, 'success');
      return result(
        'success',
        `吾爱破解签到成功${detail ? `（${detail}）` : ''}`,
        startTime,
        screenshot,
      );
    }

    if (verdict === 'already') {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        `吾爱破解今日已签到（${extractResultDetail(responseText) || '任务已完成'}）`,
        startTime,
        screenshot,
      );
    }

    const screenshot = await takeScreenshot(page, site.id, 'failed');
    return result(
      'failed',
      responseText
        ? `吾爱破解签到后未确认成功：${responseText}`
        : `吾爱破解签到后未确认成功（HTTP ${response?.status() || '无响应'}）`,
      startTime,
      screenshot,
    );
  } catch (error) {
    const profileBusy = /user data directory is already in use|ProcessSingleton|profile.*in use/i
      .test(error.message);
    return result(
      'failed',
      profileBusy
        ? '吾爱破解专用 Chrome 正在使用中，请关闭对应窗口后重试'
        : `吾爱破解签到异常：${error.message}`,
      startTime,
    );
  } finally {
    if (browserSession) await browserSession.close().catch(() => {});
  }
}

async function isLoggedIn(page) {
  const hasLogout = await page.locator('a[href*="action=logout"]').count() > 0;
  const hasUserMenu = await page.locator(
    '#um .avt, #um .vwmy, #um a[href*="space-uid"]',
  ).count() > 0;
  const hasLoginForm = await page.locator(
    'form[action*="logging"], input[name="username"]',
  ).count() > 0;
  return (hasLogout || hasUserMenu) && !hasLoginForm;
}

function compactText(value) {
  return String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 240);
}

const REDIRECT_HINT_PATTERN = /[，。,.\s]*如果您的浏览器没有自动跳转[\s\S]*$/;

export function cleanTaskMessage(value) {
  return compactText(String(value || '').replace(REDIRECT_HINT_PATTERN, ''));
}

export function classifyTaskMessage(text) {
  if (/恭喜您[^。！]{0,40}任务已完成|任务申请成功|签到成功/.test(text)) return 'success';
  if (/已经申请过|今日已签到|已经签到|任务已完成/.test(text)) return 'already';
  return '';
}

export function extractResultDetail(value) {
  const text = cleanTaskMessage(value);
  if (!text) return '';

  const match = text.match(
    /(?:任务申请成功|任务已完成|签到成功|已经申请过[^。！，]{0,60}|获得[^。！]{0,80}|奖励[^。！]{0,80})/,
  );
  return compactText(match?.[0] || text).slice(0, 120);
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
    const filename = `site${siteId}_52pojie_${status}_${timestamp}.png`;
    await page.screenshot({
      path: path.join(SCREENSHOTS_DIR, filename),
      fullPage: false,
    });
    return filename;
  } catch {
    return null;
  }
}
