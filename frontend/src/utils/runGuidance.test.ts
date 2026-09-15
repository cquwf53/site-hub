import { describe, expect, it } from 'vitest'
import { runGuidance } from './runGuidance'
describe('failure recovery guidance', () => {
  it('asks users to verify an uncertain effect before repeating a check-in', () => {
    expect(runGuidance('effect_unknown', 'TIMEOUT')).toContain('确认今日是否已签到')
    expect(runGuidance('failed', 'AUTH_EXPIRED')).toContain('重新登录')
    expect(runGuidance('failed', 'CAPTCHA_REQUIRED')).toContain('验证码')
    expect(runGuidance('failed', 'RATE_LIMIT')).toContain('限流')
    expect(runGuidance('succeeded', 'AUTH_EXPIRED')).toBe('')
  })
})
