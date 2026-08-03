import path from 'path';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { humanClick } from './human-mouse.js';
import { getProfileDir } from './login-session-manager.js';
import { launchSystemChromeCdp } from './system-chrome-cdp.js';

const CHECKIN_PATH = '/index.php';
const CHECKIN_ACTION = 'addbonus';

export function isBtschoolSite(site) {
  try {
    return new URL(site.url).hostname === 'pt.btschool.club';
  } catch {
    return false;
  }
}

export async function btschoolCheckin(site) {
  const startTime = Date.now();
  let browserSession = null;

  try {
    browserSession = await launchSystemChromeCdp(getProfileDir(site.id));
    const context = browserSession.context;
    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(30000);

    const dialogMessages = [];
    page.on('dialog', dialog => {
      dialogMessages.push(dialog.message());
      void dialog.accept().catch(() => {});
    });

    await page.goto(site.url, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForTimeout(3500);

    if (/\/login\.php$/i.test(new URL(page.url()).pathname)
      || !await isLoggedIn(page)) {
      return result(
        'failed',
        'BTSchool 登录状态已失效，请重新建立登录状态',
        startTime,
      );
    }

    const checkinLink = page.locator(
      'a[href*="index.php?action=addbonus"]',
    ).first();
    if (!await checkinLink.isVisible().catch(() => false)) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        'BTSchool 今日已签到（签到入口已隐藏）',
        startTime,
        screenshot,
      );
    }
    const beforeBonus = await readBonus(page);

    const responsePromise = page.waitForResponse(response => {
      try {
        const url = new URL(response.url());
        return url.pathname === CHECKIN_PATH
          && url.searchParams.get('action') === CHECKIN_ACTION;
      } catch {
        return false;
      }
    }, { timeout: 15000 }).catch(() => null);

    await humanClick(page, checkinLink);
    const response = await responsePromise;
    const responseText = response
      ? await response.text().catch(() => '')
      : '';
    await page.waitForTimeout(3000);

    const responseMessage = extractMessage(responseText);
    const combinedMessage = [
      ...dialogMessages,
      responseMessage,
    ].filter(Boolean).join(' ');

    const alreadyMessage = /已签到|已经签到|今日.*签|重复|请勿重复/i
      .test(combinedMessage);
    const explicitSuccess = /签到成功|成功签到|获得.{0,60}(?:魔力|奖励)|(?:魔力|奖励).{0,60}(?:增加|获得)/i
      .test(combinedMessage);

    await page.goto(site.url, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await page.waitForTimeout(2500);

    const stillAvailable = await page.locator(
      'a[href*="index.php?action=addbonus"]',
    ).first().isVisible().catch(() => false);

    const afterBonus = await readBonus(page);
    const bonusDelta = Number.isFinite(beforeBonus) && Number.isFinite(afterBonus)
      ? afterBonus - beforeBonus
      : null;

    if (alreadyMessage) {
      const screenshot = await takeScreenshot(page, site.id, 'skipped');
      return result(
        'skipped',
        `BTSchool 今日已签到${formatDetail(combinedMessage)}`,
        startTime,
        screenshot,
      );
    }

    if ((bonusDelta !== null && bonusDelta > 0)
      || (!stillAvailable && response?.ok())
      || explicitSuccess) {
      const reward = bonusDelta !== null && bonusDelta > 0
        ? `，获得 ${formatBonus(bonusDelta)} 魔力值`
        : formatDetail(combinedMessage);
      const screenshot = await takeScreenshot(page, site.id, 'success');
      return result(
        'success',
        `BTSchool 签到成功${reward}`,
        startTime,
        screenshot,
      );
    }

    const screenshot = await takeScreenshot(page, site.id, 'failed');
    return result(
      'failed',
      responseMessage || `BTSchool 签到后未确认成功（HTTP ${response?.status() || '无响应'}）`,
      startTime,
      screenshot,
    );
  } catch (error) {
    const profileBusy = /user data directory is already in use|ProcessSingleton|profile.*in use/i
      .test(error.message);
    return result(
      'failed',
      profileBusy
        ? 'BTSchool 专用 Chrome 正在使用中，请关闭登录窗口后重试'
        : `BTSchool 签到异常：${error.message}`,
      startTime,
    );
  } finally {
    if (browserSession) await browserSession.close().catch(() => {});
  }
}

async function isLoggedIn(page) {
  if (await page.locator('#info_block').count() === 0) return false;
  const infoText = await page.locator('#info_block').innerText().catch(() => '');
  return /欢迎回来|退出/.test(infoText);
}

function extractMessage(html) {
  if (!html) return '';

  const alertMatch = html.match(/alert\s*\(\s*(['"])(.*?)\1\s*\)/is);
  if (alertMatch?.[2]) return cleanText(decodeEntities(alertMatch[2]));

  const messageMatch = html.match(
    /(?:签到成功|成功签到|今日已签到|已经签到|请勿重复|获得[^<\r\n]{0,80}(?:魔力|奖励)|(?:魔力|奖励)[^<\r\n]{0,80}(?:增加|获得))[^<\r\n]{0,100}/i,
  );
  return messageMatch ? cleanText(decodeEntities(messageMatch[0])) : '';
}

function decodeEntities(value) {
  return value
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&quot;/gi, '"')
    .replace(/&#0*39;/gi, "'")
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>');
}

function cleanText(value) {
  return value
    .replace(/\\(['"\\])/g, '$1')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 180);
}

function formatDetail(message) {
  const detail = cleanText(message);
  return detail ? `（${detail}）` : '';
}

async function readBonus(page) {
  const infoText = await page.locator('#info_block').innerText().catch(() => '');
  const match = infoText.replace(/\s+/g, ' ').match(
    /魔力值\s*(?:\[使用\]\s*)?:\s*([\d,.]+)/,
  );
  if (!match) return null;

  const value = Number(match[1].replace(/,/g, ''));
  return Number.isFinite(value) ? value : null;
}

function formatBonus(value) {
  return Number(value).toLocaleString('en-US', {
    minimumFractionDigits: Number.isInteger(value) ? 0 : 1,
    maximumFractionDigits: 2,
  });
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
    const filename = `site${siteId}_btschool_${status}_${timestamp}.png`;
    await page.screenshot({
      path: path.join(SCREENSHOTS_DIR, filename),
      fullPage: false,
    });
    return filename;
  } catch {
    return null;
  }
}
