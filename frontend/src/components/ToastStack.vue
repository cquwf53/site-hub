<script setup lang="ts">
import { useAppStore } from '@/stores/app'
const app = useAppStore()
</script>

<template>
  <Teleport to="body">
    <div class="toast-stack" aria-live="polite">
      <TransitionGroup name="toast">
        <button v-for="toast in app.toasts" :key="toast.id" class="toast" :class="toast.tone" @click="app.removeToast(toast.id)">
          <span>{{ toast.tone === 'success' ? '✓' : toast.tone === 'error' ? '!' : 'i' }}</span>{{ toast.message }}
        </button>
      </TransitionGroup>
    </div>
  </Teleport>
</template>
