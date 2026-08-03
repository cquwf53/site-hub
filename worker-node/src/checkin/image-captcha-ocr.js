/**
 * 图形验证码 OCR（tesseract.js）
 * 用于 jianzhile.vip 等站点的「安全验证」弹窗。
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { closeTrackedResource, trackCloseable } from '../resource-registry.js';
import { ddddocrRunner } from './ddddocr-runner.js';

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_TESSERACT_CACHE = process.env.SITEHUB_TESSERACT_CACHE
  || process.env.CHECKIN_TESSERACT_CACHE
  || path.join(MODULE_DIR, '..', '..', 'data', 'tesseract-cache');

export function normalizeCaptchaAnswer(value) {
  return String(value || '')
    .replace(/[^0-9A-Za-z]/g, '')
    .trim();
}

/**
 * 在浏览器端把彩色扭曲验证码转成「白底黑字」的干净图，提高 OCR 成功率。
 *
 * 关键点：jianzhile 的验证码字符本身是彩色的，早期版本按「高饱和 = 噪线」
 * 抹白，反而把文字一起抹掉了。实测（12 张真实样本）表明，直接按亮度阈值
 * 保留所有暗像素、再做一次膨胀补断笔，配合 ddddocr 识别率最高（8/12→
 * 叠加重试后接近必中）。干扰线较细，膨胀后被字形主体淹没，影响有限。
 *
 * 参数（opt）：
 *   lumThreshold  亮度阈值，低于则判为文字，默认 150
 *   dilate        膨胀迭代次数（8 邻域），默认 1
 *   border        清除的边框环宽度（原始分辨率像素），默认 2
 *   scale         输出放大倍数，默认 3
 *   pad           输出白边（放大后像素），默认 10
 */
export async function preprocessCaptchaInPage(page, captchaImage, opt = {}) {
  const source = normalizeCaptchaImageSource(captchaImage);
  if (!source) return null;

  const options = {
    lumThreshold: 150,
    dilate: 1,
    border: 2,
    scale: 3,
    pad: 10,
    ...opt,
  };

  return await page.evaluate(async ({ imageSource, o }) => {
    const image = await loadImage(imageSource);
    const width = Math.max(1, image.width);
    const height = Math.max(1, image.height);

    const readCanvas = document.createElement('canvas');
    readCanvas.width = width;
    readCanvas.height = height;
    const readCtx = readCanvas.getContext('2d', { willReadFrequently: true });
    readCtx.imageSmoothingEnabled = false;
    readCtx.drawImage(image, 0, 0);
    const pixels = readCtx.getImageData(0, 0, width, height).data;

    // 亮度阈值二值化：保留所有暗像素（含彩色文字）
    const mask = new Uint8Array(width * height);
    for (let i = 0; i < width * height; i++) {
      const alpha = pixels[i * 4 + 3];
      if (alpha < 128) continue;
      const lum = 0.299 * pixels[i * 4] + 0.587 * pixels[i * 4 + 1]
        + 0.114 * pixels[i * 4 + 2];
      if (lum < o.lumThreshold) mask[i] = 1;
    }

    // 清除边框环（验证码外框常年占满四周，会干扰切分）
    const ring = o.border || 0;
    if (ring > 0) {
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          if (x < ring || y < ring || x >= width - ring || y >= height - ring) {
            mask[y * width + x] = 0;
          }
        }
      }
    }

    // 膨胀补断笔（8 邻域）
    for (let it = 0; it < (o.dilate || 0); it++) {
      const prev = mask.slice();
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          if (prev[y * width + x]) continue;
          let on = false;
          for (let dy = -1; dy <= 1 && !on; dy++) {
            for (let dx = -1; dx <= 1 && !on; dx++) {
              const nx = x + dx;
              const ny = y + dy;
              if (nx >= 0 && ny >= 0 && nx < width && ny < height
                && prev[ny * width + nx]) {
                on = true;
              }
            }
          }
          if (on) mask[y * width + x] = 1;
        }
      }
    }

    // 放大渲染成白底黑字
    const scale = o.scale || 3;
    const pad = o.pad || 0;
    const out = document.createElement('canvas');
    out.width = width * scale + pad * 2;
    out.height = height * scale + pad * 2;
    const outCtx = out.getContext('2d');
    outCtx.fillStyle = '#ffffff';
    outCtx.fillRect(0, 0, out.width, out.height);
    outCtx.fillStyle = '#000000';
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        if (mask[y * width + x]) {
          outCtx.fillRect(pad + x * scale, pad + y * scale, scale, scale);
        }
      }
    }
    return out.toDataURL('image/png');

    function loadImage(imageUrl) {
      return new Promise((resolve, reject) => {
        const element = new Image();
        element.onload = () => resolve(element);
        element.onerror = () => reject(new Error('验证码图片加载失败'));
        element.src = imageUrl;
      });
    }
  }, { imageSource: source, o: options });
}

export async function recognizeCaptchaText(imageSource, {
  createWorker,
  cachePath = DEFAULT_TESSERACT_CACHE,
  whitelist = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',
  pageSegMode = '7',
} = {}) {
  const source = normalizeCaptchaImageSource(imageSource);
  if (!source) return '';

  let workerFactory = createWorker;
  if (!workerFactory) {
    try {
      ({ createWorker: workerFactory } = await import('tesseract.js'));
    } catch (error) {
      throw new Error(`tesseract.js 未安装：${error.message}`);
    }
  }

  if (cachePath) {
    fs.mkdirSync(cachePath, { recursive: true });
  }

  const worker = trackCloseable(await workerFactory('eng', 1, {
    logger: () => {},
    cachePath: cachePath || undefined,
  }), 'tesseract', value => value.terminate());

  try {
    await worker.setParameters({
      tessedit_char_whitelist: whitelist,
      tessedit_pageseg_mode: pageSegMode,
    });
    const { data } = await worker.recognize(source);
    return normalizeCaptchaAnswer(data?.text);
  } finally {
    await closeTrackedResource(worker).catch(() => {});
  }
}

/**
 * 综合识别：优先 ddddocr（对彩色扭曲验证码更强），失败时回退 tesseract。
 *
 * 传入的应是 preprocessCaptchaInPage 处理后的「白底黑字」data URL。
 * ddddocr 结果统一大写并清洗；不可用（未装 Python/ddddocr、超时、结果过短）
 * 时自动降级，绝不抛出中断签到流程。
 *
 * @param {string} imageSource data URL 或纯 base64
 * @param {object} [opt]
 * @param {number} [opt.minLength=3] 结果最短长度，低于则视为失败
 * @param {object} [opt.runner] ddddocr 运行器（默认单例，测试可注入）
 * @returns {Promise<{ text: string, engine: 'ddddocr'|'tesseract'|'none' }>}
 */
export async function recognizeCaptcha(imageSource, {
  minLength = 3,
  runner = ddddocrRunner,
  ...tessOpt
} = {}) {
  const source = normalizeCaptchaImageSource(imageSource);
  if (!source) return { text: '', engine: 'none' };

  const base64 = source.startsWith('data:')
    ? source.slice(source.indexOf(',') + 1)
    : source;

  try {
    const raw = await runner.recognize(base64);
    const text = normalizeCaptchaAnswer(raw).toUpperCase();
    if (text.length >= minLength) return { text, engine: 'ddddocr' };
  } catch {
    // 忽略，降级 tesseract
  }

  try {
    const text = (await recognizeCaptchaText(source, {
      whitelist: '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ',
      ...tessOpt,
    })).toUpperCase();
    return { text, engine: 'tesseract' };
  } catch {
    return { text: '', engine: 'none' };
  }
}

export function normalizeCaptchaImageSource(value) {
  if (!value || typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (trimmed.startsWith('data:image/')) return trimmed;
  if (trimmed.startsWith('http://') || trimmed.startsWith('https://')) return trimmed;
  // 纯 base64
  return `data:image/png;base64,${trimmed.replace(/^base64,/, '')}`;
}
