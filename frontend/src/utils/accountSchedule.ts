import type { AccountSchedule } from '@/api/types'

export const SITEHUB_TIMEZONE = 'Asia/Shanghai' as const

export type AccountScheduleForm = {
  schedule_mode: 'inherit' | 'fixed_time' | 'custom_window'
  fixed_time: string
  window_start: string
  window_end: string
}

export function accountSchedulePayload(form: AccountScheduleForm): {
  schedule_override: AccountSchedule | null
  clear_schedule_override: boolean
} {
  if (form.schedule_mode === 'inherit') {
    return { schedule_override: null, clear_schedule_override: true }
  }

  if (form.schedule_mode === 'fixed_time') {
    return {
      schedule_override: {
        mode: 'fixed_time',
        time: form.fixed_time,
        timezone: SITEHUB_TIMEZONE,
      },
      clear_schedule_override: false,
    }
  }

  return {
    schedule_override: {
      mode: 'custom_window',
      start: form.window_start,
      end: form.window_end,
      timezone: SITEHUB_TIMEZONE,
    },
    clear_schedule_override: false,
  }
}
