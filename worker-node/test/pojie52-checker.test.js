import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-checkin-52pojie-'));
process.env.SITEHUB_DATA_DIR = tempDir;
process.env.SITEHUB_PROFILE_DIR = path.join(tempDir, 'profiles');

const {
  is52PojieSite,
  cleanTaskMessage,
  classifyTaskMessage,
  extractResultDetail,
} = await import(
  '../src/checkin/pojie52-checker.js'
);


test('52pojie adapter only handles official 52pojie hostnames', () => {
  assert.equal(is52PojieSite({ url: 'https://www.52pojie.cn/' }), true);
  assert.equal(is52PojieSite({ url: 'https://52pojie.cn/home.php' }), true);
  assert.equal(is52PojieSite({ url: 'https://pt.btschool.club/' }), false);
  assert.equal(is52PojieSite({ url: 'not-a-url' }), false);
});

test('52pojie task messages strip Discuz redirect boilerplate', () => {
  const success = '恭喜您，任务已完成，关注论坛微信“吾爱破解论坛”，'
    + '一键签到送论坛币。 如果您的浏览器没有自动跳转，请点击此链接';
  assert.equal(
    cleanTaskMessage(success),
    '恭喜您，任务已完成，关注论坛微信“吾爱破解论坛”，一键签到送论坛币',
  );
  assert.equal(cleanTaskMessage(''), '');
});

test('52pojie task messages classify success vs already-signed', () => {
  const success = '恭喜您，任务已完成，关注论坛微信“吾爱破解论坛”，一键签到送论坛币';
  assert.equal(classifyTaskMessage(success), 'success');
  assert.equal(
    classifyTaskMessage('抱歉，您已经申请过此任务，请等待任务时间结束后再申请'),
    'already',
  );
  assert.equal(classifyTaskMessage('您需要先登录才能继续本操作'), '');
});

test('52pojie result detail keeps the essential phrase only', () => {
  const success = '恭喜您，任务已完成，关注论坛微信“吾爱破解论坛”，'
    + '一键签到送论坛币。 如果您的浏览器没有自动跳转，请点击此链接';
  assert.equal(extractResultDetail(success), '任务已完成');
  assert.equal(
    extractResultDetail('抱歉，您已经申请过此任务，请等待任务时间结束后再申请'),
    '已经申请过此任务',
  );
  assert.equal(extractResultDetail(''), '');
});

test.after(() => {

  fs.rmSync(tempDir, { recursive: true, force: true });
});
