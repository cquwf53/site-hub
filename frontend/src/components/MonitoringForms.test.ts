import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MonitorPolicyForm from './MonitorPolicyForm.vue'
import ProductWatchEditor from './ProductWatchEditor.vue'
const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }))
vi.mock('@/api/client', () => ({ api, apiErrorMessage: (error: Error) => error.message }))
const policy = { watched_only: false, price_drop_amount: 5, price_drop_percent: 10, stock_confirmations: 2, notification_cooldown_minutes: 60, catalog_drop_threshold_percent: 70, catalog_minimum_size: 10 }
beforeEach(() => { vi.clearAllMocks(); api.put.mockImplementation((_path, value) => Promise.resolve(value)) })
describe('monitoring configuration', () => {
  it('saves the complete site policy including stock confirmation and catalog protection', async () => {
    api.get.mockResolvedValue({ ...policy })
    const wrapper = mount(MonitorPolicyForm, { props: { siteId: 'site-1' }, global: { plugins: [createPinia()] } }); await flushPromises()
    await wrapper.get('#policy-stock').setValue(3); await wrapper.get('#policy-cooldown').setValue(120)
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/sites/site-1/monitor-policy', { ...policy, stock_confirmations: 3, notification_cooldown_minutes: 120 })
    wrapper.unmount()
  })
  it('keeps null inheritance distinct from an explicit zero single-product threshold', async () => {
    api.get.mockResolvedValue({ watched: true, price_drop_amount: 5, price_drop_percent: 20 })
    const wrapper = mount(ProductWatchEditor, { props: { productId: 'product-1' }, global: { plugins: [createPinia()] } }); await flushPromises()
    await wrapper.get('#watch-amount').setValue(''); await wrapper.get('#watch-percent').setValue('0')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/products/product-1/watch-rule', { watched: true, price_drop_amount: null, price_drop_percent: 0 })
    expect(wrapper.emitted('updated')?.[0]?.[0]).toMatchObject({ watched: true })
    wrapper.unmount()
  })
  it('does not offer default settings as though they loaded successfully after an API failure', async () => {
    api.get.mockRejectedValue(new Error('monitor policy offline'))
    const wrapper = mount(MonitorPolicyForm, { props: { siteId: 'site-1' }, global: { plugins: [createPinia()] } }); await flushPromises()
    expect(wrapper.text()).toContain('monitor policy offline'); expect(wrapper.find('form').exists()).toBe(false)
    wrapper.unmount()
  })
})
