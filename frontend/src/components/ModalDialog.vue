<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
const props = defineProps<{ title: string; busy?: boolean; wide?: boolean }>()
const emit = defineEmits<{ close: [] }>()
function keydown(event: KeyboardEvent) { if (event.key === 'Escape' && !props.busy) emit('close') }
onMounted(() => document.addEventListener('keydown', keydown))
onUnmounted(() => document.removeEventListener('keydown', keydown))
</script>
<template>
  <Teleport to="body">
    <div class="modal-backdrop" role="presentation" @mousedown.self="!busy && $emit('close')">
      <section class="modal-card" :class="{ wide }" role="dialog" aria-modal="true" :aria-label="title">
        <header class="modal-header"><h2>{{ title }}</h2><button class="icon-button" :disabled="busy" aria-label="关闭" @click="$emit('close')">×</button></header>
        <div class="modal-body"><slot /></div>
        <footer v-if="$slots.footer" class="modal-footer"><slot name="footer" /></footer>
      </section>
    </div>
  </Teleport>
</template>
