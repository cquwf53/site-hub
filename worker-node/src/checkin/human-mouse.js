/**
 * 拟人化鼠标模拟 — 贝塞尔曲线轨迹、随机抖动与按压延迟。
 * 供 CDP 接管真实 Chrome 的适配器在点击时模拟真人操作。
 */

import { randomInt as cryptoRandomInt } from 'crypto';

const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const lastPositions = new WeakMap();

export function buildMousePath(from, to, randomInteger = cryptoRandomInt) {
  const distance = Math.hypot(to.x - from.x, to.y - from.y);
  const steps = Math.max(12, Math.min(40, Math.round(distance / 15) + randomInteger(4, 9)));
  const offset = Math.round(Math.min(120, Math.max(24, distance * 0.25)));

  const control1 = {
    x: from.x + (to.x - from.x) * 0.25 + randomInteger(-offset, offset + 1),
    y: from.y + (to.y - from.y) * 0.25 + randomInteger(-offset, offset + 1),
  };
  const control2 = {
    x: from.x + (to.x - from.x) * 0.75 + randomInteger(-offset, offset + 1),
    y: from.y + (to.y - from.y) * 0.75 + randomInteger(-offset, offset + 1),
  };

  const points = [];
  for (let index = 1; index <= steps; index++) {
    const t = index / steps;
    // 先加速后减速的缓动，贴近真人移动节奏
    const eased = t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2;
    const point = cubicBezierPoint(from, control1, control2, to, eased);
    const jitter = () => (index < steps ? randomInteger(-2, 3) : 0);
    points.push({ x: point.x + jitter(), y: point.y + jitter() });
  }
  return points;
}

function cubicBezierPoint(from, control1, control2, to, t) {
  const u = 1 - t;
  return {
    x: u ** 3 * from.x + 3 * u * u * t * control1.x + 3 * u * t * t * control2.x + t ** 3 * to.x,
    y: u ** 3 * from.y + 3 * u * u * t * control1.y + 3 * u * t * t * control2.y + t ** 3 * to.y,
  };
}

async function moveMouse(page, target) {
  const viewport = page.viewportSize() || { width: 1280, height: 800 };
  const from = lastPositions.get(page) || {
    x: cryptoRandomInt(Math.round(viewport.width * 0.1), Math.round(viewport.width * 0.6)),
    y: cryptoRandomInt(Math.round(viewport.height * 0.2), Math.round(viewport.height * 0.8)),
  };

  for (const point of buildMousePath(from, target)) {
    await page.mouse.move(point.x, point.y);
    await sleep(cryptoRandomInt(6, 19));
  }
  lastPositions.set(page, target);
}

export async function humanClick(page, locator) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) {
    await locator.click();
    return;
  }

  const target = {
    x: box.x + box.width * (cryptoRandomInt(30, 71) / 100),
    y: box.y + box.height * (cryptoRandomInt(35, 66) / 100),
  };
  await moveMouse(page, target);
  await sleep(cryptoRandomInt(70, 181));
  await page.mouse.down();
  await sleep(cryptoRandomInt(45, 131));
  await page.mouse.up();
}
