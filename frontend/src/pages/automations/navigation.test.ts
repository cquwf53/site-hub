import { mount, flushPromises } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import JobsPage from './JobsPage.vue'
import RunsPage from './RunsPage.vue'
const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('@/api/client', () => ({ api: mocks, apiErrorMessage: (error: Error) => error.message }))
vi.mock('@/stores/tasks', () => ({ useTaskStore: () => ({ track: vi.fn() }) }))
beforeEach(() => {
  mocks.get.mockReset()
  mocks.get.mockImplementation((_path, query) => Promise.resolve({ page: Number(query.page), page_size: 25, total: 75, items: [{ id: 'record', status: 'failed', site_name: 'Example', scheduled_for: '2026-09-15T10:00:00Z', error_message: 'Session expired' }] }))
})
describe.each([{ name: 'jobs', component: JobsPage }, { name: 'runs', component: RunsPage }])('$name route state', ({ name, component }) => {
  it('loads the new page, resets pagination on filter, and responds to browser navigation', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/'+name, component }, { path: '/automations/runs/:id', component: {} }] })
    await router.push('/'+name+'?page=2')
    const wrapper = mount(component, { global: { plugins: [createPinia(), router] } }); await flushPromises()
    const next = wrapper.findAll('button').find((button) => button.attributes('aria-label') === '下一页' || button.text().includes('下一页'))
    expect(next).toBeDefined(); await next!.trigger('click'); await flushPromises()
    expect(mocks.get).toHaveBeenLastCalledWith('/'+name, expect.objectContaining({ page: '3' }))
    await wrapper.get('select').setValue('failed'); await flushPromises()
    expect(mocks.get).toHaveBeenLastCalledWith('/'+name, expect.objectContaining({ page: 1, status: 'failed' }))
    await router.push('/'+name+'?page=2&status=succeeded'); await flushPromises()
    expect((wrapper.get('select').element as HTMLSelectElement).value).toBe('succeeded')
    if (name === 'jobs') { expect(wrapper.text()).toContain('Session expired'); expect(wrapper.text()).not.toContain('计划时间—') }
    wrapper.unmount()
  })
})
