// 回归测试：唤醒磁盘上的会话必须走 Host 的 sessionController（preset 工具集 + 模型路由）。
//
// 背景：以前用 ctx.agents.resume({ resumeSessionId }) 裸唤醒，preset 没人 mount，
// agent 只剩宿主层插件工具（浏览器 / 表情 / memos / 微信绑定 / working_directory），
// shell / 读写 / glob 这些 preset 工具整体消失。
//
// 用法：node _wake-selftest.mjs

import { 造唤醒会话 } from './lib/host.js'

let 通过 = 0
const 失败 = []

const 断言 = (名字, 条件, 详情) => {
  if (条件) {
    通过 += 1
    console.log(`  ✓ ${名字}`)
  } else {
    失败.push(`${名字}${详情 === undefined ? '' : ` — ${详情}`}`)
    console.log(`  ✗ ${名字}${详情 === undefined ? '' : ` — ${详情}`}`)
  }
}

// 1) 有会话控制器：必须走 resolveAgent，且不能碰裸 resume
{
  const 调用 = []
  const agent = { id: 's1' }
  const ctx = {
    get: (名) => (名 === 'sessionController'
      ? { resolveAgent: async (id) => { 调用.push(['resolve', id]); return { agent } } }
      : undefined),
    agents: {
      resume: async () => { 调用.push(['resume']) },
      get: () => undefined,
    },
  }
  const 起 = await 造唤醒会话(ctx, () => {}, (id) => (id === 's1' ? agent : undefined))('s1')
  断言('有控制器时调用 resolveAgent', 调用.length === 1 && 调用[0][0] === 'resolve' && 调用[0][1] === 's1', JSON.stringify(调用))
  断言('有控制器时返回它的 agent', 起 === agent)
  断言('有控制器时不裸 resume', !调用.some((x) => x[0] === 'resume'))
}

// 2) 控制器报错（例如会话被别的写者占着）：把原因抛出去，不许偷偷裸 resume
{
  let 裸 = 0
  const ctx = {
    get: () => ({ resolveAgent: async () => ({ error: new Error('writer held') }) }),
    agents: { resume: async () => { 裸 += 1 }, get: () => undefined },
  }
  let 抛 = ''
  try {
    await 造唤醒会话(ctx, () => {}, () => undefined)('s2')
  } catch (error) {
    抛 = String((error && error.message) || error)
  }
  断言('控制器报错要抛出原因', 抛 === 'writer held', 抛)
  断言('控制器报错时也不裸 resume', 裸 === 0)
}

// 3) 宿主没有会话控制器（老 DSH / 精简部署）：只能裸 resume，但必须记账警告
{
  let resume参数
  const 记 = []
  const agent = { id: 's3' }
  const ctx = {
    get: () => undefined,
    agents: {
      resume: async (o) => { resume参数 = o },
      get: (id) => (id === 's3' ? agent : undefined),
    },
  }
  const 起 = await 造唤醒会话(ctx, (t) => 记.push(t), (id) => (id === 's3' ? agent : undefined))('s3')
  断言('无控制器时退回 resume', Boolean(resume参数) && resume参数.resumeSessionId === 's3', JSON.stringify(resume参数))
  断言('无控制器时返回恢复出来的 agent', 起 === agent)
  断言('无控制器时必须记账警告', 记.some((t) => String(t).includes('sessionController')), JSON.stringify(记))
}

// 4) 待办：唤醒失败不能让调用方拿到半截对象
{
  const ctx = { get: () => ({ resolveAgent: async () => ({}) }), agents: { resume: async () => {}, get: () => undefined } }
  let 抛 = false
  try {
    await 造唤醒会话(ctx, () => {}, () => undefined)('s4')
  } catch (error) {
    抛 = true
  }
  断言('控制器没给 agent 时要抛错', 抛)
}

console.log(`\n${通过} 通过，${失败.length} 失败`)
if (失败.length) {
  console.log(失败.map((x) => `  ✗ ${x}`).join('\n'))
  process.exitCode = 1
}
