import { describe, expect, it } from 'vitest'
import { accountSchedulePayload, SITEHUB_TIMEZONE } from './accountSchedule'

describe('accountSchedulePayload', () => {
  it('explicitly clears an account override when inheriting the global batch', () => {
    expect(accountSchedulePayload({
      schedule_mode: 'inherit',
      fixed_time: '08:30',
      window_start: '08:00',
      window_end: '22:00',
    })).toEqual({ schedule_override: null, clear_schedule_override: true })
  })

  it('serializes a fixed time in the SiteHub business timezone', () => {
    expect(accountSchedulePayload({
      schedule_mode: 'fixed_time',
      fixed_time: '09:15',
      window_start: '08:00',
      window_end: '22:00',
    })).toEqual({
      schedule_override: { mode: 'fixed_time', time: '09:15', timezone: SITEHUB_TIMEZONE },
      clear_schedule_override: false,
    })
  })

  it('serializes a custom daily window', () => {
    expect(accountSchedulePayload({
      schedule_mode: 'custom_window',
      fixed_time: '09:15',
      window_start: '10:00',
      window_end: '12:30',
    })).toEqual({
      schedule_override: { mode: 'custom_window', start: '10:00', end: '12:30', timezone: SITEHUB_TIMEZONE },
      clear_schedule_override: false,
    })
  })
})
