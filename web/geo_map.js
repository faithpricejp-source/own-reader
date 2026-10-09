// 地理批注的地图（2026-10-09）。Leaflet 由 reader.html 以普通脚本载入（全局 L）。
// 底图：OpenTopoMap（有等高线和今地名）/ Esri 晕渲地形（无文字，看山川走势不被今地名干扰）。
// 点：① 地名库定位 = 实心；② 模型给出今地 = 空心虚线（点位是今县中心，不是遗址）。③④ 不上图。
// 府界多边形：CHGIS 有该年代府界时画虚线框。

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))

export function geoMapHTML() {
  return `<div class="geo-map"></div>
    <div class="muted geo-legend">● 地名库定位　○ 模型给出今地（点在今县中心，非遗址）　虚线框：该年代府界（CHGIS）</div>`
}

export async function renderGeoMap(host, geo) {
  const el = host.querySelector('.geo-map')
  const pts = (geo?.places || []).filter(p => p.level <= 2 && p.x != null)
  if (!el || !globalThis.L) return
  if (!pts.length) { el.remove(); host.querySelector('.geo-legend').textContent = '没有可以上图的地点（见下表）。'; return }
  const L = globalThis.L
  const eink = document.documentElement.dataset.theme === 'eink'
  const topo = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
    maxZoom: 15, subdomains: 'abc',
    attribution: '© OpenStreetMap contributors, SRTM | © OpenTopoMap (CC-BY-SA)' })
  const relief = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}', {
    maxZoom: 13, attribution: 'Tiles © Esri' })
  const map = L.map(el, { layers: [topo], zoomSnap: 0.5, attributionControl: true })
  L.control.layers({ '地形（等高线）': topo, '晕渲（无今地名）': relief }, null, { collapsed: true }).addTo(map)
  const ink = eink ? '#000' : '#a3302a'
  const group = L.featureGroup().addTo(map)
  for (const p of pts) {
    const solid = p.level === 1
    L.circleMarker([p.y, p.x], {
      radius: solid ? 7 : 8, color: ink, weight: 2, dashArray: solid ? null : '3 3',
      fillColor: ink, fillOpacity: solid ? 0.85 : 0,
    }).bindTooltip(esc(p.name), { permanent: true, direction: 'right', offset: [8, 0], className: 'geo-tip' })
      .bindPopup(`<b>${esc(p.name)}</b><br>${esc(p.today || '')}<br><span style="color:#777">${esc(p.src || '')}</span>`)
      .addTo(group)
  }
  const ids = pts.map(p => p.pgn).filter(Boolean)
  if (ids.length) {
    try {
      const r = await fetch(`/api/geo/pgn?ids=${encodeURIComponent(ids.join(','))}`).then(r => r.json())
      for (const g of r.polygons || [])
        L.geoJSON(g.geojson, { style: { color: ink, weight: 1.5, dashArray: '6 4', fillOpacity: 0.04 } })
          .bindTooltip(esc(g.name)).addTo(group)
    } catch (e) { console.error(e) }
  }
  // sheet 刚显示时容器尺寸才确定
  requestAnimationFrame(() => {
    map.invalidateSize()
    if (pts.length === 1) map.setView([pts[0].y, pts[0].x], 8)
    else map.fitBounds(group.getBounds(), { padding: [30, 30], maxZoom: 10 })
  })
  return map
}
