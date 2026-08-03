/**
 * Cookie 模式签到器
 * 带 Cookie 发送 HTTP 请求完成签到
 *
 * site.config 需要包含：
 *   cookie: Cookie 字符串
 *   checkinUrl: 签到 API 地址（默认用 site.url）
 *   method: 'GET' | 'POST' (默认 POST)
 *   headers: 额外请求头
 *   body: POST 请求体
 *   successKeyword: 成功关键词 (默认 'success')
 *   csrfSelector: CSRF token 的获取方式（可选）
 */

import {
  assessBusinessSuccess,
  fetchTextWithLimits,
  isAlreadyDone,
  parseResponsePayload,
} from './http-utils.js';

export async function cookieCheckin(site) {
  const config = site.config || {};
  const cookie = config.cookie || '';
  const checkinUrl = config.checkinUrl || site.url;
  const method = (config.method || 'POST').toUpperCase();

  if (!cookie) {
    return {
      status: 'failed',
      message: 'Cookie 未配置，请在站点设置中填写 Cookie',
      duration_ms: 0,
    };
  }

  const headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/html, */*',
    'Cookie': cookie,
    'Referer': site.url,
    ...(config.headers || {}),
  };

  const fetchOptions = { method, headers, redirect: 'follow' };

  if (method === 'POST' && config.body) {
    if (typeof config.body === 'object') {
      fetchOptions.body = JSON.stringify(config.body);
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
    } else {
      fetchOptions.body = config.body;
    }
  }

  const startTime = Date.now();

  try {
    // If CSRF token is needed, fetch main page first
    if (config.csrfSelector) {
      const { text: pageText } = await fetchTextWithLimits(site.url, {
        headers: { 'Cookie': cookie, 'User-Agent': headers['User-Agent'] },
      }, {
        timeoutMs: config.timeoutMs,
        maxBytes: config.maxResponseBytes,
        maxRedirects: config.maxRedirects,
      });

      // Simple regex to extract CSRF token from meta tag or hidden input
      const csrfMatch = pageText.match(
        /name=["']?(?:csrf[_-]?token|_token|authenticity_token)["']?\s+(?:content|value)=["']([^"']+)["']/i
      ) || pageText.match(
        /(?:content|value)=["']([^"']+)["']\s+name=["']?(?:csrf[_-]?token|_token|authenticity_token)["']?/i
      );

      if (csrfMatch) {
        const csrfToken = csrfMatch[1];
        headers['X-CSRF-Token'] = csrfToken;
        if (config.body && typeof config.body === 'object') {
          config.body._token = csrfToken;
          fetchOptions.body = JSON.stringify(config.body);
        }
      }
    }

    const { response, text } = await fetchTextWithLimits(checkinUrl, fetchOptions, {
      timeoutMs: config.timeoutMs,
      maxBytes: config.maxResponseBytes,
      maxRedirects: config.maxRedirects,
    });
    const duration = Date.now() - startTime;

    // Check for login redirect (cookie expired)
    if (/\/(?:sign-in|signin|login)(?:[/?#]|$)/i.test(response.url)) {
      return {
        status: 'failed',
        message: 'Cookie 已过期，请更新 Cookie',
        duration_ms: duration,
      };
    }

    const data = parseResponsePayload(text);

    const successKeyword = config.successKeyword || 'success';
    const responseStr = typeof data === 'string' ? data : JSON.stringify(data);
    const isSuccess = response.ok && assessBusinessSuccess(data, responseStr, successKeyword);

    // Also check for "已签" or "already" patterns
    const alreadyChecked = isAlreadyDone(data, responseStr);

    if (alreadyChecked) {
      return {
        status: 'skipped',
        message: '今日已签到，无需重复',
        duration_ms: duration,
      };
    }

    return {
      status: isSuccess ? 'success' : 'failed',
      message: isSuccess
        ? `Cookie 签到成功 (${response.status})`
        : `Cookie 签到失败: ${response.status} - ${responseStr.substring(0, 200)}`,
      duration_ms: duration,
    };
  } catch (err) {
    return {
      status: 'failed',
      message: `Cookie 请求异常: ${err.message}`,
      duration_ms: Date.now() - startTime,
    };
  }
}
