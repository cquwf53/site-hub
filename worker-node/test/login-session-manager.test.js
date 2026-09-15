import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-login-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');

const {
  getProfileDir,
  isAuthenticatedPage,
  isAuthenticatedUrl,
  loginSessionManager,
} = await import(
  '../src/checkin/login-session-manager.js'
);


test('XKool login detection only accepts its authenticated dashboard', () => {
  assert.equal(
    isAuthenticatedUrl(
      'https://new.xkool.cfd/dashboard/overview',
      'https://new.xkool.cfd/dashboard/overview',
    ),
    true,
  );
  assert.equal(
    isAuthenticatedUrl(
      'https://new.xkool.cfd/dashboard/overview',
      'https://new.xkool.cfd/sign-in?redirect=%2Fdashboard%2Foverview',
    ),
    false,
  );
  assert.equal(
    isAuthenticatedUrl(
      'https://new.xkool.cfd/dashboard/overview',
      'https://example.com/dashboard/overview',
    ),
    false,
  );
});

test('52pojie verification uses read-only login indicators instead of same-origin URL', async () => {
  const page = counts => ({
    url: () => 'https://www.52pojie.cn/',
    locator: selector => ({
      count: async () => counts.get(selector) || 0,
    }),
  });
  const loggedIn = new Map([
    ['a[href*="action=logout"]', 1],
    ['#um .avt, #um .vwmy, #um a[href*="space-uid"]', 1],
  ]);
  assert.equal(
    await isAuthenticatedPage(
      { url: 'https://www.52pojie.cn/' },
      page(loggedIn),
    ),
    true,
  );

  const guest = new Map([
    ['form[action*="logging"], input[name="username"]', 1],
  ]);
  assert.equal(
    await isAuthenticatedPage(
      { url: 'https://www.52pojie.cn/' },
      page(guest),
    ),
    false,
  );
});

test('V2EX verification requires authenticated top-navigation indicators', async () => {
  const page = (counts, url = 'https://www.v2ex.com/') => ({
    url: () => url,
    locator: selector => ({
      count: async () => counts.get(selector) || 0,
    }),
  });
  const loggedIn = new Map([
    ['#Top a[href^="/member/"]', 1],
    ['#Top a[href="/settings"]', 1],
    ['#Top a[href^="/signout"]', 1],
  ]);
  assert.equal(
    await isAuthenticatedPage(
      { url: 'https://www.v2ex.com/' },
      page(loggedIn),
    ),
    true,
  );

  const guest = new Map([
    ['#Top a[href="/signin"], #Top a[href="/signup"]', 2],
  ]);
  assert.equal(
    await isAuthenticatedPage(
      { url: 'https://www.v2ex.com/' },
      page(guest),
    ),
    false,
  );
});

test('unknown generic same-origin homepage fails closed', async () => {
  const page = {
    url: () => 'https://unknown.example/account',
    locator: () => ({ count: async () => 1 }),
  };
  assert.equal(
    await isAuthenticatedPage(
      { url: 'https://unknown.example/' },
      page,
    ),
    false,
  );
});

test('copied legacy marker remains unverified until SiteHub performs a live check', () => {
  const site = { id: 'marker-account', name: 'Marker', url: 'https://example.test/' };
  const profileDir = getProfileDir(site.id);
  fs.mkdirSync(profileDir, { recursive: true });
  fs.writeFileSync(path.join(profileDir, 'login-state.json'), JSON.stringify({
    siteId: 'legacy-id',
    authenticatedAt: '2026-07-23T00:00:00.000Z',
    lastUrl: site.url,
  }));
  assert.equal(loginSessionManager.getStatus(site).state, 'unverified');

  loginSessionManager.writeMarker(site, site.url, { verified: true });
  assert.equal(loginSessionManager.getStatus(site).state, 'ready');
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});

test('XKool live verification requires an authenticated API result on its own origin', async () => {
  let requests = 0;
  const page = (origin, authenticated) => ({
    url: () => origin,
    evaluate: async () => { requests += 1; return authenticated; },
  });
  const site = { url: 'https://new.xkool.cfd/dashboard/overview' };
  assert.equal(await isAuthenticatedPage(site, page(site.url, true)), true);
  assert.equal(await isAuthenticatedPage(site, page(site.url, false)), false);
  assert.equal(await isAuthenticatedPage(site, page('https://other.test/', true)), false);
  assert.equal(requests, 2);
});

test('profile verification returns busy while a check-in owns the same profile', async () => {
  const { acquireProfile } = await import('../src/profile-locks.js');
  const site = { id: 'locked-account', url: 'https://new.xkool.cfd/' };
  const release = acquireProfile(site.id, 'checkin');
  try {
    const status = await loginSessionManager.verify(site);
    assert.equal(status.state, 'busy');
  } finally { release(); }
  const reacquired = acquireProfile(site.id, 'after-checkin');
  reacquired();
});

test('XKool verification validates the live API payload instead of trusting cached user data', async () => {
  const { runInNewContext } = await import('node:vm');
  for (const payload of [
    { success: true, data: { id: 42 } },
    { success: false, data: { id: 42 } },
    { success: true, data: {} },
  ]) {
    const page = {
      url: () => 'https://new.xkool.cfd/dashboard/overview',
      evaluate: callback => runInNewContext(`(${callback.toString()})()`, {
        localStorage: { getItem: () => '{"id":42}' },
        fetch: async (url, options) => {
          assert.equal(url, '/api/user/self');
          assert.equal(options.method || 'GET', 'GET');
          assert.equal(options.headers['New-Api-User'], '42');
          return { ok: true, json: async () => payload };
        },
      }),
    };
    assert.equal(await isAuthenticatedPage({ url: page.url() }, page),
      payload.success === true && payload.data.id === 42);
  }
});
