export function runGuidance(status: string, reason?: string | null) {
  if (!['failed', 'effect_unknown', 'skipped'].includes(status)) return ''
  if (status === 'effect_unknown') return '先打开网站确认今日是否已签到，再决定是否重试，避免重复执行。'
  const code = (reason || '').toLowerCase()
  if (/auth|login|session|expired|credential/.test(code)) return '打开签到账号页，重新登录并验证登录状态，然后重试。'
  if (/captcha|challenge|verification/.test(code)) return '打开网站完成验证码或安全验证，再到签到账号页验证登录状态。'
  if (/rate|limit|429|throttle/.test(code)) return '等待网站限流解除后再重试；可在调度设置中延长执行间隔。'
  if (/timeout|network|connect|dns/.test(code)) return '检查网络和网站是否可访问，恢复后再重试。'
  if (/profile|busy|lock/.test(code)) return '关闭正在占用该账号的浏览器窗口，再验证 Profile 后重试。'
  if (/catalog|scrap|adapter|config/.test(code)) return '检查网站适配器与抓取配置，确认页面结构未变化，再执行同步。'
  return '查看运行说明并确认网站当前状态；修复原因后从任务中心重试。'
}
