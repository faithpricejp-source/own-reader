// LLM 回答与笔记的 Markdown 渲染安全配置（reader.html / index.html 共用，须在 marked.min.js 之后加载）
// 原始 HTML 一律丢弃；链接与图片只放行 http/https，其余协议的链接只留文字、图片整个去掉
;(() => {
  const m = globalThis.marked
  if (!m) return
  const safe = h => /^https?:\/\//i.test(String(h ?? '').trim())
  m.use({
    renderer: {
      html: () => '',
      link(t) { return safe(t.href) ? false : this.parser.parseInline(t.tokens) },
      image(t) { return safe(t.href) ? false : '' },
    },
  })
})()
