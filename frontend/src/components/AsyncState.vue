<script setup lang="ts">
defineProps<{ loading?: boolean; error?: string; empty?: boolean; emptyTitle?: string; emptyText?: string }>()
defineEmits<{ retry: [] }>()
</script>
<template>
  <div v-if="loading" class="state-panel" aria-busy="true"><span class="spinner" /><p>正在加载…</p></div>
  <div v-else-if="error" class="state-panel error-state" role="alert"><span class="state-icon">!</span><h3>加载失败</h3><p>{{ error }}</p><button class="button secondary" @click="$emit('retry')">重试</button></div>
  <div v-else-if="empty" class="state-panel"><span class="state-icon">＋</span><h3>{{ emptyTitle || '暂无内容' }}</h3><p>{{ emptyText }}</p><slot name="empty-action" /></div>
  <slot v-else />
</template>
