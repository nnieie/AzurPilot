import { describe, expect, it } from 'vitest'
import { INSTANCE_NAME_PATTERN } from './instanceName'

// 浏览器按 v 标志编译 HTML pattern 属性：编译失败时属性会被整条忽略，
// 校验会静默退化。这里用同一标志编译，确保规则真的生效。
const rule = new RegExp(`^(?:${INSTANCE_NAME_PATTERN})$`, 'v')

const accepted = ['测试', '测试实例', 'alas测试', '測試', '测试-2', 'a', 'A1_b-c', 'x'.repeat(64)]
const rejected = ['1测试', '-测试', '测试/实例', '测试\\实例', '测试.1', '测 试', '测试#1', '', 'x'.repeat(65), 'テスト', '..']

describe('实例名规则', () => {
  it('能在 v 标志下编译', () => {
    expect(() => new RegExp(`^(?:${INSTANCE_NAME_PATTERN})$`, 'v')).not.toThrow()
  })
  it('接受汉字与字母数字短横线下划线，拒绝非法首字符和路径字符', () => {
    for (const name of accepted) expect(rule.test(name), `应接受 ${name}`).toBe(true)
    for (const name of rejected) expect(rule.test(name), `应拒绝 ${name}`).toBe(false)
  })
})
