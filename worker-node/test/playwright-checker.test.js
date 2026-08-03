import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import {
  playwrightCheckAlreadyDone,
  playwrightCheckin,
} from '../src/checkin/playwright-checker.js';
import { getProfileDir } from '../src/checkin/login-session-manager.js';

function fakeBrowser(buttonText = '签到') {
  let clicked = false;
  let closed = false;
  const button = {
    textContent: async () => (clicked ? '已签' : buttonText),
    scrollIntoViewIfNeeded: async () => {},
    click: async () => { clicked = true; },
  };
  const page = {
    setDefaultTimeout: () => {},
    goto: async () => {},
    waitForTimeout: async () => {},
    waitForSelector: async () => button,
    $: async selector => (selector === '#already' ? null : button),
    screenshot: async () => {},
  };
  const context = {
    addInitScript: async () => {},
    addCookies: async () => {},
    pages: () => [page],
    close: async () => { closed = true; },
  };
  let launchedProfile = null;
  const chromium = {
    launchPersistentContext: async profileDir => {
      launchedProfile = profileDir;
      return context;
    },
  };
  return {
    chromium,
    clicked: () => clicked,
    closed: () => closed,
    launchedProfile: () => launchedProfile,
  };
}

test('generic Playwright check-in uses the account UUID persistent profile', async () => {
  const accountId = 'ad43c1c4-bce8-4944-97b0-9e106a1de64b';
  const browser = fakeBrowser();
  const result = await playwrightCheckin({
    id: accountId,
    name: 'Account',
    url: 'https://example.test/checkin',
    config: { checkinButtonSelector: '#checkin' },
  }, { chromium: browser.chromium });

  assert.equal(result.status, 'success');
  assert.equal(browser.launchedProfile(), getProfileDir(accountId));
  assert.equal(path.basename(browser.launchedProfile()), accountId);
  assert.equal(browser.clicked(), true);
  assert.equal(browser.closed(), true);
});

test('generic reconciliation is read-only and reports a conclusive pending action', async () => {
  const browser = fakeBrowser('签到');
  const result = await playwrightCheckAlreadyDone({
    id: '92f0dbbb-2756-4e0b-8d88-9f13a32a148b',
    name: 'Account',
    url: 'https://example.test/checkin',
    config: { checkinButtonSelector: '#checkin' },
  }, { chromium: browser.chromium });

  assert.deepEqual(result, {
    done: false,
    conclusive: true,
    message: '已确认今日尚未签到',
  });
  assert.equal(browser.clicked(), false);
  assert.equal(browser.closed(), true);
});
