import fs from 'fs';
import net from 'net';
import path from 'path';
import { spawn } from 'child_process';
import { trackChildProcess } from '../process-registry.js';

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

async function findAvailablePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : null;
      server.close(error => {
        if (error) reject(error);
        else resolve(port);
      });
    });
  });
}

async function waitForDevtools(port, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (response.ok) return;
    } catch {
      // Chrome is still starting.
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error('普通 Chrome 调试端口未在规定时间内启动');
}

function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export async function launchSystemChromeCdp(profileDir) {
  const executable = findChromeExecutable();
  if (!executable) throw new Error('未找到系统 Google Chrome');

  fs.mkdirSync(profileDir, { recursive: true });
  const port = await findAvailablePort();
  const browserProcess = trackChildProcess(spawn(executable, [
    `--user-data-dir=${profileDir}`,
    '--remote-debugging-address=127.0.0.1',
    `--remote-debugging-port=${port}`,
    '--start-minimized',
    '--no-first-run',
    '--no-default-browser-check',
    'about:blank',
  ], {
    detached: true,
    stdio: 'ignore',
    windowsHide: false,
  }), 'system-chrome-cdp');

  await new Promise((resolve, reject) => {
    browserProcess.once('spawn', resolve);
    browserProcess.once('error', reject);
  });
  browserProcess.unref();

  try {
    await waitForDevtools(port);
    const { chromium } = await import('playwright');
    const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
    const context = browser.contexts()[0];
    if (!context) throw new Error('普通 Chrome 未提供可用的浏览器上下文');

    return {
      browser,
      context,
      process: browserProcess,
      async close() {
        await browser.close().catch(() => {});
        const deadline = Date.now() + 5000;
        while (processExists(browserProcess.pid) && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 100));
        }
        if (processExists(browserProcess.pid)) {
          browserProcess.kill();
        }
      },
    };
  } catch (error) {
    if (processExists(browserProcess.pid)) browserProcess.kill();
    throw error;
  }
}
