#!/usr/bin/env node
/**
 * 给本插件目录补上两个「共享依赖」的链接（Windows 上是 junction）。
 *
 * 为什么需要它
 * ------------
 * 插件运行时要 import 两个 DSH 自带的包：
 *   `@deepseek-ai/schemastery`（配置 schema）与 `@deepseek-ai/dsh-tools`（工具定义）。
 * 这两个包**由 DSH 自己提供**、不随插件分发（也不该分发）。而插件是 `link:` 安装的，
 * 真实路径在 profile 之外，Node 从插件目录往上找**找不到**它们 —— 这就是为什么本仓既有
 * 的本地插件（`dsh-hybrid-gateway` / `dsh-cold-memory-watcher`）都在自己的
 * `node_modules` 下放 junction。
 *
 * 本脚本把这个动作自动化：从**你这台机器**的 `<DSH_HOME>/profiles/node_modules` 建链接。
 * 不需要联网、不复制文件、不污染 profile。
 *
 * 用法（在插件目录里跑）：
 *   node bin/setup-links.mjs
 *   node bin/setup-links.mjs --dsh-home "D:\somewhere\.dsh"
 *
 * 另一种做法（需要联网）：在本目录跑 `pnpm install`，从 npm 拉同版本的真依赖。
 * 两种都可以；脚本这条路是离线可用的兜底。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const PACKAGE_ROOT = path.join(import.meta.dirname, '..')
/** 需要链接的两个共享包。 */
const NEEDED = ['@deepseek-ai/schemastery', '@deepseek-ai/dsh-tools']

const argv = process.argv.slice(2)
const flag = (name, fallback) => {
  const index = argv.indexOf(name)
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback
}

const dshHome = flag('--dsh-home', process.env.DSH_HOME || path.join(os.homedir(), '.dsh'))
const sourceRoot = path.join(dshHome, 'profiles', 'node_modules')

console.log(`插件目录   : ${PACKAGE_ROOT}`)
console.log(`DSH_HOME   : ${dshHome}`)
console.log(`共享包来源 : ${sourceRoot}\n`)

if (!fs.existsSync(sourceRoot)) {
  console.error(`❌ 找不到共享包目录：${sourceRoot}`)
  console.error('   先用 --dsh-home 指到你的 .dsh 目录；或改用 `pnpm install` 从 npm 拉依赖。')
  process.exit(1)
}

let failed = 0
for (const name of NEEDED) {
  const target = path.join(sourceRoot, name)
  const linkPath = path.join(PACKAGE_ROOT, 'node_modules', name)
  const label = name.padEnd(28)

  if (!fs.existsSync(target)) {
    console.error(`❌ ${label} 源不存在：${target}`)
    failed += 1
    continue
  }
  if (fs.existsSync(linkPath)) {
    console.log(`✅ ${label} 已存在（跳过）`)
    continue
  }
  try {
    fs.mkdirSync(path.dirname(linkPath), { recursive: true })
    // Windows 上目录链接用 junction（不需要管理员权限）；其他平台用目录软链。
    fs.symlinkSync(target, linkPath, process.platform === 'win32' ? 'junction' : 'dir')
    console.log(`✅ ${label} 已链接 → ${target}`)
  } catch (error) {
    console.error(`❌ ${label} 建链接失败：${error.message}`)
    console.error('   手工等价做法（Windows）：mklink /J "<插件目录>\\node_modules\\@deepseek-ai\\<包名>" "<来源>"')
    failed += 1
  }
}

console.log()
if (failed > 0) {
  console.error(`有 ${failed} 项没成功 —— 插件会以「无法解析模块」加载失败，先修好再装。`)
  process.exit(1)
}
console.log('两个共享依赖都就绪。下一步：dsh plugin --profile <profile> add link:' + PACKAGE_ROOT)
