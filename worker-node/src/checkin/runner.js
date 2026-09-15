import { apiCheckin } from './api-checker.js';
import { cookieCheckin } from './cookie-checker.js';
import { playwrightCheckAlreadyDone, playwrightCheckin } from './playwright-checker.js';
import { isNodeLocSite, nodeLocCheckin } from './nodeloc-checker.js';
import { isNodeSeekSite, nodeSeekCheckin } from './nodeseek-checker.js';
import { isXkoolSite, xkoolCheckin } from './xkool-checker.js';
import { hundredXLabsCheckin, isHundredXLabsSite } from './hundredxlabs-checker.js';
import { api123NhhCheckin, isApi123NhhSite } from './api123nhh-checker.js';
import { btschoolCheckin, isBtschoolSite } from './btschool-checker.js';
import { is52PojieSite, poJie52Checkin } from './pojie52-checker.js';
import { isJianzhileSite, jianzhileCheckin, jianzhileCheckAlreadyDone } from './jianzhile-checker.js';
import fs from 'fs';
import path from 'path';
import { createHash } from 'crypto';
import { SCREENSHOTS_DIR } from '../runtime-paths.js';
import { redactText } from '../redact.js';
import { acquireProfile } from '../profile-locks.js';

const dedicatedAdapters = [
  { id: 'xkool', match: isXkoolSite, run: xkoolCheckin },
  { id: 'nodeloc', match: isNodeLocSite, run: nodeLocCheckin },
  { id: 'nodeseek', match: isNodeSeekSite, run: nodeSeekCheckin },
  { id: 'hundredxlabs', match: isHundredXLabsSite, run: hundredXLabsCheckin },
  { id: 'api123nhh', match: isApi123NhhSite, run: api123NhhCheckin },
  { id: 'jianzhile', match: isJianzhileSite, run: jianzhileCheckin, checkAlreadyDone: jianzhileCheckAlreadyDone },
  { id: 'btschool', match: isBtschoolSite, run: btschoolCheckin },
  { id: '52pojie', match: is52PojieSite, run: poJie52Checkin },
];

for (const adapter of dedicatedAdapters) adapter.tracksRemoteEffect = true;
const byId = new Map(dedicatedAdapters.map(item => [item.id, item]));

export function resolveCheckinRunner(site, adapterId) {
  if (adapterId === 'api') return { id: 'api', run: apiCheckin, tracksRemoteEffect: true };
  if (adapterId === 'cookie') return { id: 'cookie', run: cookieCheckin, tracksRemoteEffect: true };
  if (adapterId === 'playwright-generic') {
    return { id: adapterId, run: playwrightCheckin, tracksRemoteEffect: true, checkAlreadyDone: playwrightCheckAlreadyDone };
  }
  if (adapterId && byId.has(adapterId)) return byId.get(adapterId);

  const matched = dedicatedAdapters.find(item => item.match(site));
  if (matched) return matched;
  if (site.mode === 'api') return { id: 'api', run: apiCheckin, tracksRemoteEffect: true };
  if (site.mode === 'cookie') return { id: 'cookie', run: cookieCheckin, tracksRemoteEffect: true };
  return {
    id: 'playwright-generic',
    run: playwrightCheckin,
    tracksRemoteEffect: true,
    checkAlreadyDone: playwrightCheckAlreadyDone,
  };
}

function reasonFor(result) {
  if (result.reasonCode) return String(result.reasonCode);
  const message = String(result.message || '').toLowerCase();
  if (result.code === 'PROFILE_BUSY' || /profile.*(busy|in use)|processsingleton|正在使用|正在用于/.test(message)) return 'profile_busy';
  if (/executable.*(exist|found)|browser.*(install|launch)|未找到系统.*chrome|playwright 未安装/.test(message)) return 'browser_unavailable';
  if (/cross-origin redirect blocked/.test(message)) return 'redirect_blocked';
  if (result.status === 'success') return 'success';
  if (result.status === 'skipped' && /(already|已签|今日)/i.test(message)) return 'already_done';
  if (/(login|sign-in|cookie|登录|登入|未配置)/i.test(message)) return 'needs_login';
  if (/(captcha|ocr|验证码)/i.test(message)) return 'captcha_failed';
  if (/(timeout|timed out|超时)/i.test(message)) return 'timeout';
  return result.status === 'skipped' ? 'skipped' : 'adapter_error';
}

function reconciliationMethod(runner) {
  if (typeof runner.checkAlreadyDone === 'function') return runner.checkAlreadyDone.bind(runner);
  if (typeof runner.reconcile === 'function') return runner.reconcile.bind(runner);
  if (typeof runner.checkStatus === 'function') return runner.checkStatus.bind(runner);
  return null;
}

async function reconcileBeforeRetry(runner, site) {
  const check = reconciliationMethod(runner);
  if (!check) {
    return {
      status: 'effect_unknown',
      reasonCode: 'reconcile_unavailable',
      message: '适配器不支持安全状态确认，未重复执行签到',
    };
  }

  try {
    const result = await check(site);
    const status = String(result?.status || '').toLowerCase();
    const done = result === true
      || result?.done === true
      || result?.alreadyDone === true
      || ['already_done', 'already-done', 'done'].includes(status);
    const notDone = result === false
      || (result?.done === false && result?.conclusive === true)
      || (result?.alreadyDone === false && result?.conclusive === true)
      || ['not_done', 'not-done', 'pending'].includes(status);
    if (done) {
      return {
        status: 'skipped',
        reasonCode: 'reconciled',
        message: result?.message || '已确认今日签到已完成，未重复执行',
      };
    }
    if (notDone) return null;
    return {
      status: 'effect_unknown',
      reasonCode: 'reconcile_inconclusive',
      message: result?.message || '无法可靠确认远端签到状态，未重复执行',
    };
  } catch (error) {
    return {
      status: 'effect_unknown',
      reasonCode: 'reconcile_failed',
      message: `远端签到状态确认失败，未重复执行: ${error?.message || error}`,
    };
  }
}

export async function describeArtifact(candidate, artifactRoot = SCREENSHOTS_DIR) {
  if (!candidate || typeof candidate !== 'string') return null;
  const root = path.resolve(artifactRoot);
  const target = path.isAbsolute(candidate)
    ? path.resolve(candidate)
    : path.resolve(root, candidate);
  try {
    const [realRoot, realTarget] = await Promise.all([
      fs.promises.realpath(root),
      fs.promises.realpath(target),
    ]);
    const relative = path.relative(realRoot, realTarget);
    if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return null;
    const bytes = await fs.promises.readFile(realTarget);
    return {
      kind: 'screenshot',
      role: 'screenshot',
      relativePath: relative.split(path.sep).join('/'),
      sha256: createHash('sha256').update(bytes).digest('hex'),
      sizeBytes: bytes.length,
    };
  } catch {
    return null;
  }
}

export async function executeCheckin(params, options = {}) {
  const startedAt = Date.now();
  const account = params.account || {};
  const siteInfo = params.site || {};
  const config = { ...(params.checkinConfig || {}) };
  const site = {
    id: String(account.id || params.accountId || siteInfo.id),
    name: account.name ? `${siteInfo.name} / ${account.name}` : siteInfo.name,
    url: siteInfo.url,
    mode: account.mode || config.mode || 'playwright',
    config,
  };
  const resolveRunner = options.resolveRunner || resolveCheckinRunner;
  const artifactRoot = options.artifactRoot || SCREENSHOTS_DIR;
  const runner = resolveRunner(site, account.adapter || config.adapter);
  // Preserve uncertainty from an earlier attempt until a read-only check
  // conclusively proves no effect. A blocked reconciliation is never safe to replay.
  let remoteEffect = params.reconcile ? 'possible' : 'not_started';
  site.markRemoteEffect = () => { remoteEffect = 'possible'; };
  let result;
  let releaseProfile;
  try {
    if (!['api', 'cookie'].includes(runner.id)) {
      releaseProfile = acquireProfile(site.id, 'checkin');
    }
    if (params.reconcile) {
      result = await reconcileBeforeRetry(runner, site);
      if (!result) remoteEffect = 'not_started';
    }
    if (!result) {
      if (!runner.tracksRemoteEffect) site.markRemoteEffect();
      result = await runner.run(site);
    }
  } catch (error) {
    result = { status: 'failed', code: error.code, message: error?.message || String(error) };
  } finally {
    releaseProfile?.();
  }
  if (remoteEffect === 'possible' && result?.status === 'failed') {
    result = { ...result, status: 'effect_unknown' };
  }
  const artifacts = [];
  if (result?.screenshot) {
    const artifact = await describeArtifact(result.screenshot, artifactRoot);
    if (artifact) artifacts.push(artifact);
  }
  return {
    status: ['success', 'failed', 'skipped', 'cancelled', 'effect_unknown'].includes(result?.status)
      ? result.status
      : 'failed',
    reasonCode: reasonFor(result || {}),
    message: redactText(result?.message || 'Adapter returned no result', 2000),
    durationMs: Number(result?.duration_ms ?? (Date.now() - startedAt)),
    artifacts,
    metadata: {
      adapterId: runner.id,
      remoteEffect,
      nextAction: remoteEffect === 'not_started'
        ? '解决登录窗口占用、浏览器或配置问题后，可安全重试。'
        : result?.status === 'effect_unknown'
          ? '请先在站点确认今日签到结果；重试时会先只读核实，无法核实时不会重复签到。'
          : null,
    },
  };
}

export { dedicatedAdapters };
