import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/overview' },
    { path: '/overview', name: 'overview', component: () => import('@/pages/OverviewPage.vue'), meta: { title: '总览' } },
    { path: '/sites', name: 'sites', component: () => import('@/pages/SitesPage.vue'), meta: { title: '网站' } },
    {
      path: '/sites/:siteId',
      component: () => import('@/pages/site/SiteDetailLayout.vue'),
      children: [
        { path: '', redirect: { name: 'site-overview' } },
        { path: 'overview', name: 'site-overview', component: () => import('@/pages/site/SiteOverviewPage.vue'), meta: { title: '网站概览' } },
        { path: 'products', name: 'site-products', component: () => import('@/pages/site/SiteProductsPage.vue'), meta: { title: '网站商品' } },
        { path: 'accounts', name: 'site-accounts', component: () => import('@/pages/site/SiteAccountsPage.vue'), meta: { title: '签到账号' } },
        { path: 'activity', name: 'site-activity', component: () => import('@/pages/site/SiteActivityPage.vue'), meta: { title: '活动记录' } },
        { path: 'settings', name: 'site-settings', component: () => import('@/pages/site/SiteSettingsPage.vue'), meta: { title: '网站设置' } },
      ],
    },
    { path: '/products', name: 'products', component: () => import('@/pages/ProductsPage.vue'), meta: { title: '商品' } },
    { path: '/inbox', name: 'inbox', component: () => import('@/pages/InboxPage.vue'), meta: { title: '收件箱' } },
    {
      path: '/automations',
      component: () => import('@/pages/automations/AutomationsLayout.vue'),
      children: [
        { path: '', redirect: { name: 'automation-jobs' } },
        { path: 'jobs', name: 'automation-jobs', component: () => import('@/pages/automations/JobsPage.vue'), meta: { title: '自动化任务' } },
        { path: 'runs', name: 'automation-runs', component: () => import('@/pages/automations/RunsPage.vue'), meta: { title: '运行记录' } },
        { path: 'runs/:runId', name: 'automation-run', component: () => import('@/pages/automations/RunDetailPage.vue'), meta: { title: '运行详情' } },
      ],
    },
    {
      path: '/settings',
      component: () => import('@/pages/settings/SettingsLayout.vue'),
      children: [
        { path: '', redirect: { name: 'settings-general' } },
        { path: 'general', name: 'settings-general', component: () => import('@/pages/settings/GeneralSettingsPage.vue'), meta: { title: '通用设置' } },
        { path: 'tags', name: 'settings-tags', component: () => import('@/pages/settings/TagsSettingsPage.vue'), meta: { title: '标签设置' } },
        { path: 'notifications', name: 'settings-notifications', component: () => import('@/pages/settings/NotificationsSettingsPage.vue'), meta: { title: '通知设置' } },
        { path: 'adapters', name: 'settings-adapters', component: () => import('@/pages/settings/AdaptersSettingsPage.vue'), meta: { title: '适配器' } },
        { path: 'data', name: 'settings-data', component: () => import('@/pages/settings/DataSettingsPage.vue'), meta: { title: '数据管理' } },
      ],
    },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('@/pages/NotFoundPage.vue'), meta: { title: '页面不存在' } },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => { document.title = `${String(to.meta.title || 'SiteHub')} · SiteHub` })
export default router
