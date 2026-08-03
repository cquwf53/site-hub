import fs from 'fs';
import path from 'path';
import { spawn } from 'child_process';
import { PROFILES_DIR, profileDirFor } from '../runtime-paths.js';
import { trackChildProcess } from '../process-registry.js';
import { redactText } from '../redact.js';
import { launchSystemChromeCdp } from './system-chrome-cdp.js';

function normalizeAccountKey(value) {
  const raw = String(value ?? '').trim();
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(raw)) {
    throw new Error('Invalid account profile key');
  }
  return raw;
}

function getProfileDir(siteId) {
  return profileDirFor(normalizeAccountKey(siteId));
}

function getMarkerPath(siteId) {
  return path.join(getProfileDir(siteId), 'login-state.json');
}

function sanitizeUrl(value) {
  try {
    const url = new URL(value);
    return `${url.origin}${url.pathname}`;
  } catch {
    return '';
  }
}

export function isAuthenticatedUrl(siteUrl, currentUrl) {
  try {
    const site = new URL(siteUrl);
    const current = new URL(currentUrl);

    if (site.origin !== current.origin) return false;

    if (site.hostname === 'new.xkool.cfd') {
      return current.pathname.startsWith('/dashboard/');
    }

    return !/^\/(?:sign-in|signin|login)(?:\/|$)/i.test(current.pathname);
  } catch {
    return false;
  }
}

function findChromeExecutable() {
  const candidates = [
    process.env.CHROME_PATH,
    process.env.PROGRAMFILES
      ? path.join(process.env.PROGRAMFILES, 'Google', 'Chrome', 'Application', 'chrome.exe')
      : null,
    process.env['PROGRAMFILES(X86)']
      ? path.join(process.env['PROGRAMFILES(X86)'], 'Google', 'Chrome', 'Application', 'chrome.exe')
      : null,
    process.env.LOCALAPPDATA
      ? path.join(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe')
      : null,
  ].filter(Boolean);

  return candidates.find(candidate => fs.existsSync(candidate)) || null;
}

async function isAuthenticatedPage(site, page) {
  let hostname = '';
  try {
    hostname = new URL(site.url).hostname;
  } catch {
    return false;
  }

  if (hostname === 'nodeloc.com' || hostname === 'www.nodeloc.com') {
    const hasSession = await page.evaluate(async () => {
      try {
        const response = await fetch('/session/current.json', {
          credentials: 'include',
        });
        if (!response.ok) return false;
        const payload = await response.json();
        return Boolean(payload.current_user?.id || payload.user?.id);
      } catch {
        return false;
      }
    });
    if (hasSession) return true;

    return await page.locator(
      'li.header-dropdown-toggle.checkin-icon button.checkin-button',
    ).count() > 0;
  }

  if (hostname === 'nodeseek.com' || hostname === 'www.nodeseek.com') {
    return await page.locator('a.Username').count() > 0;
  }

  if (hostname === 'sub.100xlabs.space') {
    return await page.locator('a[href="/check-in"]').count() > 0
      && !/^\/(?:sign-in|signin|login)(?:\/|$)/i.test(new URL(page.url()).pathname);
  }

  if (hostname === 'api.123nhh.com') {
    return await page.locator('a[href="/console/personal"]').count() > 0
      && !/^\/(?:sign-in|signin|login)(?:\/|$)/i.test(new URL(page.url()).pathname);
  }

  if (hostname === 'jianzhile.vip' || hostname === 'www.jianzhile.vip') {
    const hasUser = await page.evaluate(() => {
      try {
        const user = JSON.parse(localStorage.getItem('user') || 'null');
        return Number(user?.id) > 0;
      } catch {
        return false;
      }
    }).catch(() => false);
    if (hasUser) return true;

    return await page.locator('a[href="/console/personal"]').count() > 0
      && !/^\/(?:sign-in|signin|login)(?:\/|$)/i.test(new URL(page.url()).pathname);
  }

  if (hostname === 'pt.btschool.club') {
    const infoText = await page.locator('#info_block').innerText().catch(() => '');
    return /欢迎回来|退出/.test(infoText);
  }

  return isAuthenticatedUrl(site.url, page.url());
}

class LoginSessionManager {
  constructor() {
    this.sessions = new Map();
    fs.mkdirSync(PROFILES_DIR, { recursive: true });
  }

  readMarker(siteId) {
    try {
      return JSON.parse(fs.readFileSync(getMarkerPath(siteId), 'utf8'));
    } catch {
      return null;
    }
  }

  writeMarker(site, currentUrl) {
    const marker = {
      siteId: site.id,
      siteName: site.name,
      authenticatedAt: new Date().toISOString(),
      lastUrl: sanitizeUrl(currentUrl),
    };

    fs.mkdirSync(getProfileDir(site.id), { recursive: true });
    fs.writeFileSync(getMarkerPath(site.id), JSON.stringify(marker, null, 2), 'utf8');
    return marker;
  }

  clearMarker(siteId) {
    fs.rmSync(getMarkerPath(siteId), { force: true });
  }

  getStatus(site) {
    const session = this.sessions.get(String(site.id));
    if (session) {
      return {
        state: session.state,
        startedAt: session.startedAt,
        authenticatedAt: session.authenticatedAt || null,
        currentUrl: session.currentUrl || '',
        method: session.method || null,
        error: session.error || null,
      };
    }

    const marker = this.readMarker(site.id);
    if (marker) {
      return {
        state: 'ready',
        startedAt: null,
        authenticatedAt: marker.authenticatedAt || null,
        currentUrl: marker.lastUrl || '',
        method: null,
        error: null,
      };
    }

    return {
      state: 'not_configured',
      startedAt: null,
      authenticatedAt: null,
      currentUrl: '',
      method: null,
      error: null,
    };
  }

  async startSystemChrome(site) {
    const siteId = String(site.id);
    const existing = this.sessions.get(siteId);
    if (existing) return this.getStatus(site);

    const executable = findChromeExecutable();
    if (!executable) {
      return {
        state: 'error',
        startedAt: null,
        authenticatedAt: null,
        currentUrl: '',
        method: 'system_chrome',
        error: '未找到系统 Google Chrome',
      };
    }

    fs.mkdirSync(getProfileDir(siteId), { recursive: true });
    const session = {
      state: 'starting',
      startedAt: new Date().toISOString(),
      authenticatedAt: null,
      currentUrl: sanitizeUrl(site.url),
      method: 'system_chrome',
      error: null,
      context: null,
      browserProcess: null,
    };
    this.sessions.set(siteId, session);

    try {
      const browserProcess = trackChildProcess(spawn(executable, [
        `--user-data-dir=${getProfileDir(siteId)}`,
        '--no-first-run',
        '--no-default-browser-check',
        site.url,
      ], {
        detached: true,
        stdio: 'ignore',
        windowsHide: false,
      }), `login-chrome:${siteId}`);
      session.browserProcess = browserProcess;

      await new Promise((resolve, reject) => {
        browserProcess.once('spawn', resolve);
        browserProcess.once('error', reject);
      });

      session.state = 'running';
      browserProcess.once('exit', () => {
        this.sessions.delete(siteId);
        const timer = setTimeout(() => {
          void this.verify(site).catch(error => {
            console.error(`[登录状态] ${site.name} 自动验证失败：${error.message}`);
          });
        }, 1500);
        timer.unref?.();
      });
      browserProcess.unref();
      return this.getStatus(site);
    } catch (error) {
      this.sessions.delete(siteId);
      return {
        state: 'error',
        startedAt: session.startedAt,
        authenticatedAt: null,
        currentUrl: session.currentUrl,
        method: 'system_chrome',
        error: error.message,
      };
    }
  }

  async verify(site) {
    const siteId = String(site.id);
    const existing = this.sessions.get(siteId);
    if (existing?.browserProcess && existing.browserProcess.exitCode === null) {
      throw new Error('请先关闭该站点的普通 Chrome 登录窗口');
    }

    const session = {
      state: 'starting',
      startedAt: new Date().toISOString(),
      authenticatedAt: null,
      currentUrl: '',
      method: 'verification',
      error: null,
      context: null,
      browserSession: null,
    };
    this.sessions.set(siteId, session);

    try {
      const hostname = new URL(site.url).hostname;
      if (
        hostname === 'nodeseek.com'
        || hostname === 'www.nodeseek.com'
        || hostname === 'pt.btschool.club'
      ) {
        session.browserSession = await launchSystemChromeCdp(getProfileDir(siteId));
        session.context = session.browserSession.context;
      } else {
        const { chromium } = await import('playwright');
        session.context = await chromium.launchPersistentContext(getProfileDir(siteId), {
          channel: 'chrome',
          headless: true,
          locale: 'zh-CN',
        });
      }
      const page = session.context.pages()[0] || await session.context.newPage();
      await page.goto(site.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(5000);
      session.currentUrl = sanitizeUrl(page.url());

      if (!await isAuthenticatedPage(site, page)) {
        this.clearMarker(siteId);
        throw new Error('登录状态验证失败，请重新登录');
      }

      const marker = this.writeMarker(site, page.url());
      return {
        state: 'ready',
        startedAt: session.startedAt,
        authenticatedAt: marker.authenticatedAt,
        currentUrl: marker.lastUrl,
        method: 'verification',
        error: null,
      };
    } finally {
      if (session.browserSession) {
        await session.browserSession.close().catch(() => {});
      } else if (session.context) {
        await session.context.close().catch(() => {});
      }
      this.sessions.delete(siteId);
    }
  }

  async start(site) {
    const siteId = String(site.id);
    const existing = this.sessions.get(siteId);
    if (existing) return this.getStatus(site);

    const session = {
      state: 'starting',
      startedAt: new Date().toISOString(),
      authenticatedAt: null,
      currentUrl: '',
      error: null,
      context: null,
      authCheckTimer: null,
    };
    this.sessions.set(siteId, session);

    try {
      fs.mkdirSync(getProfileDir(siteId), { recursive: true });
      const { chromium } = await import('playwright');
      const context = await chromium.launchPersistentContext(getProfileDir(siteId), {
        channel: 'chrome',
        headless: false,
        viewport: null,
        locale: 'zh-CN',
        args: ['--no-first-run', '--no-default-browser-check'],
      });

      session.context = context;
      session.state = 'running';

      const inspectPage = page => {
        const update = () => {
          const currentUrl = page.url();
          session.currentUrl = sanitizeUrl(currentUrl);
          clearTimeout(session.authCheckTimer);
          session.authCheckTimer = null;

          if (isAuthenticatedUrl(site.url, currentUrl)) {
            session.authCheckTimer = setTimeout(() => {
              const stableUrl = page.url();
              if (!isAuthenticatedUrl(site.url, stableUrl)) return;

              const marker = this.writeMarker(site, stableUrl);
              session.state = 'ready';
              session.authenticatedAt = marker.authenticatedAt;
              session.currentUrl = marker.lastUrl;
              session.authCheckTimer = null;
            }, 3000);
            return;
          }

          let pathname = '';
          try {
            pathname = new URL(currentUrl).pathname;
          } catch {
            // about:blank and transient navigation URLs are ignored.
          }

          if (/^\/(?:sign-in|signin|login)(?:\/|$)/i.test(pathname)) {
            session.state = 'running';
            session.authenticatedAt = null;
            this.clearMarker(siteId);
          }
        };

        page.on('load', update);
        page.on('framenavigated', frame => {
          if (frame === page.mainFrame()) update();
        });
        update();
      };

      context.pages().forEach(inspectPage);
      context.on('page', inspectPage);
      context.on('close', () => {
        clearTimeout(session.authCheckTimer);
        this.sessions.delete(siteId);
      });

      const page = context.pages()[0] || await context.newPage();
      page.setDefaultTimeout(30000);
      await page.goto(site.url, {
        waitUntil: 'domcontentloaded',
        timeout: 30000,
      });

      return this.getStatus(site);
    } catch (error) {
      session.state = 'error';
      session.error = redactText(error?.message || error, 1000);
      const errorStatus = {
        state: 'error',
        startedAt: session.startedAt,
        authenticatedAt: session.authenticatedAt,
        currentUrl: session.currentUrl,
        error: session.error,
      };
      if (session.context) {
        await session.context.close().catch(() => {});
      }
      this.sessions.delete(siteId);
      return errorStatus;
    }
  }

  async close(siteId) {
    const key = String(siteId);
    const session = this.sessions.get(key);
    if (!session) return false;
    clearTimeout(session.authCheckTimer);
    if (session.browserSession) {
      await session.browserSession.close().catch(() => {});
    } else if (session.context) {
      await session.context.close().catch(() => {});
    }
    if (session.browserProcess && session.browserProcess.exitCode === null) {
      session.browserProcess.kill();
    }
    this.sessions.delete(key);
    return true;
  }

  async closeAll() {
    await Promise.allSettled([...this.sessions.keys()].map(siteId => this.close(siteId)));
  }
}

export const loginSessionManager = new LoginSessionManager();
export { PROFILES_DIR, getProfileDir };
