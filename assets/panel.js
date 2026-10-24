/**
 * LibMem 面板（浏览器侧，纯 JS，无依赖）。
 *
 * 它由宿主插件 `lib/panel.js` 通过「结构化注入行」塞进页面（桌面端唯一能生效的通道），
 * 数据一律走**相对路径** `/libmem/*`（与页面同源，所以不需要处理跨域 —— 与小鲸鱼同一做法）。
 *
 * 面板能做什么：看状态（服务 / 模型 / 自动回忆 / 写入侧）、试搜一句、温提炼、重建知识库索引、
 * 开关自动回忆。**只做这些** —— 它是"少问我一句"用的，不是管理台。
 */
;(function () {
  'use strict'

  var API = '/libmem'
  var ID = 'libmem-panel'
  if (document.getElementById(ID)) return

  // ---------------------------------------------------------------- 样式
  var css = [
    '#' + ID + '{position:fixed;right:18px;bottom:96px;z-index:2147483000;font:13px/1.5 system-ui,"Segoe UI",sans-serif;color:#e6edf3}',
    '#' + ID + ' .lm-btn{display:flex;align-items:center;gap:6px;padding:8px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.18);background:rgba(22,27,34,.92);color:#e6edf3;cursor:grab;touch-action:none;user-select:none;box-shadow:0 6px 20px rgba(0,0,0,.35);backdrop-filter:blur(6px)}',
    '#' + ID + ' .lm-btn:active{cursor:grabbing}',
    '#' + ID + ' .lm-btn:hover{border-color:rgba(255,255,255,.35)}',
    '#' + ID + ' .lm-dot{width:8px;height:8px;border-radius:50%;background:#8b949e}',
    '#' + ID + ' .lm-dot.ok{background:#3fb950}',
    '#' + ID + ' .lm-dot.bad{background:#f85149}',
    '#' + ID + ' .lm-dot.warn{background:#d29922}',
    '#' + ID + ' .lm-card{position:absolute;right:0;bottom:46px;width:340px;max-height:70vh;overflow:auto;border-radius:12px;border:1px solid rgba(255,255,255,.14);background:rgba(22,27,34,.97);box-shadow:0 12px 40px rgba(0,0,0,.45);padding:12px 14px;display:none}',
    '#' + ID + ' .lm-card.open{display:block}',
    '#' + ID + ' h4{margin:0 0 8px;font-size:13px;font-weight:600;display:flex;justify-content:space-between;align-items:center}',
    '#' + ID + ' .lm-sec{margin:10px 0 0;padding-top:8px;border-top:1px solid rgba(255,255,255,.08)}',
    '#' + ID + ' .lm-kv{display:grid;grid-template-columns:76px 1fr;gap:2px 8px;white-space:pre-wrap;word-break:break-all}',
    '#' + ID + ' .lm-k{color:#8b949e}',
    '#' + ID + ' .lm-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}',
    '#' + ID + ' .lm-row button,#' + ID + ' .lm-row a{padding:4px 9px;border-radius:6px;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.04);color:#e6edf3;cursor:pointer;font-size:12px;text-decoration:none}',
    '#' + ID + ' .lm-row button:hover{background:rgba(255,255,255,.12)}',
    '#' + ID + ' input.lm-q{width:100%;box-sizing:border-box;margin-top:8px;padding:6px 8px;border-radius:6px;border:1px solid rgba(255,255,255,.18);background:rgba(0,0,0,.25);color:#e6edf3}',
    '#' + ID + ' .lm-hit{margin:6px 0 0;padding:6px 8px;border-radius:6px;background:rgba(255,255,255,.04)}',
    '#' + ID + ' .lm-mut{color:#8b949e;font-size:12px}',
    '#' + ID + ' .lm-err{color:#f85149}',
  ].join('\n')

  var style = document.createElement('style')
  style.textContent = css
  document.head.appendChild(style)

  // ---------------------------------------------------------------- 骨架
  var root = document.createElement('div')
  root.id = ID
  root.innerHTML = [
    '<div class="lm-card" id="' + ID + '-card"></div>',
    '<button class="lm-btn" id="' + ID + '-toggle" title="LibMem 记忆插件">',
    '<span class="lm-dot" id="' + ID + '-dot"></span><span>记忆</span></button>',
  ].join('')
  ;(document.body || document.documentElement).appendChild(root)

  var card = document.getElementById(ID + '-card')
  var dot = document.getElementById(ID + '-dot')
  var toggle = document.getElementById(ID + '-toggle')
  var state = { data: null, busy: false, note: '', query: '', hits: null, error: '' }

  // ---------------------------------------------------------------- 位置：可拖动 + 记住
  // 需求原话：「旁边会话框的那个记忆小块能不能改成可移动」。
  // 做法：按住按钮拖动（pointer 事件，鼠标/触屏都行），松手把位置存进 localStorage。
  // 拖动超过 4px 就判定为"拖动"，不再触发展开面板 —— 否则一拖就把面板打开了。
  var POS_KEY = ID + '-pos'

  function loadPos () {
    try {
      var p = JSON.parse(localStorage.getItem(POS_KEY) || 'null')
      if (p && typeof p.left === 'number' && typeof p.top === 'number') return p
    } catch (e) {}
    return null
  }

  function savePos (p) {
    try { localStorage.setItem(POS_KEY, JSON.stringify(p)) } catch (e) {}
  }

  function clamp (v, min, max) {
    return Math.min(Math.max(v, min), Math.max(min, max))
  }

  /** 把 left/top 夹回视口内（窗口缩小、或存的位置已出界时用）。 */
  function place (left, top) {
    var w = root.offsetWidth || 90
    var h = root.offsetHeight || 36
    var l = clamp(left, 4, window.innerWidth - w - 4)
    var t = clamp(top, 4, window.innerHeight - h - 4)
    root.style.left = l + 'px'
    root.style.top = t + 'px'
    root.style.right = 'auto'
    root.style.bottom = 'auto'
    return { left: l, top: t }
  }

  var drag = null
  var suppressClick = false

  toggle.addEventListener('pointerdown', function (e) {
    var rect = root.getBoundingClientRect()
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, left: rect.left, top: rect.top, moved: false }
    try { toggle.setPointerCapture(e.pointerId) } catch (err) {}
  })

  toggle.addEventListener('pointermove', function (e) {
    if (!drag || e.pointerId !== drag.id) return
    var dx = e.clientX - drag.x
    var dy = e.clientY - drag.y
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return
    drag.moved = true
    place(drag.left + dx, drag.top + dy)
  })

  function endDrag (e) {
    if (!drag) return
    if (drag.moved) {
      var rect = root.getBoundingClientRect()
      savePos({ left: rect.left, top: rect.top })
      suppressClick = true
      setTimeout(function () { suppressClick = false }, 0)
    }
    drag = null
    try { toggle.releasePointerCapture(e.pointerId) } catch (err) {}
  }

  toggle.addEventListener('pointerup', endDrag)
  toggle.addEventListener('pointercancel', endDrag)

  window.addEventListener('resize', function () {
    if (!root.style.left) return
    var rect = root.getBoundingClientRect()
    place(rect.left, rect.top)
  })

  // 启动时应用记住的位置
  ;(function () {
    var p = loadPos()
    if (p) place(p.left, p.top)
  })()

  // ---------------------------------------------------------------- 通信
  function api (path, options) {
    return fetch(API + path, options).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status)
      return r.json()
    })
  }

  function refresh () {
    if (state.busy) return
    state.busy = true
    state.error = ''
    api('/status').then(function (data) {
      state.data = data
    }).catch(function (e) {
      state.error = String(e && e.message || e)
    }).then(function () {
      state.busy = false
      render()
    })
  }

  /**
   * 发一个动作，然后重新拉状态。
   *
   * ⚠️ 2026-10-16 修：原来成功分支直接 `return refresh()`，而 `refresh()` 开头
   * 有 `if (state.busy) return` —— 于是**每次点完动作 busy 就永远停在 true**，
   * 面板一直显示"读取中…"，刷新按钮也从此失效（卸载/拉起要靠重新拉状态才知道
   * 结果，这个坑会让按钮看起来"按了没反应"）。现在先清 busy 再 refresh。
   *
   * @param {string} action
   * @param {object|null} [extra] - 附加字段。
   * @param {string} [note] - 忙的时候显示在标题右侧的说明。
   */
  function act (action, extra, note) {
    state.busy = true
    state.note = note || ''
    state.error = ''
    render()
    var body = Object.assign({ action: action }, extra || {})
    api('/action', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (res) {
      if (res && res.message) state.error = res.message
      // 动作可能改了显存占用（卸载/拉起），必须重新拉一次状态，按钮才会跟着换。
      state.busy = false
      state.note = ''
      return refresh()
    }).catch(function (e) {
      state.error = String(e && e.message || e)
      state.busy = false
      state.note = ''
      render()
    })
  }

  function search () {
    var q = state.query.trim()
    if (!q) return
    state.busy = true
    state.error = ''
    state.hits = null
    render()
    api('/search?q=' + encodeURIComponent(q)).then(function (res) {
      state.hits = (res && res.items) || []
      if (res && res.error) state.error = res.error
    }).catch(function (e) {
      state.error = String(e && e.message || e)
    }).then(function () {
      state.busy = false
      render()
    })
  }

  // ---------------------------------------------------------------- 渲染
  function esc (s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
    })
  }

  function kv (pairs) {
    return '<div class="lm-kv">' + pairs.map(function (p) {
      return '<div class="lm-k">' + esc(p[0]) + '</div><div>' + (p[2] ? p[1] : esc(p[1])) + '</div>'
    }).join('') + '</div>'
  }

  /**
   * 4B（在 llama.cpp 路由上）此刻是什么状态。
   * @param {object} r - status 报文里的 modelsCtl.router
   */
  function routerText (r) {
    if (!r) return '未知'
    if (!r.ok) return '路由不可达'
    if (r.value === 'loaded') return '已加载（占显存）'
    if (r.value === 'sleeping') return '睡眠（权重已出显存，进程还在）'
    if (r.value === 'unloaded') return '已卸载'
    if (r.value === 'missing') return '路由里没有这个模型'
    return String(r.value || '未知')
  }

  /**
   * 两个 bge（独立进程）此刻是什么状态。
   * @param {object} s - status 报文里的 modelsCtl.embed / .rerank
   */
  function serverText (s) {
    if (!s) return '未知'
    if (!s.up) return '已卸载（进程不在）'
    return s.sleeping ? '睡眠（权重已出显存，进程还在）' : '在线（占显存）'
  }

  function render () {
    var d = state.data
    // 状态点
    var cls = 'lm-dot'
    if (state.error) cls += ' bad'
    else if (!d) cls += ''
    else if (!d.service || !d.service.up) cls += ' bad'
    else if (d.models && d.models.degraded) cls += ' warn'
    else cls += ' ok'
    dot.className = cls

    if (!card.classList.contains('open')) return

    var html = ['<h4><span>LibMem 记忆</span><span class="lm-mut">' + (state.busy ? esc(state.note || '读取中…') : '') + '</span></h4>']
    html.push('<div class="lm-mut" style="margin:-4px 0 6px">基于图书馆系统 LIS 做的长期记忆系统</div>')
    if (state.error) html.push('<div class="lm-err">' + esc(state.error) + '</div>')

    if (!d) {
      html.push('<div class="lm-mut">拿不到状态。若刚重启过，等几秒再刷新。</div>')
    } else {
      var s = d.service || {}
      html.push('<div class="lm-sec">服务</div>')
      html.push(kv([
        ['状态', s.up ? ('在线 · ' + (s.mode || '')) : '离线'],
        ['版本', s.version || '—'],
        ['PID', s.pid == null ? '—' : String(s.pid)],
        ['工具', (d.tools == null ? '—' : d.tools + ' 个')],
      ]))
      if (s.baseDir) html.push(kv([['数据', s.baseDir]]))

      var m = d.models || {}
      html.push('<div class="lm-sec">模型服务</div>')
      html.push(kv([
        ['嵌入', m.embed ? (m.embed.ok ? ('ok ' + m.embed.ms + 'ms') : ('✗ ' + (m.embed.error || '不可用'))) : '未探测'],
        ['重排', m.rerank ? (m.rerank.ok ? ('ok ' + m.rerank.ms + 'ms') : ('✗ ' + (m.rerank.error || '不可用'))) : '未探测'],
      ]))
      if (m.degraded) html.push('<div class="lm-mut">⚠️ 模型服务不可用：检索结果会变差（护栏保证不返回空）</div>')

      // ★ 2026-10-16：本地模型显存控制（用户要求：「一个卸载模型按钮 + 后面跟一个
      //   需要的时候自动拉起的按钮」）。
      //   说清楚语义再给控件 —— 这两个按钮合起来才是完整的一件事：
      //   卸载只负责"现在就把显存还回来"，回不回来由那个开关决定。
      var mc = d.modelsCtl
      if (mc) {
        html.push('<div class="lm-sec">本地模型显存</div>')
        html.push(kv([
          ['4B', routerText(mc.router)],
          ['嵌入', serverText(mc.embed)],
          ['重排', serverText(mc.rerank)],
        ]))
        html.push('<div class="lm-mut">' +
          (mc.holds ? '还有模型占着显存。点「卸载模型」立刻释放（4B 走路由卸载，两个 bge 结束进程）。'
            : '三个模型都不占显存了。下次用到记忆时按下面的开关决定要不要自动拉回来。') +
          '</div>')
        html.push('<div class="lm-mut">' +
          (mc.autoload
            ? '自动拉起=开：卸载后第一次检索/提炼会自己把模型拉回来（约 2 秒）。'
            : '自动拉起=关：卸载之后**不会再自动拉起**，要用就点「拉起模型」。') +
          '</div>')
      }

      var r = d.recall || {}
      html.push('<div class="lm-sec">自动回忆</div>')
      html.push(kv([
        ['档位', r.enabled ? ((r.modeLabel || r.mode || '') + '（' + (r.mode || '') + '）') : '关'],
        ['统计', '判断 ' + (r.decisions || 0) + ' · 翻 ' + (r.recalls || 0) + ' · 点名 ' + (r.explicit || 0) +
          ' · 注入 ' + (r.injections || 0) + ' · 采用 ' + (r.adopted || 0) + ' · 失败 ' + (r.failures || 0)],
        ['纪律', r.discipline ? '开（要求模型读块、引用出处、用了就回报）' : '关（只贴块，不约束模型）'],
      ]))
      if (r.modeHint) html.push('<div class="lm-mut">' + esc(r.modeHint) + '</div>')

      // ★ 2026-10-12：档位选择 —— 照千问绘图那个「生图判断」四档做的。
      //   切换后由服务端白名单校验，界面只负责发指令。
      var modes = r.modes || []
      if (modes.length) {
        html.push('<div class="lm-mut" style="margin-top:6px">切换档位（立即生效，重启回到配置值）</div>')
        html.push('<div class="lm-row">' + modes.map(function (m) {
          var on = m.key === r.mode
          return '<button data-act="mode" data-mode="' + esc(m.key) + '"' +
            (on ? ' style="border-color:#3fb950;color:#3fb950"' : '') + '>' +
            (on ? '● ' : '') + esc(m.label) + '</button>'
        }).join('') + '</div>')
      }

      var ld = r.lastDecision
      if (ld) html.push('<div class="lm-mut">最近判断：' + (ld.recall ? '翻' : '不翻') + '（' + esc(ld.why || '') + '，' + (ld.chars || 0) + ' 字）' + (ld.coldStart ? ' · 会话开局' : '') + '</div>')
      if (r.last && r.last.query) html.push('<div class="lm-mut">最近翻到：' + esc(r.last.query) + '…（' + esc(r.last.why || '') + '）</div>')
      if (r.lastFeedback) html.push('<div class="lm-mut">最近采用：' + (r.lastFeedback.count || 0) + ' 条 ' + esc((r.lastFeedback.ids || []).join(', ')) + '</div>')

      var w = d.write || {}
      html.push('<div class="lm-sec">写入侧（冷库）</div>')
      html.push(kv([
        ['已写', (w.pairsWritten || 0) + ' 轮（跳过 ' + (w.pairsSkipped || 0) + ' / 失败 ' + (w.pairsFailed || 0) + '）'],
        ['轮询', (w.pollFiles || 0) + ' 个文件'],
      ]))
      if (w.lastWrite) html.push('<div class="lm-mut">最近落盘：' + esc(w.lastWrite.ref || JSON.stringify(w.lastWrite).slice(0, 80)) + '</div>')

      html.push('<div class="lm-sec">试搜一句</div>')
      html.push('<input class="lm-q" placeholder="回车搜索记忆…" value="' + esc(state.query) + '">')
      if (state.hits) {
        if (!state.hits.length) html.push('<div class="lm-mut">没有命中。</div>')
        for (var i = 0; i < state.hits.length; i++) {
          var h = state.hits[i]
          html.push('<div class="lm-hit">[' + esc(h.source_date || '') + (h.task_type ? '·' + esc(h.task_type) : '') + '] ' + esc(h.summary || '') + '</div>')
        }
      }

      html.push('<div class="lm-sec">操作</div>')
      // ★ 2026-10-16：这两个按钮是一对，排在最前面（用户要的"卸载 + 后面跟一个自动拉起"）。
      //   第一个按钮是**同一个位置的两个状态**：还有东西占显存时是「卸载模型」，
      //   卸干净之后变成「拉起模型」—— 这样即使自动拉起是关的，也总有手动回来的路，
      //   不会出现"点了卸载就再也回不来"。
      if (mc) {
        html.push('<div class="lm-row">' +
          '<button data-act="' + (mc.holds ? 'unload' : 'load') + '"' +
          (mc.holds ? '' : ' style="border-color:#3fb950;color:#3fb950"') + '>' +
          (mc.holds ? '卸载模型' : '拉起模型') + '</button>' +
          '<button data-act="autoload"' +
          (mc.autoload ? ' style="border-color:#3fb950;color:#3fb950"' : '') + '>' +
          (mc.autoload ? '● ' : '') + '需要时自动拉起：' + (mc.autoload ? '开' : '关') + '</button>' +
          '</div>')
      }
      html.push('<div class="lm-row">' +
        '<button data-act="refresh">刷新</button>' +
        '<button data-act="extract">温提炼</button>' +
        '<button data-act="reindex">重建知识库索引</button>' +
        '<button data-act="discipline-toggle">' + (r.discipline ? '关掉记忆纪律' : '打开记忆纪律') + '</button>' +
        '</div>')
    }

    card.innerHTML = html.join('')
    anchorCard()   // 按钮被拖到边上时，卡片改从另一侧展开

    // 事件绑定（每次重渲染都重绑，避免持有旧节点）
    var q = card.querySelector('input.lm-q')
    if (q) {
      q.addEventListener('input', function (e) { state.query = e.target.value })
      q.addEventListener('keydown', function (e) { if (e.key === 'Enter') search() })
    }
    Array.prototype.forEach.call(card.querySelectorAll('button[data-act]'), function (b) {
      b.addEventListener('click', function () {
        var a = b.getAttribute('data-act')
        if (a === 'refresh') refresh()
        else if (a === 'extract') act('extract')
        else if (a === 'reindex') act('reindex')
        else if (a === 'unload') act('unload', null, '正在卸载模型…')
        else if (a === 'load') act('load', null, '正在拉起模型…')
        else if (a === 'autoload') {
          act('autoload',
            { enabled: !(state.data && state.data.modelsCtl && state.data.modelsCtl.autoload) },
            '正在切换开关…')
        }
        else if (a === 'mode') act('recall', { mode: b.getAttribute('data-mode') })
        else if (a === 'discipline-toggle') {
          act('discipline', { enabled: !(state.data && state.data.recall && state.data.recall.discipline) })
        }
      })
    })
  }

  toggle.addEventListener('click', function () {
    // 刚才那一下是"拖动"而不是"点击"时，不展开面板（见 pointerup 里的 suppressClick）
    if (suppressClick) return
    card.classList.toggle('open')
    if (card.classList.contains('open')) refresh()
    else render()
  })

  /**
   * 面板卡片贴在按钮的哪一侧：按钮在屏幕左半边就朝右展开，反之朝左 ——
   * 这样把按钮拖到边上时，卡片不会被推出屏幕。
   */
  function anchorCard () {
    var rect = root.getBoundingClientRect()
    var leftHalf = rect.left + rect.width / 2 < window.innerWidth / 2
    card.style.left = leftHalf ? '0' : 'auto'
    card.style.right = leftHalf ? 'auto' : '0'
  }

  // 打开时拉一次状态（让状态点在没点开前也有颜色）
  refresh()
})()
