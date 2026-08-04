import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import DataSettingsPage from './DataSettingsPage.vue'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  api: {
    ...apiMocks,
    put: vi.fn(),
    patch: vi.fn(),
  },
  apiErrorMessage: (error: unknown) => error instanceof Error ? error.message : 'request failed',
}))

const backupId = '20260804T010203123456Z-abcdef123456'
const record = {
  backup_id: backupId,
  created_at: '2026-08-04T01:02:03.123456Z',
  kind: 'manual',
  label: 'Known good',
  database_file: 'site-hub.db',
  database_size_bytes: 1048576,
  database_sha256: 'a'.repeat(64),
}

function installResponses() {
  apiMocks.get.mockResolvedValue({
    items: [{ backup_id: backupId, status: 'available', record }],
    total: 1,
    pending: null,
  })
  apiMocks.post.mockImplementation((path: string) => {
    if (path === '/backups') return Promise.resolve(record)
    if (path.endsWith('/verify')) return Promise.resolve({ record, integrity_check: 'ok' })
    if (path.endsWith('/restore')) {
      return Promise.resolve({
        pending: { backup_id: backupId, staged_at: record.created_at, database_sha256: record.database_sha256 },
        restart_required: true,
      })
    }
    throw new Error(`unexpected POST ${path}`)
  })
  apiMocks.delete.mockResolvedValue(undefined)
}

async function renderPage() {
  const wrapper = mount(DataSettingsPage, {
    global: { plugins: [createPinia()] },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  installResponses()
})

describe('DataSettingsPage backups', () => {
  it('creates and verifies a database backup', async () => {
    const wrapper = await renderPage()
    expect(wrapper.text()).toContain('Known good')
    expect(wrapper.text()).toContain('1.0 MB')

    await wrapper.get('input[aria-label="备份备注"]').setValue('Before upgrade')
    await wrapper.get('[data-testid="create-backup"]').trigger('click')
    await flushPromises()
    expect(apiMocks.post).toHaveBeenCalledWith('/backups', { label: 'Before upgrade' })

    await wrapper.get(`[data-testid="verify-backup-${backupId}"]`).trigger('click')
    await flushPromises()
    expect(apiMocks.post).toHaveBeenCalledWith(`/backups/${backupId}/verify`)
  })

  it('marks a backup invalid locally when verification fails', async () => {
    const wrapper = await renderPage()
    apiMocks.post.mockRejectedValueOnce(new Error('backup digest mismatch'))

    const verifyButton = wrapper.get(`[data-testid="verify-backup-${backupId}"]`)
    await verifyButton.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('无效')
    expect(wrapper.text()).toContain('backup digest mismatch')
    expect(verifyButton.attributes('disabled')).toBeDefined()
    expect(wrapper.get(`[data-testid="restore-backup-${backupId}"]`).attributes('disabled')).toBeDefined()
  })

  it('requires the full backup id before staging an offline restore', async () => {
    const wrapper = await renderPage()
    await wrapper.get(`[data-testid="restore-backup-${backupId}"]`).trigger('click')

    const confirmation = wrapper.get('[data-testid="restore-confirmation"]')
    const submit = wrapper.get('[data-testid="stage-restore"]')
    expect(submit.attributes('disabled')).toBeDefined()
    await confirmation.setValue('wrong')
    expect(submit.attributes('disabled')).toBeDefined()
    await confirmation.setValue(backupId)
    expect(submit.attributes('disabled')).toBeUndefined()
    await submit.trigger('click')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith(`/backups/${backupId}/restore`, {
      confirmation: backupId,
    })
  })
})
