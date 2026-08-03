<script setup lang="ts">
import { ref } from 'vue'
import { api, apiErrorMessage } from '@/api/client'
import type { TaskRef } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'

const app = useAppStore()
const tasks = useTaskStore()
const bookmarkInput = ref<HTMLInputElement | null>(null)

async function importBookmarks(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const data = new FormData()
  data.append('file', file)
  try {
    const task = await api.post<TaskRef>('/imports/bookmarks', data)
    tasks.track(task, '导入浏览器书签')
    app.taskDrawerOpen = true
    app.toast('书签导入已开始', 'success')
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { input.value = '' }
}
</script>

<template>
  <div class="stack">
    <section class="card">
      <div class="card-header"><div><h2>浏览器书签</h2><p class="subtle-text">导入 Chrome、Edge 或 Firefox 导出的 Netscape HTML 文件</p></div></div>
      <div class="data-action">
        <div><h3>保留原始目录层级</h3><p>完整 URL 相同的条目会跳过；同域名下不同路径仍会作为独立网站保留。完成后可在任务详情查看导入和重复报告。</p></div>
        <input ref="bookmarkInput" class="hidden-input" type="file" accept=".html,text/html" @change="importBookmarks" />
        <button class="button secondary" @click="bookmarkInput?.click()">选择书签文件</button>
      </div>
    </section>
    <section class="card info-card">
      <h2>旧项目迁移</h2>
      <p>VShop 与 AutoCheckin 的合并采用独立命令行迁移工具，提供只读分析、完整临时库演练和正式应用三种模式。源数据库与浏览器 Profile 永不原地修改。</p>
      <p class="subtle-text">正式迁移和恢复属于切换操作，需要在两套旧服务停止后执行，因此不会从普通网页按钮直接触发。</p>
    </section>
    <section class="card info-card">
      <h2>备份边界</h2>
      <p>第一版由迁移/运维工具使用 SQLite Backup API 创建一致性快照。涉及 DPAPI 密钥和 Profile 的恢复必须在同一 Windows 用户下验证后进行。</p>
    </section>
  </div>
</template>

<style scoped>
.data-action { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 22px; }
.data-action h3 { margin-bottom: 5px; }
.data-action p, .info-card p { max-width: 760px; margin: 0; font-size: 12px; line-height: 1.7; }
.hidden-input { position: fixed; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.info-card { padding: 20px; }
.info-card h2 { margin-bottom: 8px; }
.info-card p + p { margin-top: 8px; }
@media (max-width: 600px) { .data-action { align-items: stretch; flex-direction: column; } }
</style>
