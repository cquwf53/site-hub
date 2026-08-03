import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import DynamicSchemaForm from './DynamicSchemaForm.vue'

const schema = {
  type: 'object' as const,
  required: ['username', 'password'],
  properties: {
    username: { type: 'string' as const, title: '用户名' },
    password: { type: 'string' as const, title: '密码' },
    headless: { type: 'boolean' as const, title: '无头模式' },
  },
}

describe('DynamicSchemaForm', () => {
  it('does not echo a configured secret and emits an explicit clear request', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(DynamicSchemaForm, { props: { schema, modelValue: { username: 'alice', unknown_adapter_key: 7 }, secretFields: ['password'], configuredSecretFields: ['password'], clearSecrets: [] } })
    const password = wrapper.get('input[type="password"]')
    expect((password.element as HTMLInputElement).value).toBe('')
    expect(password.attributes('placeholder')).toContain('已配置')

    await wrapper.findAll('button').find((button) => button.text() === '清除')?.trigger('click')
    expect(confirm).toHaveBeenCalledOnce()
    expect(wrapper.emitted('update:clearSecrets')?.[0]).toEqual([['password']])
    confirm.mockRestore()
  })

  it('does not clear a configured secret when confirmation is rejected', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mount(DynamicSchemaForm, { props: { schema, modelValue: {}, secretFields: ['password'], configuredSecretFields: ['password'], clearSecrets: [] } })
    await wrapper.findAll('button').find((button) => button.text() === '清除')?.trigger('click')
    expect(wrapper.emitted('update:clearSecrets')).toBeUndefined()
    confirm.mockRestore()
  })

  it('treats an automatically protected configured URL as a secret on later edits', () => {
    const wrapper = mount(DynamicSchemaForm, {
      props: {
        schema: { type: 'object', properties: { callback_url: { type: 'string', format: 'uri' } } },
        modelValue: {},
        configuredSecretFields: ['callback_url'],
      },
    })
    const input = wrapper.get('input')
    expect(input.attributes('type')).toBe('password')
    expect(input.attributes('placeholder')).toContain('已配置')
    expect((input.element as HTMLInputElement).value).toBe('')
  })

  it('preserves fields unknown to the manifest when a known field changes', async () => {
    const wrapper = mount(DynamicSchemaForm, { props: { schema, modelValue: { username: 'alice', unknown_adapter_key: 7 } } })
    await wrapper.get('#schema-username').setValue('bob')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toEqual({ username: 'bob', unknown_adapter_key: 7 })
  })
})
