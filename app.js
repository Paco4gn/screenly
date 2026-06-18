const state = {
  hosts: [],
  results: [],
  filter: 'all',
  source: 'file',
  search: '',
  screen: 'all',
  sort: 'playlist',
  selected: new Set(),
  nowPlaying: [],
  view: 'library',
  loading: false,
}

let refreshToken = 0
let selectionRefreshTimer = null

const el = Object.fromEntries([
  'hostList', 'apiVersion', 'useAuth', 'authFields', 'username', 'password', 'selectAllBtn', 'mobileMenuBtn',
  'manageHostsBtn', 'fleetDialog', 'hostManageList', 'hostAddForm', 'newHostName', 'newHostIp',
  'pageTitle', 'pageSubtitle', 'lastUpdated', 'refreshBtn', 'addAssetBtn',
  'playbackControls', 'previousAssetBtn', 'nextAssetBtn',
  'libraryView', 'monitorView', 'monitorGrid', 'screensView', 'onlineCount', 'activeCount', 'scheduledCount',
  'offlineCount', 'allBadge', 'activeBadge', 'inactiveBadge', 'searchInput',
  'screenFilter', 'sortSelect', 'autoRefreshToggle', 'bulkBar', 'selectedCount', 'bulkEnableBtn', 'bulkDisableBtn',
  'bulkDeleteBtn', 'selectVisible', 'assetsBody', 'statusGrid', 'assetDialog',
  'assetForm', 'assetFile', 'assetUrl', 'fileName', 'fileSource', 'urlSource',
  'assetName', 'startDate', 'endDate', 'duration', 'enabled', 'targetSummary',
  'submitAssetBtn', 'editDialog', 'editForm', 'editHost', 'editAssetId',
  'editHostLabel', 'editName', 'editStartDate', 'editEndDate', 'editDuration',
  'editEnabled', 'confirmDialog', 'confirmTitle', 'confirmText', 'confirmAccept',
  'toastRegion',
].map((id) => [id, document.getElementById(id)]))

function fleetHosts() {
  return state.hosts.map((item) => item.host)
}

function hostRecord(host) {
  return state.hosts.find((item) => item.host === host) || { host, name: host }
}

function selectedHosts() {
  return [...document.querySelectorAll('.host-check:checked')].map((input) => input.value)
}

function authPayload() {
  if (!el.useAuth.checked) return { username: '', password: '', apiVersion: el.apiVersion.value }
  return {
    username: el.username.value.trim(),
    password: el.password.value,
    apiVersion: el.apiVersion.value,
  }
}

function assetId(asset) {
  return String(asset.asset_id ?? asset.assetId ?? asset.id ?? '')
}

function assetKey(host, asset) {
  return `${host}::${assetId(asset)}`
}

function isEnabled(asset) {
  return asset.is_enabled === true || asset.is_enabled === 1 || asset.is_enabled === '1'
}

function isCurrentlyActive(asset) {
  if (asset.is_active !== undefined) {
    return asset.is_active === true || asset.is_active === 1 || asset.is_active === '1'
  }
  return assetStatus(asset) === 'active'
}

function allAssets() {
  return state.results.flatMap((result) => result.ok
    ? (result.assets || []).map((asset) => ({ host: result.host, version: result.version, asset }))
    : [])
}

function assetStatus(asset) {
  if (!isEnabled(asset)) return 'inactive'
  const start = parseDate(asset.start_date)
  if (start && start.getTime() > Date.now()) return 'scheduled'
  return 'active'
}

function visibleAssets() {
  const query = state.search.toLocaleLowerCase('es')
  const items = allAssets().filter((item) => {
    const status = assetStatus(item.asset)
    const matchesStatus = state.filter === 'all' || state.filter === status || (state.filter === 'active' && status === 'scheduled')
    const name = String(item.asset.name || item.asset.title || item.asset.uri || '').toLocaleLowerCase('es')
    return matchesStatus && (state.screen === 'all' || state.screen === item.host) && name.includes(query)
  })
  const hostPosition = new Map(fleetHosts().map((host, index) => [host, index]))
  const playlistCompare = (left, right) => (hostPosition.get(left.host) - hostPosition.get(right.host)) ||
    (Number(left.asset.play_order || 0) - Number(right.asset.play_order || 0))
  if (state.sort === 'name') items.sort((left, right) => String(left.asset.name || '').localeCompare(String(right.asset.name || ''), 'es'))
  else if (state.sort === 'screen') items.sort(playlistCompare)
  else if (state.sort === 'status') items.sort((left, right) => assetStatus(left.asset).localeCompare(assetStatus(right.asset)) || playlistCompare(left, right))
  else items.sort(playlistCompare)
  return items
}

function renderHosts() {
  const existingChecks = [...document.querySelectorAll('.host-check')]
  const chosenHosts = new Set(existingChecks.length ? selectedHosts() : fleetHosts())
  const resultMap = new Map(state.results.map((result) => [result.host, result]))
  el.hostList.innerHTML = state.hosts.map(({ host, name }) => {
    const result = resultMap.get(host)
    const suffix = host.split('.').pop()
    const status = result ? (result.ok ? 'ok' : result.authRequired ? 'warn' : 'bad') : ''
    const detail = result ? (result.ok ? `${(result.assets || []).length} contenidos` : result.authRequired ? 'Requiere clave' : 'Sin conexion') : 'Pendiente'
    return `<div class="host-item">
      <input class="host-check" type="checkbox" value="${host}" ${chosenHosts.has(host) ? 'checked' : ''} aria-label="Seleccionar ${escapeHtml(name)}">
      <button class="host-main" type="button" data-solo="${host}" title="Administrar solo esta pantalla"><span><strong>${escapeHtml(name)}</strong><small>${host} - ${detail}</small></span></button>
      <i class="host-state ${status}" aria-hidden="true"></i>
    </div>`
  }).join('')
  updateScreenFilter()
  renderHostManager()
}

function updateScreenFilter() {
  const current = el.screenFilter.value || state.screen
  el.screenFilter.innerHTML = '<option value="all">Todas las pantallas</option>' + state.hosts.map(({ host, name }) =>
    `<option value="${host}">${escapeHtml(name)} - ${host}</option>`).join('')
  el.screenFilter.value = fleetHosts().includes(current) ? current : 'all'
}

function render() {
  const assets = allAssets()
  const online = state.results.filter((result) => result.ok || Number(result.status) > 0).length
  const active = assets.filter((item) => assetStatus(item.asset) === 'active').length
  const scheduled = assets.filter((item) => assetStatus(item.asset) === 'scheduled').length
  const inactive = assets.filter((item) => assetStatus(item.asset) === 'inactive').length
  el.onlineCount.textContent = online
  el.offlineCount.textContent = state.results.filter((result) => Number(result.status) === 0).length
  el.activeCount.textContent = active
  el.scheduledCount.textContent = scheduled
  el.allBadge.textContent = assets.length
  el.activeBadge.textContent = active + scheduled
  el.inactiveBadge.textContent = inactive
  renderHosts()
  renderAssets()
  renderScreens()
  updateBulkBar()
}

function renderAssets() {
  const items = visibleAssets()
  if (!items.length) {
    el.assetsBody.innerHTML = `<tr><td colspan="7"><div class="empty-state"><strong>No hay contenidos que mostrar</strong><span>Prueba otro filtro o anade un contenido nuevo.</span></div></td></tr>`
    el.selectVisible.checked = false
    return
  }

  const soloHosts = selectedHosts()
  const soloHost = soloHosts.length === 1 ? soloHosts[0] : null
  const orderableCount = soloHost ? allAssets().filter((item) => item.host === soloHost && isCurrentlyActive(item.asset)).length : 0
  el.assetsBody.innerHTML = items.map(({ host, asset }) => {
    const id = assetId(asset)
    const key = assetKey(host, asset)
    const status = assetStatus(asset)
    const type = assetType(asset)
    const checked = state.selected.has(key) ? 'checked' : ''
    const canOrder = soloHost === host && orderableCount > 1 && isCurrentlyActive(asset)
    return `<tr data-key="${escapeHtml(key)}">
      <td class="check-column"><input class="row-check" type="checkbox" ${checked} aria-label="Seleccionar ${escapeHtml(asset.name || id)}"></td>
      <td><div class="asset-title"><span class="asset-thumb">${type.icon}</span><span><strong title="${escapeHtml(asset.name || '')}">${escapeHtml(asset.name || asset.title || 'Sin nombre')}</strong><small>${type.label} - ${escapeHtml(host)} - ${statusLabel(status)} - ${escapeHtml(id)}</small></span></div></td>
      <td><span class="host-chip">${escapeHtml(host)}</span></td>
      <td class="schedule-cell"><span>${formatDate(asset.start_date, 'Sin inicio')}</span><small>hasta ${formatDate(asset.end_date, 'sin limite')}</small></td>
      <td>${formatDuration(asset.duration)}</td>
      <td><span class="status-pill ${status}">${statusLabel(status)}</span></td>
      <td class="actions-column"><div class="row-actions">
        ${canOrder ? `<span class="order-controls"><button class="row-button" type="button" data-action="move-up" data-key="${escapeHtml(key)}" title="Subir en la playlist" aria-label="Subir en la playlist">&#8593;</button><button class="row-button" type="button" data-action="move-down" data-key="${escapeHtml(key)}" title="Bajar en la playlist" aria-label="Bajar en la playlist">&#8595;</button></span>` : ''}
        <button class="row-button" type="button" data-action="toggle" data-host="${host}" data-id="${escapeHtml(id)}" title="${isEnabled(asset) ? 'Desactivar' : 'Activar'}" aria-label="${isEnabled(asset) ? 'Desactivar' : 'Activar'}">${isEnabled(asset) ? '&#10074;&#10074;' : '&#9654;'}</button>
        <button class="row-button" type="button" data-action="edit" data-key="${escapeHtml(key)}" title="Editar" aria-label="Editar">&#9998;</button>
        <button class="row-button" type="button" data-action="download" data-key="${escapeHtml(key)}" title="Descargar" aria-label="Descargar">&#8681;</button>
        <button class="row-button danger" type="button" data-action="delete" data-host="${host}" data-id="${escapeHtml(id)}" title="Eliminar" aria-label="Eliminar">&#10005;</button>
      </div></td>
    </tr>`
  }).join('')
  el.selectVisible.checked = items.every((item) => state.selected.has(assetKey(item.host, item.asset)))
}

function renderScreens() {
  if (!state.results.length) {
    el.statusGrid.innerHTML = '<div class="empty-state"><strong>Sin datos de pantallas</strong><span>Pulsa actualizar para consultar la flota.</span></div>'
    return
  }
  el.statusGrid.innerHTML = state.results.map((result) => {
    const assets = result.assets || []
    const record = hostRecord(result.host)
    const active = assets.filter((asset) => assetStatus(asset) !== 'inactive').length
    const connectionLabel = result.ok ? 'En linea' : result.authRequired ? 'Protegida' : 'Sin conexion'
    const connectionClass = result.ok ? 'active' : result.authRequired ? 'scheduled' : 'inactive'
    return `<article class="screen-card">
      <div class="screen-card-head"><div><h3>${escapeHtml(record.name)}</h3><p>${result.host}</p></div><span class="status-pill ${connectionClass}">${connectionLabel}</span></div>
      <div class="screen-card-stats"><div><strong>${result.ok ? `API ${result.version}` : '-'}</strong><small>Version detectada</small></div><div><strong>${result.ok ? active : '-'}</strong><small>Activos</small></div><div><strong>${result.ok ? assets.length : '-'}</strong><small>Totales</small></div><div><strong>${result.ok ? 'Disponible' : result.authRequired ? 'Introducir clave' : 'Revisar red'}</strong><small>Estado</small></div></div>
      <button class="manage-screen secondary-button" type="button" data-manage-host="${result.host}">Administrar esta pantalla</button>
    </article>`
  }).join('')
}

function renderMonitor() {
  const existingPlayers = new Map([...el.monitorGrid.querySelectorAll('.live-player')].map((player) => {
    const key = `${player.dataset.host}::${player.dataset.id}`
    player.remove()
    return [key, player]
  }))
  const selected = selectedHosts()
  if (!selected.length) {
    el.monitorGrid.innerHTML = '<div class="empty-state"><strong>Selecciona al menos una pantalla</strong><span>Marca las Raspberry que quieras supervisar.</span></div>'
    return
  }
  const resultMap = new Map(state.nowPlaying.map((result) => [result.host, result]))
  el.monitorGrid.innerHTML = selected.map((host) => {
    const record = hostRecord(host)
    const result = resultMap.get(host)
    if (!result) return monitorShell(record, host, '<div class="monitor-placeholder"><span class="monitor-spinner"></span><strong>Consultando reproductor</strong></div>', 'Consultando')
    if (!result.ok) return monitorShell(record, host, '<div class="monitor-placeholder error"><span>!</span><strong>Sin senal del reproductor</strong><small>Revisa la red o la contrasena</small></div>', 'Sin conexion', 'inactive')
    if (!result.asset) return monitorShell(record, host, '<div class="monitor-placeholder"><span>&#9633;</span><strong>Sin contenido en reproduccion</strong></div>', 'En linea')

    const asset = result.asset
    const type = assetType(asset)
    const name = escapeHtml(asset.name || asset.title || 'Sin nombre')
    const telemetry = result.telemetry
    let visual
    if (telemetry && type.label === 'Video') {
      const id = assetId(asset)
      const position = Number(telemetry.position || 0)
      const duration = Number(telemetry.duration || asset.duration || 0)
      visual = `<video class="live-player" data-host="${host}" data-id="${escapeHtml(id)}" data-position="${position}" data-duration="${duration}" src="/api/live-media/${host}/${encodeURIComponent(id)}" muted autoplay loop playsinline preload="metadata"></video>`
    } else if (result.preview) {
      visual = `<img class="monitor-image" src="${result.preview}" alt="Vista actual de ${name}">`
    } else if (result.previewUrl && String(result.previewUrl).startsWith('http')) {
      visual = `<img class="monitor-image" src="${escapeHtml(result.previewUrl)}" alt="Vista actual de ${name}">`
    } else {
      visual = `<div class="monitor-video"><span class="video-glyph">&#9654;</span><span>${escapeHtml(type.label)}</span><small>La API no ofrece imagen ni posicion HDMI en directo</small></div>`
    }
    const position = Number(telemetry?.position || 0)
    const duration = Number(telemetry?.duration || asset.duration || 0)
    const progress = duration > 0 ? Math.min(100, Math.max(0, position / duration * 100)) : 0
    const liveMeta = telemetry ? `<div class="live-progress"><span style="width:${progress}%"></span></div><p><strong class="live-time">${formatClock(position)} / ${formatClock(duration)}</strong><span> Sincronizado con la Raspberry</span></p>` : `<p>${escapeHtml(type.label)}${asset.duration ? ` &middot; ${formatDuration(asset.duration)}` : ''}</p>`
    const details = `<div class="monitor-caption"><span class="status-pill active"><i class="pulse-dot"></i> ${telemetry ? 'En tiempo real' : 'Reproduciendo'}</span><h3 title="${name}">${name}</h3>${liveMeta}</div>`
    return monitorShell(record, host, visual + details, 'En linea', 'active')
  }).join('')

  el.monitorGrid.querySelectorAll('.live-player').forEach((freshPlayer) => {
    const key = `${freshPlayer.dataset.host}::${freshPlayer.dataset.id}`
    const existing = existingPlayers.get(key)
    if (existing) {
      existing.dataset.position = freshPlayer.dataset.position
      existing.dataset.duration = freshPlayer.dataset.duration
      freshPlayer.replaceWith(existing)
    }
  })
  syncLivePlayers()
}

function syncLivePlayers() {
  el.monitorGrid.querySelectorAll('.live-player').forEach((player) => {
    const synchronize = () => {
      const wanted = Number(player.dataset.position || 0)
      if (Number.isFinite(wanted) && Math.abs(player.currentTime - wanted) > 1.15) player.currentTime = wanted
      if (player.paused) player.play().catch(() => {})
    }
    if (player.readyState >= 1) synchronize()
    else player.onloadedmetadata = synchronize
  })
}

function formatClock(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0))
  const minutes = Math.floor(seconds / 60)
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`
}

function monitorShell(record, host, content, status, statusClass = 'scheduled') {
  return `<article class="monitor-card">
    <header><div><strong>${escapeHtml(record.name)}</strong><small>${escapeHtml(host)}</small></div><span class="status-pill ${statusClass}">${status}</span></header>
    <div class="monitor-stage">${content}</div>
    <footer><button class="secondary-button compact" type="button" data-monitor-host="${host}">Administrar</button></footer>
  </article>`
}

function updateBulkBar() {
  el.selectedCount.textContent = state.selected.size
  el.bulkBar.hidden = state.selected.size === 0
}

async function postJson(action, payload) {
  const response = await fetch(`/api/${action}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || 'Error de servidor')
  return data
}

async function fleetRequest(url, options = {}) {
  const response = await fetch(url, options)
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || 'No se pudo actualizar la flota')
  return data
}

async function loadHosts() {
  const data = await fleetRequest('/api/hosts')
  state.hosts = Array.isArray(data.hosts) ? data.hosts : []
  const allowed = new Set(fleetHosts())
  state.results = state.results.filter((result) => allowed.has(result.host))
  if (state.screen !== 'all' && !allowed.has(state.screen)) state.screen = 'all'
  renderHosts()
  updatePageContext()
}

function renderHostManager() {
  if (!el.hostManageList) return
  if (!state.hosts.length) {
    el.hostManageList.innerHTML = '<div class="empty-manage"><strong>No hay pantallas</strong><span>Anade la primera Raspberry con el formulario inferior.</span></div>'
    return
  }
  el.hostManageList.innerHTML = state.hosts.map(({ host, name }) => `
    <div class="host-manage-row" data-host-row="${host}">
      <span class="host-manage-dot"></span>
      <label><span>Nombre</span><input class="host-name-input" value="${escapeHtml(name)}" maxlength="60"></label>
      <code>${host}</code>
      <button class="row-button" type="button" data-host-action="rename" data-host="${host}" title="Guardar nombre" aria-label="Guardar nombre">&#10003;</button>
      <button class="row-button danger" type="button" data-host-action="remove" data-host="${host}" title="Eliminar pantalla" aria-label="Eliminar pantalla">&#10005;</button>
    </div>`).join('')
}

async function addFleetHost(event) {
  event.preventDefault()
  try {
    const data = await fleetRequest('/api/hosts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: el.newHostIp.value.trim(), name: el.newHostName.value.trim() }),
    })
    el.hostAddForm.reset()
    await loadHosts()
    const checkbox = document.querySelector(`.host-check[value="${data.host.host}"]`)
    if (checkbox) checkbox.checked = true
    toast('Raspberry anadida', `${data.host.name} - ${data.host.host}`)
    await refreshFleet()
  } catch (error) {
    toast('No se pudo anadir', error.message, 'error')
  }
}

async function renameFleetHost(host, row) {
  const input = row.querySelector('.host-name-input')
  try {
    const data = await fleetRequest(`/api/hosts/${encodeURIComponent(host)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: input.value.trim() }),
    })
    await loadHosts()
    toast('Nombre actualizado', data.host.name)
  } catch (error) {
    toast('No se pudo renombrar', error.message, 'error')
  }
}

async function removeFleetHost(host) {
  const record = hostRecord(host)
  const accepted = await confirmAction('Eliminar pantalla', `Se quitara ${record.name} (${host}) del panel. No se borrara nada de la Raspberry.`, 'Quitar')
  if (!accepted) return
  try {
    await fleetRequest(`/api/hosts/${encodeURIComponent(host)}`, { method: 'DELETE' })
    await loadHosts()
    toast('Pantalla eliminada', `${record.name} ya no aparece en la flota.`)
    if (selectedHosts().length) await refreshFleet(); else render()
  } catch (error) {
    toast('No se pudo eliminar', error.message, 'error')
  }
}

async function refreshFleet() {
  const hosts = selectedHosts()
  const token = ++refreshToken
  if (!hosts.length) {
    state.results = []
    render()
    setLoading(false)
    return
  }
  setLoading(true)
  try {
    const data = await postJson('list', { hosts, ...authPayload() })
    if (token !== refreshToken) return
    state.results = data.results || []
    state.selected.clear()
    el.lastUpdated.textContent = `Actualizado ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
    render()
    const online = state.results.filter((result) => result.ok).length
    toast('Consulta terminada', `${online} de ${state.results.length} pantallas en linea.`, online ? 'success' : 'error')
  } catch (error) {
    if (token !== refreshToken) return
    toast('No se pudo actualizar', error.message, 'error')
  } finally {
    if (token === refreshToken) setLoading(false)
  }
}

let monitorTimer = null
let monitorRefreshing = false

async function refreshNowPlaying(showToast = false) {
  if (monitorRefreshing) return
  const hosts = selectedHosts()
  if (!hosts.length) {
    state.nowPlaying = []
    renderMonitor()
    return
  }
  monitorRefreshing = true
  try {
    const data = await postJson('now', { hosts, ...authPayload() })
    state.nowPlaying = data.results || []
    el.lastUpdated.textContent = `En directo ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
    renderMonitor()
    if (showToast) toast('Monitor actualizado', `${state.nowPlaying.filter((item) => item.ok).length} de ${hosts.length} pantallas responden.`)
  } catch (error) {
    if (showToast) toast('No se pudo actualizar', error.message, 'error')
  } finally {
    monitorRefreshing = false
  }
}

function configureMonitorTimer() {
  if (monitorTimer) clearInterval(monitorTimer)
  monitorTimer = null
  if (state.view !== 'monitor') return
  monitorTimer = setInterval(() => {
    if (!document.querySelector('dialog[open]')) refreshNowPlaying(false)
  }, 1000)
}

function refreshActiveView() {
  return state.view === 'monitor' ? refreshNowPlaying(true) : refreshFleet()
}

function scheduleSelectionRefresh() {
  if (selectionRefreshTimer) clearTimeout(selectionRefreshTimer)
  selectionRefreshTimer = setTimeout(() => {
    selectionRefreshTimer = null
    refreshActiveView()
  }, 450)
}

function setLoading(loading) {
  state.loading = loading
  el.refreshBtn.disabled = loading
  el.refreshBtn.innerHTML = loading ? '<span class="button-icon">&#8635;</span> Consultando' : '<span class="button-icon">&#8635;</span> Actualizar'
}

function openAssetDialog() {
  const hosts = selectedHosts()
  if (!hosts.length) return toast('Selecciona una pantalla', 'Elige al menos un destino antes de anadir contenido.', 'error')
  el.targetSummary.textContent = `${hosts.length} pantalla${hosts.length === 1 ? '' : 's'} seleccionada${hosts.length === 1 ? '' : 's'}`
  el.assetDialog.showModal()
}

async function submitAsset(event) {
  event.preventDefault()
  const hosts = selectedHosts()
  if (!hosts.length) return toast('Sin destinos', 'Selecciona al menos una pantalla.', 'error')
  el.submitAssetBtn.disabled = true
  try {
    let payload
    if (state.source === 'file') {
      if (!el.assetFile.files[0]) throw new Error('Selecciona un archivo de video o imagen.')
      const form = new FormData(el.assetForm)
      form.set('hosts', hosts.join(','))
      form.set('apiVersion', el.apiVersion.value)
      form.set('username', el.username.value.trim())
      form.set('password', el.password.value)
      form.set('enabled', el.enabled.checked ? '1' : '0')
      form.set('skipAssetCheck', '1')
      const response = await fetch('/api/upload', { method: 'POST', body: form })
      payload = await response.json()
      if (!response.ok) throw new Error(payload.error || 'Error durante la subida')
    } else {
      if (!el.assetUrl.value.trim()) throw new Error('Indica una direccion web valida.')
      payload = await postJson('url', {
        hosts, url: el.assetUrl.value.trim(), name: el.assetName.value.trim(),
        startDate: el.startDate.value, endDate: el.endDate.value,
        duration: Number(el.duration.value || 0), enabled: el.enabled.checked,
        ...authPayload(),
      })
    }
    const ok = (payload.results || []).filter((item) => item.ok).length
    toast('Contenido procesado', `${ok} de ${hosts.length} pantallas completadas.`, ok ? 'success' : 'error')
    el.assetDialog.close()
    el.assetForm.reset()
    el.endDate.value = '9999-01-01T00:00'
    el.enabled.checked = true
    await refreshFleet()
  } catch (error) {
    toast('No se pudo anadir', error.message, 'error')
  } finally {
    el.submitAssetBtn.disabled = false
  }
}

async function runTargets(operation, targets, extra = {}) {
  if (!targets.length) return
  try {
    const data = await postJson('asset', { operation, targets, ...extra, ...authPayload() })
    const ok = (data.results || []).filter((item) => item.ok).length
    toast('Operacion terminada', `${ok} de ${targets.length} cambios completados.`, ok ? 'success' : 'error')
    await refreshFleet()
  } catch (error) {
    toast('No se pudo completar', error.message, 'error')
  }
}

function targetFrom(host, id) {
  return [{ host, assetId: id }]
}

function selectedTargets() {
  return [...state.selected].map((key) => {
    const split = key.indexOf('::')
    return { host: key.slice(0, split), assetId: key.slice(split + 2) }
  })
}

function findItem(key) {
  return allAssets().find((item) => assetKey(item.host, item.asset) === key)
}

function openEdit(key) {
  const item = findItem(key)
  if (!item) return
  const asset = item.asset
  el.editHost.value = item.host
  el.editAssetId.value = assetId(asset)
  el.editHostLabel.textContent = `${item.host} - ${asset.name || 'Sin nombre'}`
  el.editName.value = asset.name || asset.title || ''
  el.editStartDate.value = toLocalInput(asset.start_date)
  el.editEndDate.value = toLocalInput(asset.end_date)
  el.editDuration.value = Number(asset.duration || 0)
  el.editEnabled.checked = isEnabled(asset)
  el.editDialog.showModal()
}

async function submitEdit(event) {
  event.preventDefault()
  const update = {
    name: el.editName.value.trim(), start_date: el.editStartDate.value,
    end_date: el.editEndDate.value, duration: Number(el.editDuration.value || 0),
    is_enabled: el.editEnabled.checked,
  }
  el.editDialog.close()
  await runTargets('update', targetFrom(el.editHost.value, el.editAssetId.value), { update })
}

async function downloadContent(key) {
  const item = findItem(key)
  if (!item) return
  toast('Preparando descarga', item.asset.name || 'Solicitando contenido a Screenly...')
  try {
    const response = await fetch('/api/download', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ targets: [{ host: item.host, assetId: assetId(item.asset) }], ...authPayload() }),
    })
    const contentType = response.headers.get('content-type') || ''
    if (contentType.includes('application/json')) {
      const data = await response.json()
      if (!response.ok) throw new Error(data.error || 'No se pudo descargar')
      if (data.type === 'url' && data.url) window.open(data.url, '_blank', 'noopener')
      return
    }
    if (!response.ok) throw new Error('No se pudo descargar el archivo')
    const blob = await response.blob()
    const disposition = response.headers.get('content-disposition') || ''
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
    const plainName = disposition.match(/filename="?([^";]+)"?/i)?.[1]
    const filename = encodedName ? decodeURIComponent(encodedName) : plainName || item.asset.name || 'contenido'
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.append(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    toast('Descarga iniciada', filename)
  } catch (error) {
    toast('No se pudo descargar', error.message, 'error')
  }
}

function manageOnly(host) {
  document.querySelectorAll('.host-check').forEach((input) => { input.checked = input.value === host })
  state.screen = host
  el.screenFilter.value = host
  state.selected.clear()
  switchView('library')
  updatePageContext()
  refreshFleet()
}

function updatePageContext() {
  const hosts = selectedHosts()
  if (!el.libraryView.classList.contains('active')) return
  if (hosts.length === 1) {
    const record = hostRecord(hosts[0])
    el.pageTitle.textContent = record.name
    el.pageSubtitle.textContent = `${hosts[0]} - Administracion individual`
  } else {
    el.pageTitle.textContent = 'Biblioteca de contenidos'
    el.pageSubtitle.textContent = `Gestion conjunta de ${hosts.length} pantallas`
  }
  el.playbackControls.hidden = hosts.length !== 1
}

async function controlPlayback(direction) {
  const hosts = selectedHosts()
  if (hosts.length !== 1) return
  try {
    await postJson('control', { hosts, direction, ...authPayload() })
    toast('Reproduccion actualizada', direction === 'next' ? 'Contenido siguiente seleccionado.' : 'Contenido anterior seleccionado.')
  } catch (error) {
    toast('No se pudo cambiar', error.message, 'error')
  }
}

async function movePlaylistAsset(key, direction) {
  const item = findItem(key)
  if (!item) return
  const ordered = allAssets()
    .filter((candidate) => candidate.host === item.host && isCurrentlyActive(candidate.asset))
    .sort((left, right) => Number(left.asset.play_order || 0) - Number(right.asset.play_order || 0))
  const currentIndex = ordered.findIndex((candidate) => assetKey(candidate.host, candidate.asset) === key)
  const targetIndex = direction === 'up' ? currentIndex - 1 : currentIndex + 1
  if (currentIndex < 0 || targetIndex < 0 || targetIndex >= ordered.length) {
    return toast('Orden sin cambios', direction === 'up' ? 'El contenido ya es el primero.' : 'El contenido ya es el ultimo.')
  }
  const swapped = [...ordered]
  ;[swapped[currentIndex], swapped[targetIndex]] = [swapped[targetIndex], swapped[currentIndex]]
  try {
    await postJson('order', {
      hosts: [item.host], orderedIds: swapped.map((candidate) => assetId(candidate.asset)), ...authPayload(),
    })
    toast('Playlist actualizada', `${item.asset.name || 'Contenido'} se ha movido.`)
    await refreshFleet()
  } catch (error) {
    toast('No se pudo reordenar', error.message, 'error')
  }
}

let autoRefreshTimer = null

function configureAutoRefresh() {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer)
  autoRefreshTimer = null
  localStorage.setItem('fleetboard-auto-refresh', el.autoRefreshToggle.checked ? '1' : '0')
  if (!el.autoRefreshToggle.checked) return
  autoRefreshTimer = setInterval(() => {
    if (!state.loading && !document.querySelector('dialog[open]')) refreshFleet()
  }, 60000)
}

function confirmAction(title, text, acceptLabel = 'Eliminar') {
  el.confirmTitle.textContent = title
  el.confirmText.textContent = text
  el.confirmAccept.textContent = acceptLabel
  el.confirmDialog.showModal()
  return new Promise((resolve) => {
    el.confirmDialog.addEventListener('close', () => resolve(el.confirmDialog.returnValue === 'confirm'), { once: true })
  })
}

function switchView(view) {
  state.view = view
  document.querySelectorAll('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.view === view))
  el.libraryView.classList.toggle('active', view === 'library')
  el.monitorView.classList.toggle('active', view === 'monitor')
  el.screensView.classList.toggle('active', view === 'screens')
  el.pageTitle.textContent = view === 'library' ? 'Biblioteca de contenidos' : view === 'monitor' ? 'En pantalla ahora' : 'Estado de pantallas'
  el.pageSubtitle.textContent = view === 'library' ? `Gestion conjunta de ${selectedHosts().length} pantallas` : view === 'monitor' ? 'Supervision en directo de los reproductores seleccionados' : 'Supervision de la flota local'
  if (view === 'library') updatePageContext()
  else el.playbackControls.hidden = true
  el.addAssetBtn.hidden = view !== 'library'
  if (view === 'monitor') refreshNowPlaying(false)
  configureMonitorTimer()
  document.querySelector('.sidebar').classList.remove('open')
}

function switchSource(source) {
  state.source = source
  document.querySelectorAll('[data-source]').forEach((button) => button.classList.toggle('active', button.dataset.source === source))
  el.fileSource.classList.toggle('active', source === 'file')
  el.urlSource.classList.toggle('active', source === 'url')
}

function toast(title, message, type = 'success') {
  const node = document.createElement('div')
  node.className = `toast ${type === 'error' ? 'error' : ''}`
  node.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span>`
  el.toastRegion.append(node)
  setTimeout(() => node.remove(), 4500)
}

function assetType(asset) {
  const value = String(asset.mimetype || asset.type || asset.uri || '').toLowerCase()
  if (value.includes('image') || /\.(jpg|jpeg|png|gif|webp)(\?|$)/.test(value)) return { icon: 'IMG', label: 'Imagen' }
  if (value.includes('web') || /^https?:/.test(String(asset.uri || ''))) return { icon: 'WEB', label: 'Web' }
  return { icon: 'VID', label: 'Video' }
}

function statusLabel(status) {
  return status === 'active' ? 'Activo' : status === 'scheduled' ? 'Programado' : 'Inactivo'
}

function formatDuration(value) {
  const seconds = Number(value || 0)
  if (!seconds) return 'Auto'
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return minutes ? `${minutes} min${rest ? ` ${rest} s` : ''}` : `${rest} s`
}

function parseDate(value) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function formatDate(value, fallback) {
  const date = parseDate(value)
  if (!date || date.getFullYear() >= 9999) return fallback
  return date.toLocaleString('es-ES', { dateStyle: 'short', timeStyle: 'short' })
}

function toLocalInput(value) {
  const date = parseDate(value)
  if (!date) return ''
  if (date.getFullYear() >= 9999) return '9999-01-01T00:00'
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 16)
}

function escapeHtml(value) {
  return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;')
}

document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)))
document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => document.getElementById(button.dataset.close).close()))
document.querySelectorAll('[data-source]').forEach((button) => button.addEventListener('click', () => switchSource(button.dataset.source)))
document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => {
  state.filter = button.dataset.filter
  document.querySelectorAll('[data-filter]').forEach((item) => item.classList.toggle('active', item === button))
  renderAssets()
}))

el.refreshBtn.addEventListener('click', refreshActiveView)
el.manageHostsBtn.addEventListener('click', () => { renderHostManager(); el.fleetDialog.showModal() })
el.hostAddForm.addEventListener('submit', addFleetHost)
el.hostManageList.addEventListener('click', (event) => {
  const button = event.target.closest('[data-host-action]')
  if (!button) return
  const row = button.closest('[data-host-row]')
  if (button.dataset.hostAction === 'rename') renameFleetHost(button.dataset.host, row)
  if (button.dataset.hostAction === 'remove') removeFleetHost(button.dataset.host)
})
el.previousAssetBtn.addEventListener('click', () => controlPlayback('previous'))
el.nextAssetBtn.addEventListener('click', () => controlPlayback('next'))
el.addAssetBtn.addEventListener('click', openAssetDialog)
el.assetForm.addEventListener('submit', submitAsset)
el.editForm.addEventListener('submit', submitEdit)
el.mobileMenuBtn.addEventListener('click', () => document.querySelector('.sidebar').classList.toggle('open'))
el.searchInput.addEventListener('input', () => { state.search = el.searchInput.value; renderAssets() })
el.screenFilter.addEventListener('change', () => { state.screen = el.screenFilter.value; renderAssets() })
el.sortSelect.addEventListener('change', () => { state.sort = el.sortSelect.value; renderAssets() })
el.autoRefreshToggle.addEventListener('change', configureAutoRefresh)
el.useAuth.addEventListener('change', () => {
  el.authFields.hidden = !el.useAuth.checked
  refreshActiveView()
})
el.assetFile.addEventListener('change', () => { el.fileName.textContent = el.assetFile.files[0]?.name || 'MP4, MOV, WEBM, JPG o PNG' })
el.hostList.addEventListener('change', () => {
  state.selected.clear()
  state.screen = 'all'
  el.screenFilter.value = 'all'
  updateBulkBar()
  updatePageContext()
  if (state.view === 'monitor') renderMonitor()
  renderAssets()
  el.targetSummary.textContent = `${selectedHosts().length} pantallas seleccionadas`
  scheduleSelectionRefresh()
})
el.hostList.addEventListener('click', (event) => {
  const button = event.target.closest('[data-solo]')
  if (button) manageOnly(button.dataset.solo)
})
el.selectAllBtn.addEventListener('click', () => {
  const checks = [...document.querySelectorAll('.host-check')]
  checks.forEach((input) => { input.checked = true })
  state.screen = 'all'
  el.screenFilter.value = 'all'
  updatePageContext()
  el.targetSummary.textContent = `${selectedHosts().length} pantallas seleccionadas`
  refreshActiveView()
})
el.selectVisible.addEventListener('change', () => {
  visibleAssets().forEach((item) => {
    const key = assetKey(item.host, item.asset)
    if (el.selectVisible.checked) state.selected.add(key); else state.selected.delete(key)
  })
  renderAssets(); updateBulkBar()
})
el.assetsBody.addEventListener('change', (event) => {
  if (!event.target.classList.contains('row-check')) return
  const key = event.target.closest('tr').dataset.key
  if (event.target.checked) state.selected.add(key); else state.selected.delete(key)
  updateBulkBar()
})
el.assetsBody.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]')
  if (!button) return
  const action = button.dataset.action
  if (action === 'edit') return openEdit(button.dataset.key)
  if (action === 'download') return downloadContent(button.dataset.key)
  if (action === 'move-up') return movePlaylistAsset(button.dataset.key, 'up')
  if (action === 'move-down') return movePlaylistAsset(button.dataset.key, 'down')
  if (action === 'toggle') {
    const item = allAssets().find(({ host, asset }) => host === button.dataset.host && assetId(asset) === button.dataset.id)
    return runTargets(item && isEnabled(item.asset) ? 'disable' : 'enable', targetFrom(button.dataset.host, button.dataset.id))
  }
  if (action === 'delete' && await confirmAction('Eliminar contenido', `Se eliminara de ${button.dataset.host}. Esta accion no se puede deshacer.`)) {
    return runTargets('delete', targetFrom(button.dataset.host, button.dataset.id))
  }
})
el.bulkEnableBtn.addEventListener('click', () => runTargets('enable', selectedTargets()))
el.bulkDisableBtn.addEventListener('click', () => runTargets('disable', selectedTargets()))
el.bulkDeleteBtn.addEventListener('click', async () => {
  const targets = selectedTargets()
  if (await confirmAction('Eliminar contenidos', `Se eliminaran ${targets.length} contenidos de sus pantallas.`)) runTargets('delete', targets)
})
el.statusGrid.addEventListener('click', (event) => {
  const button = event.target.closest('[data-manage-host]')
  if (button) manageOnly(button.dataset.manageHost)
})
el.monitorGrid.addEventListener('click', (event) => {
  const button = event.target.closest('[data-monitor-host]')
  if (button) manageOnly(button.dataset.monitorHost)
})

async function initialize() {
  try {
    el.autoRefreshToggle.checked = localStorage.getItem('fleetboard-auto-refresh') === '1'
    configureAutoRefresh()
    await loadHosts()
    if (state.hosts.length) await refreshFleet()
    else render()
  } catch (error) {
    toast('No se pudo iniciar', error.message, 'error')
  }
}

initialize()
