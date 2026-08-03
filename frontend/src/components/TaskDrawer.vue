<script setup lang="ts">
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'
import StatusBadge from './StatusBadge.vue'

const app = useAppStore()
const store = useTaskStore()
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <button v-if="app.taskDrawerOpen" class="drawer-scrim" aria-label="关闭任务抽屉" @click="app.taskDrawerOpen = false" />
    </Transition>
    <Transition name="drawer">
      <aside v-if="app.taskDrawerOpen" class="task-drawer" aria-label="后台任务">
        <div class="drawer-header">
          <div><h2>后台任务</h2><p>{{ store.streamConnected ? '实时更新已连接' : '轮询更新' }}</p></div>
          <button class="icon-button" aria-label="关闭" @click="app.taskDrawerOpen = false">×</button>
        </div>
        <div v-if="!store.tasks.length" class="empty-compact">暂无后台任务</div>
        <div v-else class="task-list">
          <article v-for="task in store.tasks" :key="task.id" class="task-item">
            <div class="row-between"><strong>{{ task.title }}</strong><StatusBadge :status="task.status" /></div>
            <p v-if="task.message">{{ task.message }}</p>
            <div v-if="task.progress !== null" class="progress-track"><span :style="{ width: `${Math.max(0, Math.min(100, task.progress))}%` }" /></div>
            <div class="task-meta"><span>{{ task.kind }}</span><button class="text-button subtle" @click="store.dismiss(task.id)">移除</button></div>
          </article>
        </div>
        <button v-if="store.tasks.length" class="button secondary drawer-clear" @click="store.clearFinished">清除已完成</button>
      </aside>
    </Transition>
  </Teleport>
</template>
