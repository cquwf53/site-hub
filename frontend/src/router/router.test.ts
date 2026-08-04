import { describe, expect, it } from 'vitest'
import router from './index'

describe('router', () => {
  it('contains every product route in the unified information architecture', () => {
    const names = new Set(router.getRoutes().map((route) => route.name))
    ;['overview', 'sites', 'site-overview', 'site-products', 'site-accounts', 'site-activity', 'site-settings', 'products', 'inbox', 'automation-jobs', 'automation-runs', 'settings-general', 'settings-site-types', 'settings-tags', 'settings-notifications', 'settings-adapters', 'settings-data'].forEach((name) => expect(names.has(name)).toBe(true))
  })
})
