/**
 * ddddocr 识别器 —— 通过常驻 Python 子进程调用 ddddocr 模型。
 *
 * ddddocr 是针对通用字符验证码训练的 CRNN 模型，对 jianzhile.vip 这类
 * 彩色扭曲验证码的识别率远高于 tesseract。它是 Python 库，这里用一个
 * 常驻子进程（stdin/stdout 行协议）承载，Node 端串行发图、按 FIFO 收结果。
 *
 * 环境变量：
 *   CHECKIN_PYTHON           Python 可执行文件，默认 "python"
 *   CHECKIN_DISABLE_DDDDOCR  设为 "1" 时禁用（强制上层降级到 tesseract）
 *
 * 任何不可用情形（未装 Python / 未装 ddddocr / 启动超时 / 进程崩溃 / 识别超时）
 * 都返回 null，由调用方降级到 tesseract，不会抛出中断签到流程。
 */

import { spawn } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import { trackChildProcess } from '../process-registry.js';

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
const PY_SCRIPT = path.join(MODULE_DIR, 'ddddocr_cli.py');
const PYTHON = process.env.SITEHUB_PYTHON || process.env.CHECKIN_PYTHON || 'python';
const STARTUP_TIMEOUT_MS = 30000;
const REQUEST_TIMEOUT_MS = 20000;

class DdddocrRunner {
  constructor() {
    this.proc = null;
    this.startup = null; // Promise<boolean>
    this.handshakeDone = false;
    this.queue = []; // FIFO: { resolve, timer }
    this.stdoutBuffer = '';
    this.disabled = process.env.SITEHUB_DISABLE_DDDDOCR === '1'
      || process.env.CHECKIN_DISABLE_DDDDOCR === '1';
  }

  ensureStarted() {
    if (this.disabled) return Promise.resolve(false);
    if (this.startup) return this.startup;

    this.startup = new Promise(resolve => {
      let settled = false;
      const settle = value => {
        if (settled) return;
        settled = true;
        resolve(value);
      };

      let proc;
      try {
        proc = trackChildProcess(
          spawn(PYTHON, [PY_SCRIPT], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true }),
          'ddddocr',
        );
      } catch {
        settle(false);
        return;
      }

      this.proc = proc;
      this.handshakeDone = false;
      this.stdoutBuffer = '';

      const startupTimer = setTimeout(() => {
        settle(false);
        this._reset();
      }, STARTUP_TIMEOUT_MS);
      startupTimer.unref?.();

      proc.stdout.setEncoding('utf8');
      proc.stdout.on('data', chunk => {
        this.stdoutBuffer += chunk;
        let newlineIndex;
        while ((newlineIndex = this.stdoutBuffer.indexOf('\n')) >= 0) {
          const line = this.stdoutBuffer.slice(0, newlineIndex).trim();
          this.stdoutBuffer = this.stdoutBuffer.slice(newlineIndex + 1);
          if (!line) continue;

          let payload;
          try {
            payload = JSON.parse(line);
          } catch {
            continue; // 忽略非协议输出
          }

          if (!this.handshakeDone) {
            this.handshakeDone = true;
            clearTimeout(startupTimer);
            if (payload.ready === true) {
              settle(true);
            } else {
              settle(false);
              this._reset();
            }
            continue;
          }

          this._resolveNext(payload);
        }
      });

      proc.stderr.on('data', () => {});

      proc.on('error', () => {
        clearTimeout(startupTimer);
        settle(false);
        this._failAll();
        this._reset();
      });

      proc.on('exit', () => {
        clearTimeout(startupTimer);
        settle(false);
        this._failAll();
        this._reset();
      });
    });

    return this.startup;
  }

  _resolveNext(payload) {
    const job = this.queue.shift();
    if (!job) return;
    clearTimeout(job.timer);
    if (payload.ok && typeof payload.text === 'string') {
      job.resolve(payload.text);
    } else {
      job.resolve(null);
    }
  }

  _failAll() {
    while (this.queue.length) {
      const job = this.queue.shift();
      clearTimeout(job.timer);
      job.resolve(null);
    }
  }

  _reset() {
    if (this.proc) {
      try {
        this.proc.kill();
      } catch {
        // ignore
      }
    }
    this.proc = null;
    this.startup = null;
    this.handshakeDone = false;
    this.stdoutBuffer = '';
  }

  /**
   * 识别一张验证码。
   * @param {string} base64 纯 base64（不含 data: 前缀）
   * @returns {Promise<string|null>} 识别文本；不可用/失败/超时返回 null
   */
  async recognize(base64) {
    if (!base64) return null;
    const ready = await this.ensureStarted();
    if (!ready || !this.proc) return null;

    return new Promise(resolve => {
      const timer = setTimeout(() => {
        // 超时意味着响应错位风险，直接重置进程，下次惰性重启
        this._failAll();
        this._reset();
        resolve(null);
      }, REQUEST_TIMEOUT_MS);
      timer.unref?.();

      this.queue.push({ resolve, timer });

      try {
        this.proc.stdin.write(`${base64}\n`);
      } catch {
        clearTimeout(timer);
        const index = this.queue.findIndex(job => job.timer === timer);
        if (index >= 0) this.queue.splice(index, 1);
        resolve(null);
      }
    });
  }

  shutdown() {
    if (this.proc) {
      try {
        this.proc.stdin.write('__EXIT__\n');
      } catch {
        // ignore
      }
    }
    this._reset();
  }
}

export const ddddocrRunner = new DdddocrRunner();
export { DdddocrRunner };
