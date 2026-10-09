import '/vendor/foliate-js/view.js'
import { Overlayer } from '/vendor/foliate-js/overlayer.js'
import { geoMapHTML, renderGeoMap } from '/web/geo_map.js'
import { createGloss, rawText } from '/web/gloss_ui.js'

const $ = s => document.querySelector(s)
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
const desk = window.OWN_READER_THEME === 'desk'
// LLM 回答按 Markdown 渲染；原始 HTML 丢弃与链接协议白名单见 web/safe_md.js
const md = s => globalThis.marked ? globalThis.marked.parse(String(s ?? ''), { breaks: true }) : esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
const bookId = Number(new URLSearchParams(location.search).get('id'))

const device = (() => {
  try {
    let d = localStorage.getItem('own-reader-device')
    if (!d) {
      const kind = /Android/.test(navigator.userAgent) ? 'android' : /Mac/.test(navigator.userAgent) ? 'mac' : 'web'
      d = `${kind}-${Math.random().toString(36).slice(2, 8)}`
      localStorage.setItem('own-reader-device', d)
    }
    return d
  } catch { return 'unknown' }
})()

const api = (path, body) => fetch(path, body ? {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
} : undefined).then(async r => {
  const j = await r.json()
  if (!r.ok) throw new Error(j.error || r.status)
  return j
})
const logEvent = (type, extra = {}) => api('/api/events', { device, book_id: bookId, type, ...extra })

const LABELS = { free: '免费模型', local: '本地模型', claude: 'Claude', grok: 'Grok', kimi: 'Kimi', gemini: 'Gemini' }
// 个人数据不许发给免费模型和 Gemini（与 server/llm.py 的 POLICY_DRAFT 一致）
const UPGRADES = ['local', 'grok', 'kimi', 'claude']

// value(cfi) -> { kind: 'hl'|'ask', ...record }
const marks = new Map()
let view, current = null, lastAnnoClick = 0
// PDF（foliate 固定版式渲染器没有 overlayer）：自绘划线层
let isPDF = false, bookBlob = null, pdfTextDoc = null
const docIndex = new WeakMap()
const pdfResolvers = new Map()

const BASE_CSS = desk ? `
  html { color-scheme: light dark; }
  html, body { background: transparent !important; }
  body { color: #2a2622 !important; }
  p, li, blockquote, dd { text-align: justify; }
  ::selection { background: rgba(163, 48, 42, .22); }
  @media (prefers-color-scheme: dark) { body { color: #e8e2d9 !important; } a { color: #e07a5f; } }
` : `
  html { color-scheme: light; }
  body { color: #000 !important; background: #fff !important; }
  p, li, blockquote, dd { text-align: justify; }
  ::selection { background: #999; color: #000; }
`

// 排版设置（每台设备各存一份）：中文字体、英文字体、字号、行距。
// 字体来源：服务器 /api/fonts（字体文件，任何设备可用）+ Mac 系统字体（只在 Mac 上有）。
const MAC_FONTS = [
  { family: 'Songti SC', label: '宋体（系统）', zh: true }, { family: 'Kaiti SC', label: '楷体（系统）', zh: true },
  { family: 'STFangsong', label: '仿宋（系统）', zh: true }, { family: 'PingFang SC', label: '苹方（系统）', zh: true },
  { family: 'Iowan Old Style', label: 'Iowan Old Style（系统）' }, { family: 'Palatino', label: 'Palatino（系统）' },
  { family: 'Georgia', label: 'Georgia（系统）' }, { family: 'Baskerville', label: 'Baskerville（系统）' },
  { family: 'New York', label: 'New York（系统）' },
]
const TYPO_KEY = 'own-reader-typo'
const TYPO_DEFAULT = desk ? { zh: 'Songti SC', en: 'Iowan Old Style', size: 106, line: 1.9 }
  : { zh: 'Noto Serif SC', en: 'ChareInk7SPW', size: window.OWN_READER_LARGE ? 135 : 100, line: 1.75, bold: 0.3 }
const typo = (() => {
  try { return { ...TYPO_DEFAULT, ...JSON.parse(localStorage.getItem(TYPO_KEY) || '{}') } } catch { return { ...TYPO_DEFAULT } }
})()
// Mac App 的 ⌘+/⌘-/⌘0 调字号（不能用整页缩放，见 macapp/main.swift）；d=0 还原默认
window.ownReaderFontStep = d => {
  typo.size = d === 0 ? TYPO_DEFAULT.size : Math.max(80, Math.min(200, typo.size + d))
  try { localStorage.setItem(TYPO_KEY, JSON.stringify(typo)) } catch {}
  applyStyles()
}
let serverFonts = []

function typoCSS() {
  const faces = serverFonts.filter(f => f.family === typo.zh || f.family === typo.en).flatMap(f => f.files.map(x =>
    `@font-face { font-family: "${f.family}"; src: url("${location.origin}${encodeURI(x.url)}");
       font-weight: ${x.weight}; font-style: ${x.italic ? 'italic' : 'normal'}; font-display: swap; }`)).join('\n')
  const stack = `"${typo.en}", "${typo.zh}", serif`
  return `${faces}
  body, p, li, div, span, blockquote, td, h1, h2, h3, h4, h5, h6 { font-family: ${stack} !important; }
  body { font-size: ${typo.size}% !important; }
  p, li, blockquote, dd { line-height: ${typo.line} !important; }
  ${typo.bold ? `body, body * { -webkit-text-stroke: ${typo.bold}px currentColor; }` : ''}`
}
const applyStyles = () => view?.renderer.setStyles?.(BASE_CSS + typoCSS())

// ---- PDF：foliate 固定版式渲染器不支持 overlayer，划线用注入页面内的 SVG 自绘 ----

const resolveMark = cfi => {
  if (!pdfResolvers.has(cfi)) pdfResolvers.set(cfi, view?.resolveNavigation?.(cfi) ?? null)
  return pdfResolvers.get(cfi)
}

const SVG_NS = 'http://www.w3.org/2000/svg'
// 点击命中：与 foliate Overlayer 同思路，保存视口坐标矩形自行比较（SVG 不接收指针事件）
const pdfHitRects = new WeakMap()

function drawPDFMarks(doc, index) {
  doc.getElementById('or-pdf')?.remove()
  if (index == null) return
  const layer = doc.createElementNS(SVG_NS, 'svg')
  layer.id = 'or-pdf'
  Object.assign(layer.style, {
    position: 'absolute', top: '0', left: '0', width: '100%', height: '100%',
    pointerEvents: 'none', zIndex: '5',
  })
  // 页面渲染带 CSS transform（--scale-factor），把视口坐标换算回布局坐标
  const html = doc.documentElement
  const hb = html.getBoundingClientRect()
  const sx = html.offsetWidth ? hb.width / html.offsetWidth : 1
  const sy = html.offsetHeight ? hb.height / html.offsetHeight : 1
  const hits = []
  for (const [cfi, m] of marks) {
    const r = resolveMark(cfi)
    if (!r || r.index !== index) continue
    let range
    try { range = typeof r.anchor === 'function' ? r.anchor(doc) : r.anchor } catch { continue }
    if (!range || typeof range.getClientRects !== 'function') continue
    const rects = Array.from(range.getClientRects())
    if (!rects.length) continue
    hits.push({ cfi, rects })
    for (const rect of rects) {
      const el = doc.createElementNS(SVG_NS, 'rect')
      const x = (rect.left - hb.left) / sx
      const y = (rect.top - hb.top) / sy
      const w = rect.width / sx
      const h = rect.height / sy
      el.setAttribute('x', x); el.setAttribute('width', Math.max(w, 1))
      if (m.kind === 'ask') {
        el.setAttribute('y', y + h - 2 / sy); el.setAttribute('height', Math.max(2 / sy, 1))
        el.setAttribute('fill', desk ? '#a3302a' : '#000')
      } else {
        el.setAttribute('y', y); el.setAttribute('height', Math.max(h, 1))
        el.setAttribute('fill', desk ? '#f2cf5b' : '#bdbdbd')
        el.setAttribute('fill-opacity', '0.4')
      }
      layer.append(el)
    }
  }
  pdfHitRects.set(doc, hits)
  if (layer.childElementCount) {
    doc.body.style.position = 'relative'
    doc.body.append(layer)
  }
}

const redrawPDF = () => {
  for (const { doc } of view?.renderer?.getContents() ?? [])
    if (doc?.body) drawPDFMarks(doc, docIndex.get(doc))
}

// pdf.js 的文字层在 iframe load 后异步渲染、缩放/翻页时也会重渲染，
// 划线层需要跟随补画（relocate 后与窗口尺寸变化时调度）
let pdfDrawTimer
const schedulePDFRedraw = () => {
  clearTimeout(pdfDrawTimer)
  pdfDrawTimer = setTimeout(() => { redrawPDF(); setTimeout(redrawPDF, 600) }, 250)
}
addEventListener('resize', () => { if (isPDF) schedulePDFRedraw() })

function hitMark(doc, e) {
  const { clientX: x, clientY: y } = e
  const hits = pdfHitRects.get(doc) ?? []
  for (let i = hits.length - 1; i >= 0; i--)
    for (const { top, left, bottom, right } of hits[i].rects)
      if (top <= y && left <= x && bottom > y && right > x) return marks.get(hits[i].cfi)
  return null
}

// ---- PDF 文字层：提问上下文取当前页与前后各一页；扫描版检测 ----

async function pdfDocForText() {
  if (pdfTextDoc) return pdfTextDoc
  if (!globalThis.pdfjsLib) await import('/vendor/foliate-js/pdf.js')
  const { pdfjsLib } = globalThis
  const base = `${location.origin}/vendor/foliate-js/vendor/pdfjs/`
  const transport = new pdfjsLib.PDFDataRangeTransport(bookBlob.size, [])
  transport.requestDataRange = (b, e) =>
    bookBlob.slice(b, e).arrayBuffer().then(chunk => transport.onDataRange(b, chunk))
  pdfTextDoc = await pdfjsLib.getDocument({
    range: transport, cMapUrl: `${base}cmaps/`,
    standardFontDataUrl: `${base}standard_fonts/`, isEvalSupported: false,
  }).promise
  return pdfTextDoc
}

async function pageText(d, pageNumber) {
  if (pageNumber < 1 || pageNumber > d.numPages) return ''
  try {
    const tc = await (await d.getPage(pageNumber)).getTextContent()
    return tc.items.map(it => it.str + (it.hasEOL ? '\n' : ' ')).join('').trim()
  } catch { return '' }
}

async function checkPDFTextLayer() {
  try {
    const d = await pdfDocForText()
    let chars = 0
    for (let i = 1; i <= Math.min(d.numPages, 5); i++) chars += (await pageText(d, i)).length
    if (chars) return
    if (localStorage.getItem(`own-reader-pdf-notext-${bookId}`)) return
    document.body.insertAdjacentHTML('afterbegin', `<div id="pdf-hint" style="position:fixed;top:0;left:0;right:0;z-index:30;
      background:#5b4a36;color:#fff;padding:10px 14px;font-size:14px;display:flex;gap:10px;align-items:center">
      <span style="flex:1">这个 PDF 没有文字层（扫描版）：可以翻页和记进度，但不能选字划线或提问。</span>
      <button id="pdf-hint-x" style="background:transparent;color:#fff;border:1px solid #fff;border-radius:6px;padding:2px 10px">知道了</button></div>`)
    $('#pdf-hint-x').onclick = () => {
      $('#pdf-hint').remove()
      try { localStorage.setItem(`own-reader-pdf-notext-${bookId}`, '1') } catch {}
    }
  } catch (e) { console.error(e) }
}

async function main() {
  const state = await api(`/api/books/${bookId}/state`)
  document.title = state.title
  $('#title').textContent = state.title
  // 安卓 crengine 版留下的划线没有 cfi（只有 xpointer）：各节载入时按原文找到位置再画（placeXpHighlights）
  for (const h of state.highlights) {
    if (h.cfi) marks.set(h.cfi, { kind: 'hl', ...h })
    else if (h.pos_kind === 'crengine' && h.text) xpPending.push(h)
  }
  for (const k of (state.inks || [])) if (k.pos_kind !== 'pdf') inkPending.push(k)
  for (const a of state.asks) if (a.cfi && !marks.has(a.cfi)) marks.set(a.cfi, { kind: 'ask', ...a })

  const blob = await fetch(`/api/books/${bookId}/file`).then(r => {
    if (!r.ok) throw new Error('这本书没有可打开的文件（EPUB/PDF）')
    return r.blob()
  })
  isPDF = state.format === 'pdf'
  bookBlob = blob
  view = document.createElement('foliate-view')
  document.body.prepend(view)
  await view.open(new File([blob], isPDF ? 'book.pdf' : 'book.epub',
    { type: isPDF ? 'application/pdf' : 'application/epub+zip' }))
  if (isPDF) {
    view.renderer.setAttribute('zoom', 'fit-width')
    checkPDFTextLayer()
  } else {
    view.renderer.setAttribute('flow', 'paginated')
    view.renderer.setAttribute('margin', desk ? '56px' : '24px')
    view.renderer.setAttribute('gap', desk ? '7%' : '6%')
    if (desk) {
      view.renderer.setAttribute('max-inline-size', '640px')
      view.renderer.setAttribute('max-column-count', '2')
    }
    // 大屏墨水屏：foliate 默认栏宽 720px，13.3 寸上两边空白太大
    if (window.OWN_READER_LARGE) {
      view.renderer.setAttribute('max-inline-size', '1080px')
      view.renderer.setAttribute('max-column-count', '1')
      view.renderer.setAttribute('gap', '4%')
    }
  }
  serverFonts = (await api('/api/fonts').catch(() => ({ fonts: [] }))).fonts
  applyStyles()

  view.addEventListener('load', onLoad)
  view.addEventListener('relocate', onRelocate)
  view.addEventListener('create-overlay', () => { for (const value of marks.keys()) view.addAnnotation({ value }) })
  view.addEventListener('draw-annotation', e => {
    const m = marks.get(e.detail.annotation.value)
    if (m?.kind === 'ask') e.detail.draw(Overlayer.underline, { color: desk ? '#a3302a' : '#000', width: 2 })
    else e.detail.draw(Overlayer.highlight, { color: desk ? '#f2cf5b' : '#bdbdbd' })
  })
  view.addEventListener('show-annotation', e => {
    lastAnnoClick = performance.now()
    const m = marks.get(e.detail.value)
    if (m) showMark(m)
  })

  // 安卓版更晚读过时，它的 xpointer 网页版用不了，按 fraction 跳
  const xp = state.progress_xp
  const useXp = xp?.fraction != null && (!state.progress || (xp.ts || '') > (state.progress.ts || ''))
  await view.init({ lastLocation: useXp ? undefined : state.progress?.cfi, showTextStart: !useXp })
  if (useXp) await view.goToFraction(xp.fraction)
  logEvent('open')
}

function onKey(e) {
  if (e.target.closest?.('textarea, input')) return
  if (e.key === 'ArrowLeft' || e.key === 'PageUp') view?.goLeft()
  if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); view?.goRight() }
  if (e.key === 'Escape') { closeSheet(); $('#top').classList.remove('show') }
}

// ---- 按需对照翻译（2026-10-07）：外文书默认开，每节载入时翻该节、顺带预取下一节；译文以 .or-tr 追加在原文段落末尾 ----
const TR_KEY = `own-reader-tr-${bookId}`
let trOn = (() => { try { return localStorage.getItem(TR_KEY) !== 'off' } catch { return true } })()
const trDocs = new Map() // index -> doc
// div 只取叶子块（2026-10-09 补，与 server/bilingual.py、精读批注一致）
const TR_SEL = 'p, li, blockquote, h1, h2, h3, h4, h5, h6, dd, dt, figcaption, div'
const trBlocks = doc => [...doc.querySelectorAll(TR_SEL)]
  .filter(el => !el.querySelector(TR_SEL) && !el.closest('.or-tr, .or-gl, .or-ink') && el.textContent.trim().length > 1)
const trText = rawText // 去掉插进去的译文与精读批注再取原文
const TR_CSS = `.or-tr { display: block; margin-top: .3em; font-size: .94em; text-indent: 0; text-align: justify;
  color: ${desk ? '#6b625a' : '#000'}; } .or-tr .or-loc { font-size: .75em; opacity: .7; }
  ${desk ? '@media (prefers-color-scheme: dark) { .or-tr { color: #b9b0a5; } }' : ''}`
let trRenderTimer
function trRelayout() {
  clearTimeout(trRenderTimer)
  trRenderTimer = setTimeout(() => view?.renderer?.render?.(), 600)
}
function trInsert(els, hashes, done) {
  let changed = false
  els.forEach((el, i) => {
    const t = done[hashes[i]]
    if (!t || el.querySelector(':scope > .or-tr')) return
    const span = el.ownerDocument.createElement('span')
    span.className = 'or-tr'
    span.lang = 'zh-CN'
    span.textContent = t.zh
    if (t.local) span.insertAdjacentHTML('beforeend', ' <span class="or-loc">〔本机模型译，数字请对照原文〕</span>')
    el.append(span)
    changed = true
  })
  if (changed) trRelayout()
}
async function trSection(doc, index) {
  if (!trOn || isPDF) return
  if (!doc.getElementById('or-tr-css')) {
    const st = doc.createElement('style')
    st.id = 'or-tr-css'
    st.textContent = TR_CSS
    doc.head?.append(st)
  }
  const els = trBlocks(doc)
  if (!els.length) return
  let r
  try { r = await api('/api/translate', { paras: els.map(trText) }) } catch { return }
  if (r.lang === 'zh') return
  $('#b-tr').hidden = false
  // 服务器会丢掉空段，按同样规则对齐
  const kept = els.filter(el => trText(el))
  trInsert(kept, r.hashes, r.done)
  trPrefetch(index + 1)
  let pending = r.pending, tries = 0
  while (pending && trOn && trDocs.get(index) === doc && tries++ < 120) {
    await new Promise(res => setTimeout(res, 2500))
    let g
    try { g = await api('/api/translate/get', { hashes: r.hashes }) } catch { continue }
    trInsert(kept, r.hashes, g.done)
    pending = g.pending
  }
}
const trPrefetched = new Set()
async function trPrefetch(index) {
  const sec = view?.book?.sections?.[index]
  if (!sec?.createDocument || trPrefetched.has(index)) return
  trPrefetched.add(index)
  try {
    const doc = await sec.createDocument()
    const paras = trBlocks(doc).map(trText)
    if (paras.length) await api('/api/translate', { paras })
  } catch {}
}
$('#b-tr').onclick = () => {
  trOn = !trOn
  try { localStorage.setItem(TR_KEY, trOn ? 'on' : 'off') } catch {}
  $('#b-tr').classList.toggle('on', trOn)
  for (const [index, doc] of trDocs) {
    if (trOn) trSection(doc, index)
    else doc.querySelectorAll('.or-tr').forEach(n => n.remove())
  }
  if (!trOn) trRelayout()
}

const gloss = createGloss({ api, bookId, desk, getView: () => view, relayout: trRelayout, isPDF: () => isPDF, openSheet })
$('#b-gl').classList.toggle('on', gloss.isOn())
$('#b-gl').onclick = async () => {
  const render = async () => {
    openSheet('精读批注', await gloss.panelHTML())
    gloss.bindPanel($('#sheet-body'), render)
    $('#b-gl').classList.toggle('on', gloss.isOn())
  }
  await render()
}

// 全书导读（2026-10-09）：把书读厚的总入口
$('#b-dos').onclick = async () => {
  const show = async () => {
    let d
    try { d = await api(`/api/books/${bookId}/dossier`) } catch (e) { openSheet('全书导读', `出错了：${esc(e.message)}`); return }
    if (d.state === 'done') {
      openSheet('全书导读', `<div class="answer">${md(d.md)}</div>
        <div class="muted">${esc(d.model)} · 约 ${(d.usd || 0).toFixed(2)} 美元${d.truncated ? ' · 正文过长，只读了前面部分' : ''}</div>
        <div class="row"><button id="dos-redo">重新生成</button></div>
        <div id="dos-ch" class="muted"></div>`)
      $('#dos-redo').onclick = async () => { await api(`/api/books/${bookId}/dossier`, { force: true }); show() }
      const ch = await api(`/api/books/${bookId}/chapters`).catch(() => null)
      if (ch) {
        $('#dos-ch').innerHTML = `各章导读：${ch.chapters ? `已生成 ${ch.done}/${ch.chapters} 章` : '还没生成'}${ch.pending ? `，${ch.pending} 章在后台批量生成（通常一小时内）` : ''}。
          打开「批」后显示在每章开头。` + (ch.pending || (ch.chapters && ch.done >= ch.chapters) ? '' :
          `<div class="row"><button id="dos-ch-go">生成各章导读（走批量，五折）</button></div>`)
        const b = $('#dos-ch-go')
        if (b) b.onclick = async () => { b.disabled = true; await api(`/api/books/${bookId}/chapters`, {}); show() }
      }
    } else if (d.state === 'running') {
      openSheet('全书导读', `<div class="muted">正在读整本书并联网核对（通常 5–8 分钟），可以先关掉接着读。</div>`)
      setTimeout(() => { if ($('#sheet').classList.contains('show') && $('#sheet-h').textContent === '全书导读') show() }, 10000)
    } else {
      openSheet('全书导读', `${d.state === 'error' ? `<div class="muted">上次失败：${esc(d.error)}</div>` : ''}
        <div class="muted">把整本书交给 Claude 读一遍，联网核书外事实，写出：来龙去脉、想干嘛、结构、独特之处、偏颇与争议、反响、和你的关系、留给你的问题。
        书外事实都附来源，查不到标「未核」。一本书只生成一次（一本 30 万字的英文书约 2–3 美元、5–6 分钟，走你配置的 Claude API key）。</div>
        <div class="row"><button class="primary" id="dos-go">生成导读</button></div>`)
      $('#dos-go').onclick = async () => { await api(`/api/books/${bookId}/dossier`, {}); show() }
    }
  }
  await show()
}

// ---- 安卓原生版的划线：按原文在本节里找位置，换成 cfi 再画 ----
// xpointer 的 DocFragment[k] 是那份 EPUB 的第 k 个 spine 文档；导读版多插了一页，序号可能错一位，
// 所以序号对上的节里任何长度都认，别的节里只认 8 个字以上的原文（太短容易认错地方）。
const xpPending = []
const xpResolved = new Map() // 划线事件号 -> cfi（笔记列表跳转用）
const inkPending = []
const inkResolved = new Map() // ink_id -> cfi（笔记列表跳转用）
function placeXpHighlights(doc, index) {
  if (!xpPending.length) return
  const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, {
    acceptNode: n => n.parentElement?.closest('.or-tr, .or-gl, .or-ink') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  })
  let flat = ''
  const map = [] // flat 里每个字 -> [节点, 节点内偏移]；去掉所有空白，两边换行/缩进不同也能对上
  for (let n; (n = walker.nextNode());) {
    const t = n.nodeValue
    for (let i = 0; i < t.length; i++) if (!/\s/.test(t[i])) { flat += t[i]; map.push([n, i]) }
  }
  for (let k = xpPending.length - 1; k >= 0; k--) {
    const h = xpPending[k]
    const needle = h.text.replace(/\s+/g, '')
    const frag = +(/DocFragment\[(\d+)\]/.exec(h.pos || '')?.[1] || 0)
    if (!needle || (frag - 1 !== index && needle.length < 8)) continue
    const at = flat.indexOf(needle)
    if (at < 0) continue
    const range = doc.createRange()
    const [sn, so] = map[at], [en, eo] = map[at + needle.length - 1]
    range.setStart(sn, so)
    range.setEnd(en, eo + 1)
    try {
      const cfi = view.getCFI(index, range)
      marks.set(cfi, { kind: 'hl', ...h, cfi })
      xpResolved.set(h.id, cfi)
      xpPending.splice(k, 1)
      view.addAnnotation({ value: cfi })
    } catch (e) { console.warn('xp highlight', e) }
  }
}

// ---- 安卓原生版的手写批注：按手写旁边的原文找到段落，在段落末尾画出笔迹和识别出的文字 ----
const INK_CSS = `.or-ink { display: block; margin: .4em 0 .2em; padding: .2em .6em; border-left: 2px solid ${desk ? '#a3302a' : '#000'};
  text-indent: 0; text-align: left; font-size: .9em; } .or-ink svg { display: block; max-width: 100%; height: auto; }
  .or-ink .or-ink-t { margin-top: .2em; } .or-ink .or-ink-l { font-size: .75em; opacity: .6; }`
function inkSVG(strokes, maxW = 420) {
  const xs = [], ys = []
  for (const s of strokes) for (let i = 0; i + 2 < s.length; i += 3) { xs.push(s[i]); ys.push(s[i + 1]) }
  if (!xs.length) return ''
  const pad = 6, x0 = Math.min(...xs) - pad, y0 = Math.min(...ys) - pad
  const w = Math.max(...xs) - x0 + pad, h = Math.max(...ys) - y0 + pad
  const k = Math.min(1, maxW / w)
  const paths = strokes.map(s => {
    let d = ''
    for (let i = 0; i + 2 < s.length; i += 3) d += `${i ? 'L' : 'M'}${(s[i] - x0).toFixed(1)} ${(s[i + 1] - y0).toFixed(1)}`
    return `<path d="${d}"/>`
  }).join('')
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w.toFixed(0)} ${h.toFixed(0)}" width="${(w * k).toFixed(0)}" height="${(h * k).toFixed(0)}"
    fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`
}
function placeInks(doc, index) {
  if (!inkPending.length) return
  if (!doc.getElementById('or-ink-css')) {
    const st = doc.createElement('style'); st.id = 'or-ink-css'; st.textContent = INK_CSS; doc.head?.append(st)
  }
  const blocks = trBlocks(doc).map(el => [el, rawText(el).replace(/\s+/g, '')])
  for (let k = inkPending.length - 1; k >= 0; k--) {
    const ink = inkPending[k]
    // 上下文是「锚点所在行 + 上下各一行」：先用中间那行（锚点行），再用其他行；太短的行不用
    const lines = (ink.context || '').split('\n').map(l => l.replace(/\s+/g, ''))
    const order = [lines[Math.floor(lines.length / 2)], ...lines].filter(l => l && l.length >= 6)
    let hit = null
    for (const line of order) { hit = blocks.find(([, t]) => t.includes(line))?.[0]; if (hit) break }
    if (!hit) continue
    const box = doc.createElement('div')
    box.className = 'or-ink'
    box.innerHTML = `<div class="or-ink-l">✎ Boox 手写</div>${inkSVG(ink.strokes)}<div class="or-ink-t">${
      ink.recognized ? esc(ink.recognized) : '<span style="opacity:.6">（尚未识别）</span>'}</div>`
    hit.append(box)
    try {
      const r = doc.createRange(); r.selectNodeContents(hit); r.collapse(true)
      inkResolved.set(ink.ink_id, view.getCFI(index, r))
    } catch (e) { console.warn('ink cfi', e) }
    inkPending.splice(k, 1)
  }
}

function onLoad({ detail: { doc, index } }) {
  trDocs.set(index, doc)
  placeXpHighlights(doc, index)
  placeInks(doc, index)
  $('#b-tr').classList.toggle('on', trOn)
  trSection(doc, index)
  gloss.section(doc, index)
  if (isPDF) {
    docIndex.set(doc, index)
    drawPDFMarks(doc, index)
    schedulePDFRedraw()
  }
  doc.addEventListener('keydown', onKey)
  let timer
  doc.addEventListener('selectionchange', () => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      const sel = doc.getSelection()
      const text = sel?.toString().trim()
      if (sel && sel.rangeCount && text) {
        current = { doc, index, range: sel.getRangeAt(0).cloneRange(), text }
        $('#selbar').classList.add('show')
      } else if (!$('#sheet').classList.contains('show')) {
        $('#selbar').classList.remove('show')
      }
    }, 350)
  })
  doc.addEventListener('click', e => {
    if (e.target.closest?.('a')) return
    // stops Chrome's "Touch to Search" panel from opening on page-turn taps
    if (!doc.getSelection()?.toString().trim()) e.preventDefault()
    setTimeout(() => {
      if (performance.now() - lastAnnoClick < 500) return
      if (doc.getSelection()?.toString().trim()) return
      if ($('#sheet').classList.contains('show')) return
      if (isPDF) {
        const m = hitMark(doc, e)
        if (m) { lastAnnoClick = performance.now(); showMark(m); return }
      }
      const x = (e.screenX - (window.screenX || 0)) / window.innerWidth
      if (x < 0.3) view.goLeft()
      else if (x > 0.7) view.goRight()
      else $('#top').classList.toggle('show')
    }, 0)
  })
}

let saveTimer, lastSaved, pendingDetail
function writeProgress(detail) {
  if (!detail?.cfi || detail.cfi === lastSaved) return
  lastSaved = detail.cfi
  // Kimi-6: 这条 fire-and-forget 的写以前没有 catch，服务不可时是未处理 rejection、进度静默丢失
  logEvent('progress', { cfi: detail.cfi, payload: { fraction: detail.fraction, chapter: detail.tocItem?.label } })
    .catch(saveFail)
}
function onRelocate({ detail }) {
  if (isPDF) schedulePDFRedraw()
  $('#chap').textContent = detail.tocItem?.label ?? ''
  $('#pct').textContent = detail.fraction != null ? `${Math.round(detail.fraction * 100)}%` : ''
  pendingDetail = detail // Kimi-6: 留住最后一次位置，防抖没到点也能补发
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => { saveTimer = null; writeProgress(pendingDetail) }, 3000)
}
// Kimi-6: 关页面/退回书架/退 App 前，把还在 3 秒防抖里的最后一次进度立刻补发
function flushProgress() {
  if (!saveTimer) return
  clearTimeout(saveTimer)
  saveTimer = null
  writeProgress(pendingDetail)
}
window.addEventListener('pagehide', flushProgress)
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushProgress() })

function selectionContext(sel) {
  const { doc, range } = sel
  const before = doc.createRange()
  before.setStart(doc.body, 0)
  before.setEnd(range.startContainer, range.startOffset)
  const after = doc.createRange()
  after.setStart(range.endContainer, range.endOffset)
  after.setEnd(doc.body, doc.body.childNodes.length)
  return { before: before.toString(), after: after.toString() }
}

function clearSelection() {
  current?.doc.getSelection()?.removeAllRanges()
  $('#selbar').classList.remove('show')
}

function openSheet(title, html) {
  $('#sheet-h').textContent = title
  $('#sheet-body').innerHTML = html
  $('#sheet-body').onclick = null
  $('#sheet').classList.add('show')
  $('#selbar').classList.remove('show')
}
const closeSheet = () => $('#sheet').classList.remove('show')

// Kimi-3: 写入（划线/评注/删除/反馈）失败必须看得见。#foot 常驻底部、两种主题下都可读。
let failTimer
function saveFail(e) {
  $('#savefail').textContent = `没存上：${e.message || e}`
  clearTimeout(failTimer)
  failTimer = setTimeout(() => { $('#savefail').textContent = '' }, 8000)
  console.error(e)
}

async function addHighlight(sel) {
  const cfi = view.getCFI(sel.index, sel.range)
  const existing = marks.get(cfi)
  if (existing?.kind === 'hl') return existing
  const { id } = await logEvent('highlight', { cfi, text: sel.text, payload: { color: 'gray' } })
  const m = { kind: 'hl', id, cfi, text: sel.text, note: null }
  marks.set(cfi, m)
  if (isPDF) { resolveMark(cfi); redrawPDF() }
  else view.addAnnotation({ value: cfi })
  return m
}

$('#s-hl').onclick = async () => {
  if (!current) return
  try { await addHighlight(current) } catch (e) { saveFail(e); return } // Kimi-3: 失败就别清选区、给提示
  clearSelection()
}

$('#s-note').onclick = () => {
  if (!current) return
  const sel = current
  openSheet('评注', `<div class="quote">${esc(sel.text)}</div>
    <textarea id="note-text" rows="6" placeholder="写下你的想法"></textarea>
    <div class="row"><button class="primary" id="note-save">保存</button></div>`)
  $('#note-save').onclick = async () => {
    const note = $('#note-text').value.trim()
    if (!note) return
    try {
      const m = await addHighlight(sel)
      await logEvent('note', { cfi: m.cfi, text: note, payload: { highlight_id: m.id } })
      m.note = note
    } catch (e) { saveFail(e); return } // Kimi-3: 两步写入任一步失败都提示；sheet 不关，原样可重试（addHighlight 认已存在的划线，不会重复）
    sel.doc.getSelection()?.removeAllRanges()
    closeSheet()
  }
}

$('#s-ask').onclick = () => {
  if (!current) return
  const sel = current
  const cfi = view.getCFI(sel.index, sel.range)
  const chapter = view.lastLocation?.tocItem?.label ?? ''
  openSheet('提问', `<div class="quote">${esc(sel.text)}</div>
    <textarea id="ask-q" rows="3" placeholder="想问什么？留空则请它解释这段话"></textarea>
    <div class="row"><button class="primary" id="ask-go">提问</button></div>
    <div id="ask-out"></div>`)
  let asking = false // Kimi-2: 一次只允许一个请求在跑（#pending 和 #ask-go 都是单实例假设）
  const run = async (backend, upgradeOf) => {
    if (asking) return
    asking = true
    const question = $('#ask-q').value.trim()
    const out = $('#ask-out')
    const label = LABELS[backend] || 'AI'
    let ctx = selectionContext(sel)
    if (isPDF) {
      // PDF 的前后文：当前页 + 前后各一页的文字层
      try {
        const d = await pdfDocForText()
        const page = (docIndex.get(sel.doc) ?? 0) + 1
        const [prev, next] = await Promise.all([pageText(d, page - 1), pageText(d, page + 1)])
        ctx = { before: (prev ? prev + '\n' : '') + ctx.before,
                after: ctx.after + (next ? '\n' + next : '') }
      } catch (e) { console.error(e) }
    }
    out.insertAdjacentHTML('beforeend', `<div class="item" id="pending"><span class="muted">${label}思考中……</span></div>`)
    try {
      const r = await api('/api/ask', {
        device, book_id: bookId, cfi, selection: sel.text, question, chapter,
        context_before: ctx.before, context_after: ctx.after, backend, upgrade_of: upgradeOf,
      })
      $('#pending').remove()
      if (!marks.has(cfi)) {
        marks.set(cfi, { kind: 'ask', id: r.id, cfi, selection: sel.text, question, answer: r.answer, model: r.model })
        if (isPDF) { resolveMark(cfi); redrawPDF() }
        else view.addAnnotation({ value: cfi })
      }
      out.insertAdjacentHTML('beforeend', `<div class="item">
        <div class="muted">${esc(LABELS[r.backend] || label)} · ${esc(r.model)} · ${(r.latency_ms / 1000).toFixed(0)} 秒${r.note ? ' · ' + esc(r.note) : ''}</div>
        <div class="answer">${md(r.answer)}</div>
        <div class="row">
          <button data-fb="1" data-id="${r.id}">有用</button>
          <button data-fb="-1" data-id="${r.id}">不满意</button>
        </div>
        <div class="muted">换个模型重问：</div>
        <div class="row models">
          ${UPGRADES.filter(b => b !== (r.backend || backend)).map(b => `<button data-up="${b}" data-id="${r.id}">${LABELS[b]}</button>`).join('')}
        </div></div>`)
    } catch (e) {
      $('#pending')?.remove()
      out.insertAdjacentHTML('beforeend', `<div class="item">出错了：${esc(e.message)}
        <div class="row"><button data-up="${backend || ''}">重试</button></div></div>`)
    } finally { // Kimi-2: 成功/失败都要解锁，并且只在真的跑完之后才允许再提问
      asking = false
      $('#ask-go').disabled = false
    }
  }
  $('#ask-go').onclick = () => { $('#ask-go').disabled = true; run(undefined) } // 不指定后端：服务器用 OWN_READER_DEFAULT_BACKEND
  $('#ask-out').onclick = async e => {
    const b = e.target.closest('button')
    if (!b) return
    if (b.dataset.fb) {
      try { await logEvent('feedback', { payload: { ask_id: Number(b.dataset.id), rating: Number(b.dataset.fb) } }) }
      catch (e) { saveFail(e); return } // Kimi-3: 同一条写入链路的另一处（C-3 未点名，但失败模式一样）
      b.parentElement.innerHTML = `<span class="muted">已记录：${b.dataset.fb === '1' ? '有用' : '不满意'}</span>`
    } else if (b.dataset.up !== undefined) {
      if (asking) return // Kimi-2: 已经有一个在跑，就别把按钮按死、也别再起一个
      b.disabled = true
      run(b.dataset.up || undefined, b.dataset.id ? Number(b.dataset.id) : undefined)
    }
  }
}

// 地理批注（2026-10-09）：地名 → 历史地名库定位 → 地图 + 事实表 + 形势解说。存成 mode=geo 的提问记录。
$('#s-geo').onclick = async () => {
  if (!current) return
  const sel = current
  const cfi = view.getCFI(sel.index, sel.range)
  const chapter = view.lastLocation?.tocItem?.label ?? ''
  const ctx = selectionContext(sel)
  openSheet('地理批注', `<div class="quote">${esc(sel.text)}</div>
    <div id="geo-out"><div class="item"><span class="muted">正在抽地名、查历史地名库、写解说（约一分钟）……</span></div></div>`)
  try {
    const r = await api('/api/geo', { device, book_id: bookId, cfi, selection: sel.text, chapter,
      context_before: ctx.before, context_after: ctx.after }) // 不指定后端：服务器用 OWN_READER_DEFAULT_BACKEND
    const geo = { year: r.year, places: r.places }
    if (!marks.has(cfi)) {
      marks.set(cfi, { kind: 'ask', id: r.id, cfi, selection: sel.text, question: '地理批注', answer: r.answer, model: r.model, geo })
      if (isPDF) { resolveMark(cfi); redrawPDF() }
      else view.addAnnotation({ value: cfi })
    }
    const out = $('#geo-out')
    if (!out) return // 等待期间 sheet 已关
    out.innerHTML = `${geoMapHTML()}<div class="answer">${md(r.answer)}</div>
      <div class="muted">${esc(r.model)} · ${(r.latency_ms / 1000).toFixed(0)} 秒</div>`
    renderGeoMap(out, geo)
  } catch (e) {
    const out = $('#geo-out')
    if (out) out.innerHTML = `<div class="item">出错了：${esc(e.message)}</div>`
  }
}

function showMark(m) {
  if (m.kind === 'ask' && m.geo) {
    openSheet('地理批注', `<div class="quote">${esc(m.selection)}</div>
      <div id="geo-out">${geoMapHTML()}<div class="answer">${md(m.answer)}</div><div class="muted">${esc(m.model)}</div></div>`)
    renderGeoMap($('#geo-out'), m.geo)
  } else if (m.kind === 'ask') {
    openSheet('之前的提问', `<div class="quote">${esc(m.selection)}</div>
      ${m.question ? `<div class="q">${esc(m.question)}</div>` : ''}
      <div class="muted">${esc(m.model)}</div><div class="answer">${md(m.answer)}</div>`)
  } else {
    openSheet('划线', `<div class="quote">${esc(m.text)}</div>
      ${m.note ? `<div class="answer">${esc(m.note)}</div>` : ''}
      <div class="row"><button id="hl-del">删除这条划线</button></div>`)
    $('#hl-del').onclick = async () => {
      try { await logEvent('delete', { cfi: m.cfi, payload: { target_id: m.id } }) }
      catch (e) { saveFail(e); return } // Kimi-3: 没删成别让本地状态先减掉
      marks.delete(m.cfi)
      if (isPDF) { pdfResolvers.delete(m.cfi); redrawPDF() }
      else view.deleteAnnotation({ value: m.cfi })
      closeSheet()
    }
  }
}

if (desk) {
  $('#hotzone').addEventListener('mouseenter', () => $('#top').classList.add('show'))
  $('#top').addEventListener('mouseleave', () => $('#top').classList.remove('show'))
}

// 服务器中文字体的显示名（字体文件里的 family 是英文）
const ZH_LABEL = { 'LXGW WenKai Lite': '霞鹜文楷 轻便版', 'LXGW WenKai Screen': '霞鹜文楷 屏幕版（墨水屏推荐）',
  'LXGW Neo ZhiSong': '霞鹜新致宋', 'Noto Serif SC': '思源宋体', 'Noto Sans CJK SC': '思源黑体', 'Zhuque Fangsong': '朱雀仿宋' }
$('#b-font').onclick = () => {
  const opts = zh => [...(desk ? MAC_FONTS : []), ...serverFonts.map(f => ({ family: f.family, label: ZH_LABEL[f.family] || f.family, zh: f.zh }))]
    .filter(f => !!f.zh === zh)
  const sel = (id, list, val) => `<select id="${id}">${list.map(f =>
    `<option value="${esc(f.family)}"${f.family === val ? ' selected' : ''}>${esc(f.label)}</option>`).join('')}</select>`
  openSheet('排版', `
    <label class="field">中文字体${sel('t-zh', opts(true), typo.zh)}</label>
    <label class="field">英文字体${sel('t-en', opts(false), typo.en)}</label>
    <label class="field">字号 <span id="t-size-v">${typo.size}%</span><input id="t-size" type="range" min="80" max="160" step="2" value="${typo.size}"></label>
    <label class="field">行距 <span id="t-line-v">${typo.line}</span><input id="t-line" type="range" min="1.3" max="2.4" step="0.05" value="${typo.line}"></label>
    <label class="field">加黑 <span id="t-bold-v">${typo.bold || 0}</span><input id="t-bold" type="range" min="0" max="0.8" step="0.1" value="${typo.bold || 0}"></label>
    <p class="muted">设置只存在这台设备上。英文字母用英文字体，汉字回退到中文字体。墨水屏第一次换字体要下载字体文件（中文约 12MB）。</p>`)
  const save = () => {
    Object.assign(typo, { zh: $('#t-zh').value, en: $('#t-en').value, size: Number($('#t-size').value), line: Number($('#t-line').value), bold: Number($('#t-bold').value) })
    $('#t-size-v').textContent = typo.size + '%'
    $('#t-line-v').textContent = typo.line
    $('#t-bold-v').textContent = typo.bold
    try { localStorage.setItem(TYPO_KEY, JSON.stringify(typo)) } catch {}
    applyStyles()
  }
  for (const id of ['t-zh', 't-en', 't-size', 't-line', 't-bold']) $('#' + id).onchange = save
}

$('#sheet-x').onclick = () => { closeSheet(); clearSelection() }
$('#b-home').onclick = () => { location.href = '/' }
$('#b-toc').onclick = () => {
  const items = []
  const walk = (list, depth) => {
    for (const t of list ?? []) {
      items.push(`<div class="item" data-href="${esc(t.href)}" style="padding-left:${depth * 18}px">${esc(t.label)}</div>`)
      walk(t.subitems, depth + 1)
    }
  }
  walk(view.book.toc, 0)
  openSheet('目录', items.join('') || '<p class="muted">这本书没有目录</p>')
  $('#sheet-body').onclick = e => {
    const href = e.target.closest('[data-href]')?.dataset.href
    if (href) { view.goTo(href); closeSheet(); $('#top').classList.remove('show') }
  }
}

// 书内搜索：用 foliate-js 的 view.search()，结果列出章节名+前后 30 字片段，点击跳转（命中文保持高亮）
const MAX_SEARCH_RESULTS = 200
let searchRun = null
const srSnippet = ex => {
  const pre = String(ex?.pre ?? ''), post = String(ex?.post ?? '')
  const cutPre = pre.slice(-30), cutPost = post.slice(0, 30)
  return `${pre.length > cutPre.length ? '…' : ''}${esc(cutPre)}<b>${esc(ex?.match ?? '')}</b>${esc(cutPost)}${post.length > cutPost.length ? '…' : ''}`
}

async function runSearch(raw) {
  const q = raw.trim()
  if (!view || !q) return
  if (searchRun) searchRun.cancel = true
  const my = searchRun = { cancel: false }
  const list = $('#sr-list'), status = $('#sr-status')
  if (!list) return
  list.innerHTML = ''
  status.textContent = '搜索中…'
  let count = 0
  try {
    for await (const r of view.search({ query: q, draw: Overlayer.highlight,
      drawOptions: { color: desk ? 'rgba(163, 48, 42, .25)' : '#c8c8c8' } })) {
      if (my.cancel) return
      if (r === 'done' || !r.subitems) continue
      for (const it of r.subitems) {
        count++
        if (count <= MAX_SEARCH_RESULTS) list.insertAdjacentHTML('beforeend',
          `<div class="item sr-item" data-cfi="${esc(it.cfi)}"><div class="muted">${esc(r.label) || '正文'}</div>
           <div class="snip">${srSnippet(it.excerpt)}</div></div>`)
      }
      status.textContent = `已找到 ${count} 处……`
    }
    if (!my.cancel) status.textContent = count ? `共 ${count} 处${count > MAX_SEARCH_RESULTS ? `（只列前 ${MAX_SEARCH_RESULTS}）` : ''}` : '没有找到'
  } catch {
    if (!my.cancel) status.textContent = '搜索失败'
  }
}

$('#b-search').onclick = () => {
  openSheet('书内搜索', `<div id="sr-bar"><input id="sr-q" placeholder="输入要查找的文字" autocomplete="off">
    <button id="sr-go" class="primary">搜索</button></div>
    <div id="sr-status" class="muted"></div><div id="sr-list"></div>`)
  const input = $('#sr-q')
  input.focus()
  $('#sr-go').onclick = () => runSearch(input.value)
  input.onkeydown = e => { if (e.key === 'Enter') runSearch(input.value) }
  let t
  input.oninput = () => { clearTimeout(t); if (input.value.trim().length >= 2) t = setTimeout(() => runSearch(input.value), 600) }
  $('#sr-list').onclick = e => {
    const cfi = e.target.closest('[data-cfi]')?.dataset.cfi
    if (cfi) { view.goTo(cfi); closeSheet(); $('#top').classList.remove('show') }
  }
}
$('#b-notes').onclick = async () => {
  const s = await api(`/api/books/${bookId}/state`)
  const rows = [
    ...s.highlights.map(h => ({ ts: h.ts, cfi: h.cfi || xpResolved.get(h.id) || '', html: `${h.pos_kind === 'crengine' ? '<div class="muted">Boox</div>' : ''}<div class="quote">${esc(h.text)}</div>${h.note ? `<div>${esc(h.note)}</div>` : ''}` })),
    ...(s.inks || []).map(k => ({
      ts: k.ts,
      cfi: inkResolved.get(k.ink_id) || '',
      html: `<div class="muted">Boox 手写${k.chapter ? ' · ' + esc(k.chapter) : ''}</div>${inkSVG(k.strokes, 260)}<div>${k.recognized ? esc(k.recognized) : '<span class="muted">（尚未识别）</span>'}</div>`,
    })),
    ...s.asks.map(a => ({ ts: a.ts, cfi: a.cfi, html: `<div class="quote">${esc(a.selection)}</div><div class="q">问：${esc(a.question || '解释这段')}</div><div class="answer">${md(a.answer)}</div><div class="muted">${esc(a.model)}</div>` })),
    ...(s.imported || []).map(n => ({ ts: n.created_ts || '', cfi: '', html: `<div class="muted">${n.source === 'weread' ? '微信读书' : esc(n.source)}${n.chapter ? ' · ' + esc(n.chapter) : ''}</div>${n.quote ? `<div class="quote">${esc(n.quote)}</div>` : ''}${n.text ? `<div class="answer">${md(n.text)}</div>` : ''}` })),
  ].sort((a, b) => b.ts.localeCompare(a.ts))
  openSheet(`笔记（${rows.length}）`, rows.map(r => `<div class="item" data-cfi="${esc(r.cfi)}">${r.html}</div>`).join('') || '<p class="muted">还没有划线和提问</p>')
  $('#sheet-body').onclick = e => {
    const cfi = e.target.closest('[data-cfi]')?.dataset.cfi
    if (cfi) { view.goTo(cfi); closeSheet(); $('#top').classList.remove('show') }
  }
}
document.addEventListener('keydown', onKey)

main().catch(e => {
  document.body.insertAdjacentHTML('beforeend', `<p style="padding:20px">打开失败：${esc(e.message)}</p>`)
  console.error(e)
})
