<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import TaskDrawer from './TaskDrawer.vue'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'

const app = useAppStore()
const tasks = useTaskStore()
const route = useRoute()
const navigation = [
  { to: '/overview', label: '总览', glyph: '⌂' },
  { to: '/sites', label: '网站', glyph: '◫' },
  { to: '/products', label: '商品', glyph: '◇' },
  { to: '/inbox', label: '收件箱', glyph: '✉' },
  { to: '/automations/jobs', label: '自动化', glyph: '↻' },
  { to: '/settings/general', label: '设置', glyph: '⚙' },
]
const title = computed(() => String(route.meta.title || 'SiteHub'))
onMounted(() => tasks.connect())
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ open: app.mobileNavOpen }" aria-label="主导航">
      <div class="brand"><span class="brand-mark">S</span><span>SiteHub</span></div>
      <nav class="primary-nav">
        <RouterLink v-for="item in navigation" :key="item.to" :to="item.to" @click="app.mobileNavOpen = false">
          <span class="nav-glyph" aria-hidden="true">{{ item.glyph }}</span><span>{{ item.label }}</span>
        </RouterLink>
      </nav>
      <div class="sidebar-foot">
        <span class="presence-dot" :class="{ online: app.workerOnline }" />
        <span>{{ app.workerOnline ? 'Worker 已连接' : 'Worker 未连接' }}</span>
      </div>
    </aside>
    <button v-if="app.mobileNavOpen" class="nav-scrim" aria-label="关闭导航" @click="app.mobileNavOpen = false" />

    <div class="workspace">
      <header class="topbar">
        <button class="icon-button menu-button" aria-label="打开导航" @click="app.mobileNavOpen = true">☰</button>
        <div class="topbar-title">{{ title }}</div>
        <div class="topbar-spacer" />
        <button class="task-button" @click="app.taskDrawerOpen = true">
          <span class="presence-dot" :class="{ online: tasks.streamConnected }" />
          任务
          <span v-if="tasks.activeCount" class="count-badge">{{ tasks.activeCount }}</span>
        </button>
      </header>
      <div v-if="app.bootError" class="global-banner error-banner" role="alert">
        <span>无法连接 SiteHub：{{ app.bootError }}</span>
        <button class="text-button" @click="app.bootstrap(true)">重试</button>
      </div>
      <main class="page-surface"><RouterView /></main>
    </div>
    <TaskDrawer />
  </div>
</template>
