// 在 <head> 里同步执行，先定主题再渲染，避免闪一下。
// desk = 发光屏（Mac App / 桌面浏览器）：暖纸色、封面墙、阴影；eink = 墨水屏（安卓设备）：纯黑白、无动画。
// 手动覆盖：localStorage own-reader-theme = desk | eink（Mac App 菜单「显示 → 墨水屏模式」切换）。
(() => {
  let t = null
  try { t = localStorage.getItem('own-reader-theme') } catch {}
  if (t !== 'desk' && t !== 'eink') t = /Android/.test(navigator.userAgent) ? 'eink' : 'desk'
  document.documentElement.dataset.theme = t
  window.OWN_READER_THEME = t
  // 大屏墨水屏（如 13.3 寸、CSS 宽约 1245px）：界面整体放大。localStorage own-reader-large = 1/0 可手动覆盖。
  let large = null
  try { large = localStorage.getItem('own-reader-large') } catch {}
  if (large !== '1' && large !== '0') large = t === 'eink' && Math.min(screen.width, screen.height) >= 1000 ? '1' : '0'
  if (large === '1') document.documentElement.dataset.large = '1'
  window.OWN_READER_LARGE = large === '1'
})()
