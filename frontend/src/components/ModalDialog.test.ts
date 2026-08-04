import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import ModalDialog from './ModalDialog.vue'

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ModalDialog focus management', () => {
  it('moves focus inside, traps tab navigation, and restores the opener', async () => {
    const opener = document.createElement('button')
    opener.textContent = '打开弹窗'
    document.body.append(opener)
    opener.focus()

    const wrapper = mount(ModalDialog, {
      attachTo: document.body,
      props: { title: '测试弹窗' },
      slots: {
        default: '<input aria-label="内容输入"><button aria-label="最后操作">完成</button>',
      },
    })
    await nextTick()

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!
    const closeButton = dialog.querySelector<HTMLElement>('[aria-label="关闭"]')!
    const lastButton = dialog.querySelector<HTMLElement>('[aria-label="最后操作"]')!
    expect(document.activeElement).toBe(closeButton)

    lastButton.focus()
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(closeButton)

    closeButton.focus()
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(lastButton)

    wrapper.unmount()
    expect(document.activeElement).toBe(opener)
  })
})
