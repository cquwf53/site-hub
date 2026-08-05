<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { api, apiErrorMessage } from '@/api/client'
import type { OverviewSites, SiteQuickAccess, SiteRecommendationReason } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { formatDate, hostname } from '@/utils/format'

type QuickAccessTab = 'frequent' | 'recent'

const app = useAppStore()
const loading = ref(true)
const error = ref('')
const activeTab = ref<QuickAccessTab>('frequent')
const frequentTabButton = ref<HTMLButtonElement | null>(null)
const recentTabButton = ref<HTMLButtonElement | null>(null)
const pendingIds = ref<Set<string>>(new Set())
const overview = ref<OverviewSites>({ frequent: [], recent: [], has_usage: false })
const visibleSites = computed(() => overview.value[activeTab.value].slice(0, 8))
const emptyMessage = computed(() => activeTab.value === 'recent'
  ? '从 SiteHub 打开网站后，最近访问会显示在这里。'
  : '没有可显示的网站，可在网站设置中恢复已隐藏的网站。')

function activateTab(tab: QuickAccessTab, focus = false) {
  activeTab.value = tab
  if (!focus) return
  void nextTick(() => {
    const target = tab === 'frequent' ? frequentTabButton.value : recentTabButton.value
    target?.focus()
  })
}

function handleTabKeydown(event: KeyboardEvent) {
  let target: QuickAccessTab | null = null
  if (event.key === 'Home') target = 'frequent'
  else if (event.key === 'End') target = 'recent'
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    target = activeTab.value === 'frequent' ? 'recent' : 'frequent'
  }
  if (!target) return
  event.preventDefault()
  activateTab(target, true)
}

function setPending(siteId: string, pending: boolean) {
  const next = new Set(pendingIds.value)
  if (pending) next.add(siteId)
  else next.delete(siteId)
  pendingIds.value = next
}

function reasonLabel(site: SiteQuickAccess) {
  const labels: Record<SiteRecommendationReason, string> = {
    pinned: '已置顶',
    frequent: site.visit_count ? `常用 · ${site.visit_count} 次` : '常用网站',
    recent: site.last_visited_at ? `最近访问 ${formatDate(site.last_visited_at)}` : '最近访问',
    starter: '等待使用习惯',
  }
  return labels[site.recommendation_reason]
}

function recordLocalVisit(openedSite: SiteQuickAccess) {
  const visitedAt = new Date().toISOString()
  const visitCount = (openedSite.visit_count || 0) + 1
  const frequentSite = overview.value.frequent.find((item) => item.id === openedSite.id)
  if (frequentSite) {
    frequentSite.visit_count = visitCount
    frequentSite.last_visited_at = visitedAt
    if (!frequentSite.pinned) frequentSite.recommendation_reason = 'frequent'
  }
  const recentEntry: SiteQuickAccess = {
    ...openedSite,
    visit_count: visitCount,
    last_visited_at: visitedAt,
    recommendation_reason: 'recent',
  }
  overview.value.recent = [
    recentEntry,
    ...overview.value.recent.filter((item) => item.id !== openedSite.id),
  ].slice(0, 8)
  overview.value.has_usage = true
}

async function load(options: { quiet?: boolean } = {}) {
  if (!options.quiet) loading.value = true
  error.value = ''
  try {
    overview.value = await api.get<OverviewSites>('/overview/sites', { limit: 8 })
  } catch (value) {
    error.value = apiErrorMessage(value)
  } finally {
    loading.value = false
  }
}

async function openSite(site: SiteQuickAccess) {
  if (pendingIds.value.has(site.id)) return
  setPending(site.id, true)
  try {
    await api.post<void>(`/sites/${site.id}/open`)
    app.toast(`已打开 ${site.name}`, 'success')
    // Keep the cards in place while the user is interacting. The server will
    // apply the new ranking the next time this panel is loaded.
    recordLocalVisit(site)
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    setPending(site.id, false)
  }
}

async function togglePin(site: SiteQuickAccess) {
  if (pendingIds.value.has(site.id)) return
  setPending(site.id, true)
  try {
    await api.patch(`/sites/${site.id}`, { pinned: !site.pinned })
    app.toast(site.pinned ? '已取消置顶' : '已置顶到快捷访问', 'success')
    await load({ quiet: true })
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    setPending(site.id, false)
  }
}

async function hideSite(site: SiteQuickAccess) {
  if (pendingIds.value.has(site.id)) return
  setPending(site.id, true)
  try {
    await api.patch(`/sites/${site.id}`, { overview_hidden: true })
    app.toast('已从快捷访问隐藏，可在网站设置中恢复', 'success')
    await load({ quiet: true })
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  } finally {
    setPending(site.id, false)
  }
}

async function resetUsage() {
  const confirmed = window.confirm('确定清除所有网站的使用记录吗？置顶和隐藏设置会保留。')
  if (!confirmed) return
  try {
    await api.post<void>('/overview/sites/reset-usage')
    app.toast('使用记录已清除', 'success')
    activeTab.value = 'frequent'
    await load({ quiet: true })
  } catch (value) {
    app.toast(apiErrorMessage(value), 'error')
  }
}

onMounted(load)
</script>

<template>
  <section class="card quick-access" aria-labelledby="quick-access-title">
    <div class="quick-access-header">
      <div>
        <h2 id="quick-access-title">快捷访问</h2>
        <p class="subtle-text">根据最近访问和使用频率自动排序</p>
      </div>
      <div class="quick-access-actions">
        <div class="tabs quick-tabs" role="tablist" aria-label="快捷访问排序方式">
          <button ref="frequentTabButton" id="quick-tab-frequent" type="button" role="tab" :class="{ active: activeTab === 'frequent' }" :aria-selected="activeTab === 'frequent'" :tabindex="activeTab === 'frequent' ? 0 : -1" aria-controls="quick-sites-panel" @click="activateTab('frequent')" @keydown="handleTabKeydown">常用</button>
          <button ref="recentTabButton" id="quick-tab-recent" type="button" role="tab" :class="{ active: activeTab === 'recent' }" :aria-selected="activeTab === 'recent'" :tabindex="activeTab === 'recent' ? 0 : -1" aria-controls="quick-sites-panel" @click="activateTab('recent')" @keydown="handleTabKeydown">最近</button>
        </div>
        <button v-if="overview.has_usage" type="button" class="text-button subtle reset-usage" @click="resetUsage">清除记录</button>
      </div>
    </div>

    <p v-if="!loading && !error && !overview.has_usage" class="learning-note">
      <span aria-hidden="true">✦</span>
      还没有访问记录。这里会根据你通过 SiteHub 打开网站的次数和最近使用时间自动学习，当前先展示置顶和最近添加的网站。
    </p>

    <div v-if="loading" class="quick-state" role="status">正在整理常用网站…</div>
    <div v-else-if="error" class="quick-state quick-error" role="alert">
      <span>快捷访问暂时无法加载：{{ error }}</span>
      <button type="button" class="text-button" @click="load()">重试</button>
    </div>
    <div
      v-else
      id="quick-sites-panel"
      class="quick-sites-panel"
      role="tabpanel"
      :aria-labelledby="activeTab === 'frequent' ? 'quick-tab-frequent' : 'quick-tab-recent'"
    >
      <div v-if="visibleSites.length" class="quick-site-grid">
        <article v-for="site in visibleSites" :key="site.id" class="quick-site-card">
          <button type="button" class="quick-site-main" :disabled="pendingIds.has(site.id)" :aria-label="`打开 ${site.name}`" @click="openSite(site)">
            <span class="site-icon quick-site-icon" aria-hidden="true">{{ site.name.slice(0, 1).toUpperCase() }}</span>
            <span class="quick-site-copy">
              <strong>{{ site.name }}</strong>
              <small>{{ hostname(site.url) }}</small>
            </span>
            <span class="open-mark" aria-hidden="true">↗</span>
          </button>
          <div class="quick-site-meta">
            <span>{{ reasonLabel(site) }}</span>
            <div class="quick-site-controls">
              <RouterLink class="quick-control" :to="`/sites/${site.id}/overview`" :aria-label="`查看 ${site.name} 详情`" title="网站详情">详情</RouterLink>
              <button type="button" class="quick-control icon-control" :class="{ pinned: site.pinned }" :disabled="pendingIds.has(site.id)" :aria-label="site.pinned ? `取消置顶 ${site.name}` : `置顶 ${site.name}`" :title="site.pinned ? '取消置顶' : '置顶'" @click="togglePin(site)">★</button>
              <button type="button" class="quick-control icon-control" :disabled="pendingIds.has(site.id)" :aria-label="`从快捷访问隐藏 ${site.name}`" title="从快捷访问隐藏" @click="hideSite(site)">×</button>
            </div>
          </div>
        </article>
      </div>
      <div v-else class="quick-state">{{ emptyMessage }}</div>
    </div>
  </section>
</template>

<style scoped>
.quick-access { margin-bottom: 17px; overflow: hidden; }
.quick-access-header { display: flex; min-height: 70px; align-items: center; justify-content: space-between; gap: 20px; padding: 13px 20px 0; border-bottom: 1px solid var(--line); }
.quick-access-header h2, .quick-access-header p { margin: 0; }
.quick-access-header p { margin-top: 3px; }
.quick-access-actions { display: flex; align-self: stretch; align-items: center; gap: 14px; }
.quick-tabs { align-self: stretch; border-bottom: 0; }
.quick-tabs button { height: 100%; padding-right: 17px; padding-left: 17px; }
.reset-usage { white-space: nowrap; }
.learning-note { display: flex; align-items: flex-start; gap: 8px; margin: 0; padding: 10px 20px; border-bottom: 1px solid #e1e4ff; color: #5f68a6; background: #f7f8ff; font-size: 12px; line-height: 1.55; }
.learning-note span { color: var(--accent); }
.quick-site-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; background: var(--line); }
.quick-site-card { min-width: 0; background: var(--surface); }
.quick-site-main { display: flex; width: 100%; min-height: 82px; align-items: center; gap: 11px; padding: 15px 14px 10px; border: 0; background: transparent; cursor: pointer; text-align: left; }
.quick-site-main:hover:not(:disabled) { background: var(--surface-soft); }
.quick-site-main:focus-visible, .quick-control:focus-visible { position: relative; z-index: 1; outline: 2px solid var(--accent); outline-offset: -2px; }
.quick-site-main:disabled { opacity: .55; cursor: wait; }
.quick-site-icon { width: 38px; height: 38px; }
.quick-site-copy { min-width: 0; flex: 1; }
.quick-site-copy strong, .quick-site-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.quick-site-copy strong { font-size: 13px; }
.quick-site-copy small { margin-top: 4px; color: var(--subtle); font-size: 11px; }
.open-mark { color: var(--subtle); font-size: 14px; }
.quick-site-meta { display: flex; min-height: 35px; align-items: center; justify-content: space-between; gap: 8px; padding: 0 9px 7px 14px; color: var(--subtle); font-size: 10px; }
.quick-site-controls { display: flex; align-items: center; }
.quick-control { display: inline-grid; min-width: 28px; min-height: 27px; place-items: center; padding: 0 5px; border: 0; border-radius: 6px; color: var(--muted); background: transparent; cursor: pointer; font-size: 10px; }
.quick-control:hover:not(:disabled) { color: var(--accent); background: var(--accent-soft); }
.quick-control.icon-control { font-size: 15px; }
.quick-control.pinned { color: #e7a839; }
.quick-control:disabled { opacity: .45; cursor: wait; }
.quick-state { display: flex; min-height: 117px; align-items: center; justify-content: center; gap: 10px; padding: 22px; color: var(--muted); text-align: center; font-size: 13px; }
.quick-error { color: var(--negative); }
@media (max-width: 1150px) {
  .quick-site-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
  .quick-access-header { align-items: flex-start; flex-direction: column; gap: 7px; padding-top: 16px; }
  .quick-access-actions { width: 100%; min-height: 45px; justify-content: space-between; }
  .quick-tabs { align-self: stretch; }
  .quick-site-grid { grid-template-columns: 1fr; }
}
</style>
