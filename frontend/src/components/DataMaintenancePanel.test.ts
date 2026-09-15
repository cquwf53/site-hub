import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DataMaintenancePanel from './DataMaintenancePanel.vue'
const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }))
vi.mock('@/api/client', () => ({ api, apiErrorMessage: (error: Error) => error.message }))
const policy = { automatic_backup_enabled: true, backup_interval_hours: 24, retention_enabled: false, backup_keep_count: 7, log_retention_days: 30, screenshot_retention_days: 30, price_history_retention_days: 365 }
const status = { running: false, last_run_at: null, last_success_at: null, last_error: null, next_run_at: null, latest_backup: null, latest_verification: null, last_cleanup: null }
beforeEach(() => { vi.clearAllMocks(); api.get.mockImplementation(path => Promise.resolve(path === '/settings/maintenance' ? { ...policy } : { ...status })); api.put.mockImplementation((_path, value) => Promise.resolve(value)) })
afterEach(() => vi.restoreAllMocks())
describe('maintenance settings', () => {
  it('requires explicit confirmation when enabling cleanup, while preserving retention parameters', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mount(DataMaintenancePanel, { global: { plugins: [createPinia()] } }); await flushPromises()
    await wrapper.findAll('input[type=checkbox]')[1]!.setValue(true)
    await wrapper.get('form').trigger('submit'); await flushPromises(); expect(api.put).not.toHaveBeenCalled()
    confirm.mockReturnValue(true); await wrapper.get('#log-days').setValue(60); await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/settings/maintenance', { ...policy, retention_enabled: true, log_retention_days: 60 })
    wrapper.unmount()
  })
  it('shows verification and cleanup counts from manual maintenance and refreshes the backup list', async () => {
    api.post.mockResolvedValue({ ...status, latest_verification: { backup_id: 'backup-1', verified_at: '2026-09-15T10:00:00Z', integrity_check: 'ok' }, last_cleanup: { backups: 2, logs: 3, screenshots: 4, price_history: 5 } })
    const wrapper = mount(DataMaintenancePanel, { global: { plugins: [createPinia()] } }); await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '立即维护')!.trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/maintenance/run'); expect(wrapper.text()).toContain('备份 2，日志 3，截图 4，价格记录 5'); expect(wrapper.emitted('changed')).toHaveLength(1)
    wrapper.unmount()
  })
})
