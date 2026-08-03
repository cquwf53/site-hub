<script setup lang="ts">
import { onMounted } from 'vue'
import AppShell from '@/components/AppShell.vue'
import ToastStack from '@/components/ToastStack.vue'
import { useAppStore } from '@/stores/app'

const app = useAppStore()
onMounted(() => void app.bootstrap())
</script>

<template>
  <AppShell />
  <ToastStack />
</template>
