// 精读批注（2026-10-09）：背景/释义/存疑/双关/言外，追加在原文段落末尾（.or-gl，与对照翻译 .or-tr 同法，
// 不改变原文节点，划线 CFI 不受影响；view.js 的 getCFI 会把落在 .or-gl 里的位置挪到它前面）。
// 每本书单独开关（默认关）；开着时每节载入就批该节、顺带预取下一节；批过的段落服务器永久缓存。

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
export const GL_SEL = 'p, li, blockquote, h1, h2, h3, h4, h5, h6, dd, dt, figcaption, div'
// 段落原文：去掉我们插进去的译文与批注（否则哈希变了、还会把批注当原文再批一遍）
export const rawText = el => {
  if (!el.querySelector('.or-tr, .or-gl, .or-ink')) return el.textContent.replace(/\s+/g, ' ').trim()
  const c = el.cloneNode(true)
  c.querySelectorAll('.or-tr, .or-gl, .or-ink').forEach(n => n.remove())
  return c.textContent.replace(/\s+/g, ' ').trim()
}
export const glBlocks = doc => [...doc.querySelectorAll(GL_SEL)]
  .filter(el => !el.querySelector(GL_SEL) && !el.closest('.or-tr, .or-gl, .or-ink') && rawText(el).length > 1)

const css = desk => `.or-gl { display: block; margin: .35em 0 .2em; padding: .25em .6em; font-size: .86em; line-height: 1.55;
  text-indent: 0; text-align: left; border-left: 2px solid ${desk ? '#c9a27a' : '#000'};
  ${desk ? 'background: rgba(201,162,122,.08); color: #5b5047;' : 'color: #000;'} }
  .or-gl-i { display: block; margin: .15em 0; }
  .or-gl-t { font-weight: 700; margin-right: .3em; } .or-gl-q { font-style: italic; opacity: .8; }
  .or-gl-c { font-size: .85em; opacity: .65; }
  .or-gl.or-gl-fold { display: inline; border: none; background: none; padding: 0 0 0 .3em; margin: 0;
    font-size: .72em; opacity: .75; cursor: pointer; ${desk ? 'color: #a3302a;' : 'text-decoration: underline;'} }
  .or-gl-ch { display: block; margin: .2em 0 .6em; } .or-gl-ch h3 { font-size: 1em; margin: .6em 0 .2em; }
  .or-gl-ch p, .or-gl-ch ul, .or-gl-ch ol { margin: .2em 0; }
  ${desk ? '@media (prefers-color-scheme: dark) { .or-gl { color: #c9bfb3; background: rgba(201,162,122,.10); } }' : ''}`

export function createGloss({ api, bookId, desk, getView, relayout, isPDF, openSheet }) {
  const KEY = `own-reader-gl-${bookId}`
  const MODE_KEY = 'own-reader-gl-mode'
  let on = (() => { try { return localStorage.getItem(KEY) === 'on' } catch { return false } })()
  // 显示方式：展开（批注整框插在段末）/ 折叠（段末一行小标记，点开在底部看）。墨水屏和窄屏默认折叠（展开太挤）
  let mode = (() => { try { return localStorage.getItem(MODE_KEY) } catch { return null } })()
    || (document.documentElement.dataset.theme === 'eink' || window.innerWidth < 760 ? 'fold' : 'full')
  const mdR = t => globalThis.marked ? globalThis.marked.parse(String(t ?? '')) : esc(t)
  const itemsHTML = items => items.filter(it => it.type === '本章').map(it =>
      `<div class="or-gl-ch"><b>本章导读</b>${mdR(it.note)}</div>`).join('') +
    items.filter(it => it.type !== '本章').map(it => `<p><b>${esc(it.type)}</b> <i>${esc(it.quote)}</i> — ${esc(it.note)}` +
      (it.type === '存疑' || it.conf === '低' ? ` <span class="muted">〔模型判断${it.conf ? '，把握' + esc(it.conf) : ''}〕</span>` : '') + `</p>`).join('')
  const docs = new Map()
  const fold = new WeakMap()  // 折叠标记 → 这一段的批注
  const prefetched = new Set()
  let lastError = null

  function insert(els, hashes, done) {
    let changed = false
    els.forEach((el, i) => {
      const items = done[hashes[i]]
      if (!items?.length || el.querySelector(':scope > .or-gl')) return
      const box = el.ownerDocument.createElement('span')
      box.className = 'or-gl'
      box.lang = 'zh-CN'
      if (mode === 'fold') {
        const n = {}
        for (const it of items) if (it.type !== '本章') n[it.type] = (n[it.type] || 0) + 1
        const parts = Object.entries(n).map(([t, c]) => c > 1 ? `${t}${c}` : t)
        box.className = 'or-gl or-gl-fold'
        box.textContent = (items.some(it => it.type === '本章') ? '〔本章导读〕' : '') + (parts.length ? `〔批：${parts.join(' ')}〕` : '')
        fold.set(box, items)
        el.append(box)
        changed = true
        return
      }
      // 本章导读（Markdown，放最前）与逐段批注同框显示
      const md = t => globalThis.marked ? globalThis.marked.parse(String(t ?? '')) : esc(t)
      const chap = items.filter(it => it.type === '本章')
      const rest = items.filter(it => it.type !== '本章')
      box.innerHTML = chap.map(it => `<span class="or-gl-ch"><span class="or-gl-t">本章导读</span>${md(it.note)}</span>`).join('') + rest.map(it => `<span class="or-gl-i"><span class="or-gl-t">${esc(it.type)}</span>` +
        `<span class="or-gl-q">${esc(it.quote)}</span> — ${esc(it.note)}` +
        (it.type === '存疑' || it.conf === '低' ? ` <span class="or-gl-c">〔模型判断${it.conf ? '，把握' + esc(it.conf) : ''}〕</span>` : '') +
        `</span>`).join('')
      el.append(box)
      changed = true
    })
    if (changed) relayout()
  }

  async function section(doc, index) {
    docs.set(index, doc)
    if (!doc.__glClick) {  // 点折叠标记：同步打开底部面板，阅读页的点击翻页会因面板已打开而跳过
      doc.__glClick = true
      doc.addEventListener('click', e => {
        const b = e.target.closest?.('.or-gl-fold')
        if (b && fold.has(b)) openSheet('批注', itemsHTML(fold.get(b)))
      })
    }
    if (!on || isPDF()) return
    if (!doc.getElementById('or-gl-css')) {
      const st = doc.createElement('style')
      st.id = 'or-gl-css'
      st.textContent = css(desk)
      doc.head?.append(st)
    }
    const els = glBlocks(doc)
    if (!els.length) { prefetch(index + 1); return } // 封面、插图页：直接预取下一节
    let r
    try { r = await api('/api/gloss', { book_id: bookId, paras: els.map(rawText) }) } catch (e) { lastError = e.message; return }
    insert(els, r.hashes, r.done)
    prefetch(index + 1)
    let pending = r.pending, tries = 0
    while (pending && on && docs.get(index) === doc && tries++ < 200) {
      await new Promise(res => setTimeout(res, 4000))
      let g
      try { g = await api('/api/gloss/get', { hashes: r.hashes }) } catch { continue }
      insert(els, r.hashes, g.done)
      pending = g.pending
      lastError = g.error
    }
  }

  async function prefetch(index) {
    const sec = getView()?.book?.sections?.[index]
    if (!sec?.createDocument || prefetched.has(index)) return
    prefetched.add(index)
    try {
      const doc = await sec.createDocument()
      const paras = glBlocks(doc).map(rawText)
      if (paras.length) await api('/api/gloss', { book_id: bookId, paras })
    } catch {}
  }

  function setOn(v) {
    on = v
    try { localStorage.setItem(KEY, on ? 'on' : 'off') } catch {}
    for (const [index, doc] of docs) {
      if (on) section(doc, index)
      else doc.querySelectorAll('.or-gl').forEach(n => n.remove())
    }
    if (!on) relayout()
  }

  // 「批」按钮打开的设置面板：开关、整本预跑、读者画像
  async function panelHTML() {
    let st = null, prof = ''
    try { st = await api(`/api/gloss/book/${bookId}`) } catch {}
    try { prof = (await api('/api/gloss/profile')).text } catch {}
    let tr = null
    try { tr = await api(`/api/books/${bookId}/bilingual/status?kick=0`) } catch {}
    return `<div class="muted">按「对你」可能看不懂的地方批：背景、释义、存疑（作者可能错或有争议）、双关、言外之意。
      引用必须逐字出自原文，没把握就不批。批注插在段落下方，批过的段落永久缓存。</div>
      <div class="row"><button id="gl-toggle" class="${on ? '' : 'primary'}">${on ? '关闭本书批注' : '打开本书批注（读到哪批到哪）'}</button></div>
      <div class="row"><button id="gl-mode">显示方式：${mode === 'fold' ? '折叠（点标记看）' : '展开在段落下'} — 点此切换</button></div>
      ${st ? `<div class="muted">本书已批 ${st.done} / ${st.total} 段${st.running ? '，整本预跑中' : ''}${st.error ? '；' + esc(st.error) : ''}</div>
      <div class="row"><button id="gl-book">${st.running ? '停止整本预跑' : '整本预跑（后台批完全书）'}</button></div>` : ''}
      ${tr && tr.lang !== 'zh' ? `<div class="muted" style="margin-top:12px">对照翻译：已译 ${tr.done} / ${tr.total} 段。
        整本批量翻译走 Claude API 批量（五折，通常一小时内，约每 10 万词 1–2 美元）。</div>
        <div class="row"><button id="tr-book">整本批量翻译</button></div>` : ''}
      ${lastError ? `<div class="muted">最近一次出错：${esc(lastError)}</div>` : ''}
      <div class="muted" style="margin-top:12px">读者画像（模型据此判断你可能不懂什么，可改）：</div>
      <textarea id="gl-prof" rows="8" style="width:100%">${esc(prof)}</textarea>
      <div class="row"><button id="gl-prof-save">保存画像</button></div>`
  }

  function bindPanel(root, rerender) {
    root.querySelector('#gl-toggle').onclick = () => { setOn(!on); rerender() }
    root.querySelector('#gl-mode').onclick = () => {
      mode = mode === 'fold' ? 'full' : 'fold'
      try { localStorage.setItem(MODE_KEY, mode) } catch {}
      for (const [index, doc] of docs) { doc.querySelectorAll('.or-gl').forEach(n => n.remove()); if (on) section(doc, index) }
      relayout(); rerender()
    }
    const b = root.querySelector('#gl-book')
    if (b) b.onclick = async () => {
      b.disabled = true
      const running = b.textContent.startsWith('停止')
      try { await api(`/api/gloss/book/${bookId}/${running ? 'stop' : 'start'}`, {}) } catch (e) { lastError = e.message }
      rerender()
    }
    const t = root.querySelector('#tr-book')
    if (t) t.onclick = async () => {
      t.disabled = true
      try { const r = await api(`/api/books/${bookId}/translate_book`, {}); t.textContent = r.blocks ? `已提交 ${r.blocks} 块，预估 ${r.est} 美元` : (r.note || '没有要翻的段落') }
      catch (e) { t.textContent = '提交失败：' + e.message }
    }
    root.querySelector('#gl-prof-save').onclick = async e => {
      try { await api('/api/gloss/profile', { text: root.querySelector('#gl-prof').value }); e.target.textContent = '已保存' }
      catch (err) { e.target.textContent = '保存失败：' + err.message }
    }
  }

  return { section, setOn, isOn: () => on, panelHTML, bindPanel }
}
