/**
 * API 模式签到器
 * 直接发送 HTTP 请求完成签到（最快，资源占用最低）
 *
 * site.config 需要包含：
 *   method: 'GET' | 'POST' (默认 POST)
 *   checkinUrl: 签到 API 地址
 *   headers: 自定义请求头 (object)
 *   body: POST 请求体 (object/string)
 *   successKeyword: 响应中的成功关键词 (默认 'success')
 */

import {
  assessBusinessSuccess,
  fetchTextWithLimits,
  isAlreadyDone,
  parseResponsePayload,
} from './http-utils.js';

export async function apiCheckin(site) {
  const config = site.config || {};
  const checkinUrl = config.checkinUrl || site.url;
  const method = (config.method || 'POST').toUpperCase();
  const headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    ...(config.headers || {}),
  };

  const fetchOptions = { method, headers };

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
    const { response, text } = await fetchTextWithLimits(checkinUrl, fetchOptions, {
      timeoutMs: config.timeoutMs,
      maxBytes: config.maxResponseBytes,
      maxRedirects: config.maxRedirects,
    });
    const duration = Date.now() - startTime;
    const data = parseResponsePayload(text);

    const successKeyword = config.successKeyword || 'success';
    const responseStr = typeof data === 'string' ? data : JSON.stringify(data);
    if (response.ok && isAlreadyDone(data, responseStr)) {
      return {
        status: 'skipped',
        message: '今日已签到，无需重复执行',
        duration_ms: duration,
      };
    }
    const isSuccess = response.ok && assessBusinessSuccess(data, responseStr, successKeyword);

    return {
      status: isSuccess ? 'success' : 'failed',
      message: isSuccess
        ? `API 签到成功 (${response.status})`
        : `API 签到失败: ${response.status} - ${responseStr.substring(0, 200)}`,
      duration_ms: duration,
    };
  } catch (err) {
    return {
      status: 'failed',
      message: `API 请求异常: ${err.message}`,
      duration_ms: Date.now() - startTime,
    };
  }
}
