import path from 'node:path';
import { profileDirFor } from './runtime-paths.js';

const owners = new Map();

function profileKey(accountId) {
  const resolved = path.resolve(profileDirFor(String(accountId)));
  return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
}

export function acquireProfile(accountId, owner) {
  const key = profileKey(accountId);
  if (owners.has(key)) {
    const error = new Error('该 Profile 正在用于登录、验证或签到，请等待操作完成或关闭登录窗口后重试');
    error.code = 'PROFILE_BUSY';
    throw error;
  }
  const token = Symbol(owner);
  owners.set(key, token);
  return () => {
    if (owners.get(key) === token) owners.delete(key);
  };
}
