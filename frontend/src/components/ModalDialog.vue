<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'

const props = defineProps<{ title: string; busy?: boolean; wide?: boolean }>()
const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLElement | null>(null)
let previouslyFocused: HTMLElement | null = null

const focusableSelector = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function focusableElements() {
  if (!dialog.value) return []
  return [...dialog.value.querySelectorAll<HTMLElement>(focusableSelector)]
    .filter((element) => element.getAttribute('aria-hidden') !== 'true')
}

function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    if (!props.busy) {
      event.preventDefault()
      emit('close')
    }
    return
  }
  if (event.key !== 'Tab') return
  const elements = focusableElements()
  if (!elements.length) {
    event.preventDefault()
    dialog.value?.focus()
    return
  }
  const first = elements[0]
  const last = elements[elements.length - 1]
  const active = document.activeElement
  if (!dialog.value?.contains(active)) {
    event.preventDefault()
    const target = event.shiftKey ? last : first
    target.focus()
  } else if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

onMounted(() => {
  previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
  void nextTick(() => {
    const preferred = dialog.value?.querySelector<HTMLElement>('[autofocus]')
    const target = preferred || focusableElements()[0] || dialog.value
    target?.focus()
  })
})

onUnmounted(() => {
  if (previouslyFocused?.isConnected) previouslyFocused.focus()
})
</script>
<template>
  <Teleport to="body">
    <div class="modal-backdrop" role="presentation" @mousedown.self="!busy && $emit('close')">
      <section ref="dialog" class="modal-card" :class="{ wide }" role="dialog" aria-modal="true" :aria-label="title" tabindex="-1" @keydown="keydown">
        <header class="modal-header"><h2>{{ title }}</h2><button class="icon-button" :disabled="busy" aria-label="关闭" @click="$emit('close')">×</button></header>
        <div class="modal-body"><slot /></div>
        <footer v-if="$slots.footer" class="modal-footer"><slot name="footer" /></footer>
      </section>
    </div>
  </Teleport>
</template>
