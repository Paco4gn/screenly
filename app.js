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
  diagnostics: [],
  history: [],
  users: [],
  view: 'library',
  role: 'admin',
  settings: { defaultAuth: { enabled: false, username: '', hasPassword: false, apiVersion: 'auto' } },
  loading: false,
}

let refreshToken = 0
let selectionRefreshTimer = null
let fleetAbortController = null
let previewHydrationTimer = null
let previewObserver = null
let pendingPreviews = []
let activePreviewLoads = 0

const MAX_PARALLEL_PREVIEWS = 2

const el = Object.fromEntries([
  'hostList', 'apiVersion', 'useAuth', 'authFields', 'username', 'password', 'saveGlobalAuthBtn', 'clearGlobalAuthBtn', 'globalAuthStatus', 'authScopeNote', 'editSelectedAuthBtn', 'selectAllBtn', 'mobileMenuBtn', 'sidebarBackdrop',
  'manageHostsBtn', 'fleetDialog', 'hostManageList', 'hostAddForm', 'newHostName', 'newHostIp',
  'pageTitle', 'pageSubtitle', 'lastUpdated', 'refreshBtn', 'addAssetBtn',
  'playbackControls', 'previousAssetBtn', 'nextAssetBtn',
  'libraryView', 'monitorView', 'monitorGrid', 'screensView', 'onlineCount', 'activeCount', 'scheduledCount',
  'opsView', 'runDiagnosticsBtn', 'diagnosticsList', 'alertsList', 'historyList', 'usersPanel', 'usersList', 'userAddForm', 'newUserEmail', 'newUserPassword', 'newUserRole',
  'offlineCount', 'allBadge', 'activeBadge', 'scheduledBadge', 'inactiveBadge', 'searchInput',
  'screenFilter', 'sortSelect', 'autoRefreshToggle', 'bulkBar', 'selectedCount', 'bulkEnableBtn', 'bulkDisableBtn',
  'bulkScheduleBtn', 'bulkDeleteBtn', 'selectVisible', 'assetsBody', 'statusGrid', 'assetDialog',
  'assetForm', 'assetFile', 'assetUrl', 'fileName', 'fileSource', 'urlSource',
  'assetName', 'startDate', 'endDate', 'duration', 'enabled', 'avoidDuplicates', 'targetSummary', 'uploadStatus',
  'submitAssetBtn', 'editDialog', 'editForm', 'editHost', 'editAssetId',
  'editHostLabel', 'editName', 'editStartDate', 'editEndDate', 'editDuration',
  'scheduleDialog', 'scheduleForm', 'bulkStartDate', 'bulkEndDate', 'bulkDuration', 'bulkEnabled', 'scheduleSummary',
  'editEnabled', 'confirmDialog', 'confirmTitle', 'confirmText', 'confirmAccept',
  'toastRegion', 'logoutBtn',
].map((id) => [id, document.getElementById(id)]))

function fleetHosts() {
  return state.hosts.map((item) => item.host)
}

function hostRecord(host) {
  return state.hosts.find((item) => item.host === host) || { host, name: host }
}

function globalAuthRecord() {
  return state.settings.defaultAuth || { enabled: false, username: '', hasPassword: false, apiVersion: 'auto' }
}

function selectedHosts() {
  return [...document.querySelectorAll('.host-check:checked')].map((input) => input.value)
}

function authPayload() {
  if (!el.useAuth.checked) return { apiVersion: el.apiVersion.value }
  const username = el.username.value.trim()
  const password = el.password.value
  if (!password) return { apiVersion: el.apiVersion.value }
  return { username, password, apiVersion: el.apiVersion.value }
}

function hostLabel(host) {
  const record = hostRecord(host)
  return record.name && record.name !== host ? `${record.name} - ${host}` : host
}

function hostAuthState(host) {
  const record = hostRecord(host)
  const own = record.auth || {}
  const global = globalAuthRecord()
  if (own.enabled && own.username) {
    return {
      mode: 'own',
      label: own.hasPassword ? `Propias: ${own.username}` : `Propias incompletas: ${own.username}`,
      hint: own.hasPassword ? 'Usa credenciales propias guardadas.' : 'Tiene usuario propio, pero falta la contrasena.',
    }
  }
  if (global.enabled && global.username) {
    return {
      mode: 'global',
      label: global.hasPassword ? `Globales: ${global.username}` : `Globales incompletas: ${global.username}`,
      hint: global.hasPassword ? 'Usa las credenciales globales guardadas.' : 'Hay usuario global, pero falta la contrasena.',
    }
  }
  return { mode: 'none', label: 'Sin credenciales', hint: 'No hay credenciales guardadas para esta Raspberry.' }
}

function canOperate() {
  return ['admin', 'operator'].includes(state.role)
}

function canAdmin() {
  return state.role === 'admin'
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
  const end = parseDate(asset.end_date)
  if (end && end.getFullYear() < 9999 && end.getTime() < Date.now()) return 'inactive'
  if (start && start.getTime() > Date.now()) return 'scheduled'
  return 'active'
}

function visibleAssets() {
  const query = state.search.toLocaleLowerCase('es')
  const items = allAssets().filter((item) => {
    const status = assetStatus(item.asset)
    const matchesStatus = state.filter === 'all' || state.filter === status
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

function playlistItems(host) {
  return allAssets()
    .filter((candidate) => candidate.host === host)
    .sort((left, right) => Number(left.asset.play_order || 0) - Number(right.asset.play_order || 0))
}

function renderHosts() {
  const existingChecks = [...document.querySelectorAll('.host-check')]
  const chosenHosts = new Set(existingChecks.length ? selectedHosts() : fleetHosts())
  const resultMap = new Map(state.results.map((result) => [result.host, result]))
  el.hostList.innerHTML = state.hosts.map(({ host, name }) => {
    const result = resultMap.get(host)
    const maintenance = Boolean(hostRecord(host).maintenance)
    const authState = hostAuthState(host)
    const status = maintenance ? 'maintenance' : result ? (result.ok ? 'ok' : result.authRequired ? 'warn' : 'bad') : ''
    const detail = maintenance ? 'Mantenimiento' : result ? (result.ok ? `${(result.assets || []).length} contenidos` : diagnosticLabel(result)) : 'Pendiente'
    return `<div class="host-item">
      <input class="host-check" type="checkbox" value="${host}" ${chosenHosts.has(host) ? 'checked' : ''} aria-label="Seleccionar ${escapeHtml(name)}">
      <button class="host-main" type="button" data-solo="${host}" title="${escapeHtml(authState.hint)}"><span><strong>${escapeHtml(name)}</strong><small>${host} - ${detail}${authState.mode !== 'none' ? ` - ${escapeHtml(authState.label)}` : ''}</small></span></button>
      <i class="host-state ${status}" aria-hidden="true"></i>
    </div>`
  }).join('')
  updateSelectAllButton()
  updateScreenFilter()
  renderHostManager()
}

function updateSelectAllButton() {
  const selected = selectedHosts().length
  const allSelected = state.hosts.length > 0 && selected === state.hosts.length
  el.selectAllBtn.textContent = allSelected ? 'Ninguna' : 'Todas'
  el.selectAllBtn.setAttribute('aria-label', allSelected ? 'Deseleccionar todas las pantallas' : 'Seleccionar todas las pantallas')
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
  el.activeBadge.textContent = active
  el.scheduledBadge.textContent = scheduled
  el.inactiveBadge.textContent = inactive
  renderHosts()
  renderAssets()
  renderScreens()
  updateBulkBar()
}

function renderAssets() {
  const items = visibleAssets()
  if (!items.length) {
    resetPreviewHydration()
    el.assetsBody.innerHTML = `<tr><td colspan="7"><div class="empty-state"><strong>No hay contenidos que mostrar</strong><span>Prueba otro filtro o anade un contenido nuevo.</span></div></td></tr>`
    el.selectVisible.checked = false
    el.selectVisible.indeterminate = false
    return
  }

  const soloHosts = selectedHosts()
  const soloHost = soloHosts.length === 1 ? soloHosts[0] : null
  const orderableCount = soloHost ? playlistItems(soloHost).length : 0
  let previousHost = ''
  el.assetsBody.innerHTML = items.map(({ host, asset }) => {
    const id = assetId(asset)
    const key = assetKey(host, asset)
    const status = assetStatus(asset)
    const type = assetType(asset)
    const checked = state.selected.has(key) ? 'checked' : ''
    const useRichPreview = type.label === 'Imagen' || type.label === 'Video'
    const canOrder = soloHost === host && orderableCount > 1
    const groupHeader = !soloHost && state.screen === 'all' && host !== previousHost
      ? `<tr class="screen-group-row"><td colspan="7"><strong>${escapeHtml(hostLabel(host))}</strong><span>${allAssets().filter((item) => item.host === host).length} contenidos</span></td></tr>` : ''
    previousHost = host
    const preview = useRichPreview ? assetPreviewMarkup(host, asset, type, key) : `<span class="asset-thumb">${type.icon}</span>`
    return `${groupHeader}<tr data-key="${escapeHtml(key)}">
      <td class="check-column"><input class="row-check" type="checkbox" ${checked} aria-label="Seleccionar ${escapeHtml(asset.name || id)}"></td>
      <td><div class="asset-title ${useRichPreview ? 'with-preview' : ''}">${preview}<span><strong title="${escapeHtml(asset.name || '')}">${escapeHtml(asset.name || asset.title || 'Sin nombre')}</strong><small>${type.label} - ${statusLabel(status)}</small></span></div></td>
      <td><span class="host-chip" title="${escapeHtml(host)}">${escapeHtml(hostRecord(host).name)}</span></td>
      <td class="schedule-cell"><span>${formatDate(asset.start_date, 'Sin inicio')}</span><small>hasta ${formatDate(asset.end_date, 'sin limite')}</small></td>
      <td>${formatDuration(asset.duration)}</td>
      <td><span class="status-pill ${status}">${statusLabel(status)}</span></td>
      <td class="actions-column"><div class="row-actions">
        ${canOrder ? `<span class="order-controls"><button class="row-button" type="button" data-action="move-up" data-key="${escapeHtml(key)}" title="Subir en la playlist" aria-label="Subir en la playlist">&#8593;</button><button class="row-button" type="button" data-action="move-down" data-key="${escapeHtml(key)}" title="Bajar en la playlist" aria-label="Bajar en la playlist">&#8595;</button></span>` : ''}
        ${canOperate() ? `<button class="row-button" type="button" data-action="toggle" data-host="${host}" data-id="${escapeHtml(id)}" title="${isEnabled(asset) ? 'Desactivar' : 'Activar'}" aria-label="${isEnabled(asset) ? 'Desactivar' : 'Activar'}">${isEnabled(asset) ? '&#10074;&#10074;' : '&#9654;'}</button>` : ''}
        ${canOperate() ? `<button class="row-button" type="button" data-action="edit" data-key="${escapeHtml(key)}" title="Editar" aria-label="Editar">&#9998;</button>` : ''}
        <button class="row-button" type="button" data-action="download" data-key="${escapeHtml(key)}" title="Descargar" aria-label="Descargar">&#8681;</button>
        ${canAdmin() ? `<button class="row-button danger" type="button" data-action="delete" data-host="${host}" data-id="${escapeHtml(id)}" title="Eliminar" aria-label="Eliminar">&#10005;</button>` : ''}
      </div></td>
    </tr>`
  }).join('')
  const selectedVisible = items.filter((item) => state.selected.has(assetKey(item.host, item.asset))).length
  el.selectVisible.checked = selectedVisible === items.length
  el.selectVisible.indeterminate = selectedVisible > 0 && selectedVisible < items.length
  hydrateAssetPreviews()
}

function assetMediaUrl(host, asset) {
  return `/api/asset-media/${encodeURIComponent(host)}/${encodeURIComponent(assetId(asset))}`
}

function liveMediaUrl(host, asset) {
  return `/api/live-media/${encodeURIComponent(host)}/${encodeURIComponent(assetId(asset))}`
}

function assetPreviewMarkup(host, asset, type, key) {
  if (type.label === 'Imagen') {
    return `<span class="asset-media loading"><img class="asset-preview-media" data-src="${liveMediaUrl(host, asset)}" data-fallback="${assetMediaUrl(host, asset)}" loading="lazy" alt="Vista previa de ${escapeHtml(asset.name || asset.title || 'contenido')}"></span>`
  }
  if (type.label === 'Video') {
    return `<span class="asset-media video loading"><video class="asset-preview-media asset-preview-video" data-key="${escapeHtml(key)}" data-src="${liveMediaUrl(host, asset)}" data-fallback="${assetMediaUrl(host, asset)}" muted playsinline preload="none"></video></span>`
  }
  return `<span class="asset-thumb">${type.icon}</span>`
}

function resetPreviewHydration() {
  if (previewHydrationTimer) clearTimeout(previewHydrationTimer)
  previewHydrationTimer = null
  if (previewObserver) previewObserver.disconnect()
  previewObserver = null
  pendingPreviews = []
  activePreviewLoads = 0
}

function enqueuePreview(media) {
  if (!media || media.dataset.previewQueued === '1' || media.dataset.previewLoaded === '1') return
  media.dataset.previewQueued = '1'
  pendingPreviews.push(media)
  flushPreviewQueue()
}

function flushPreviewQueue() {
  while (activePreviewLoads < MAX_PARALLEL_PREVIEWS && pendingPreviews.length) {
    const media = pendingPreviews.shift()
    if (!media?.isConnected || media.dataset.previewLoaded === '1') continue

    activePreviewLoads += 1
    media.dataset.previewLoaded = '1'
    const wrapper = media.parentElement
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      wrapper?.classList.remove('loading')
      activePreviewLoads = Math.max(0, activePreviewLoads - 1)
      flushPreviewQueue()
    }
    const retryFallback = () => {
      if (!media.dataset.fallback || media.dataset.fallbackTried === '1') return false
      media.dataset.fallbackTried = '1'
      media.src = media.dataset.fallback
      if (media.tagName === 'VIDEO') media.load()
      return true
    }

    if (media.tagName === 'IMG') {
      media.addEventListener('load', finish, { once: true })
      const failImage = () => {
        if (!retryFallback()) finish()
      }
      media.addEventListener('error', failImage)
      media.src = media.dataset.src || ''
    } else {
      media.addEventListener('loadedmetadata', () => {
        const duration = Number(media.duration || 0)
        const target = Number.isFinite(duration) && duration > 2 ? Math.min(2, duration * 0.08) : 0
        if (target > 0) {
          try { media.currentTime = target } catch {}
        }
      }, { once: true })
      media.addEventListener('loadeddata', () => {
        try { media.pause() } catch {}
        finish()
      }, { once: true })
      media.addEventListener('error', () => {
        if (!retryFallback()) finish()
      })
      media.preload = 'metadata'
      media.src = media.dataset.src || ''
      media.load()
    }
  }
}

function hydrateAssetPreviews() {
  resetPreviewHydration()
  const previews = [...el.assetsBody.querySelectorAll('.asset-preview-media')]
  if (!previews.length) return

  previewHydrationTimer = window.setTimeout(() => {
    if ('IntersectionObserver' in window) {
      previewObserver = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return
          enqueuePreview(entry.target)
          previewObserver?.unobserve(entry.target)
        })
      }, { rootMargin: '180px 0px' })
      previews.forEach((preview) => previewObserver.observe(preview))
      return
    }
    previews.slice(0, MAX_PARALLEL_PREVIEWS * 2).forEach((preview) => enqueuePreview(preview))
  }, 120)
}

function applyPlaylistOrderLocally(host, orderedIds) {
  const result = state.results.find((entry) => entry.host === host && entry.ok)
  if (!result?.assets) return
  const positions = new Map(orderedIds.map((assetIdValue, index) => [String(assetIdValue), index]))
  result.assets = [...result.assets]
    .sort((left, right) => {
      const leftPosition = positions.get(assetId(left))
      const rightPosition = positions.get(assetId(right))
      return (leftPosition ?? Number(left.play_order || 0)) - (rightPosition ?? Number(right.play_order || 0))
    })
    .map((asset, index) => ({ ...asset, play_order: positions.get(assetId(asset)) ?? index }))
}

function renderScreens() {
  if (!state.results.length) {
    el.statusGrid.innerHTML = '<div class="empty-state"><strong>Sin datos de pantallas</strong><span>Pulsa actualizar para consultar la flota.</span></div>'
    return
  }
  el.statusGrid.innerHTML = state.results.map((result) => {
    const assets = result.assets || []
    const record = hostRecord(result.host)
    const active = assets.filter((asset) => assetStatus(asset) === 'active').length
    const maintenance = Boolean(record.maintenance)
    const connectionLabel = maintenance ? 'Mantenimiento' : result.ok ? 'En linea' : result.authRequired ? 'API protegida' : 'Sin conexion'
    const connectionClass = maintenance ? 'maintenance' : result.ok ? 'active' : result.authRequired ? 'scheduled' : 'inactive'
    return `<article class="screen-card">
      <div class="screen-card-head"><div><h3>${escapeHtml(record.name)}</h3><p>${result.host}</p></div><span class="status-pill ${connectionClass}">${connectionLabel}</span></div>
      <div class="screen-card-stats"><div><strong>${result.ok ? `API ${result.version}` : '-'}</strong><small>Version detectada</small></div><div><strong>${result.ok ? active : '-'}</strong><small>En emision</small></div><div><strong>${result.ok ? assets.length : '-'}</strong><small>Totales</small></div><div><strong>${maintenance ? 'Pausada' : result.ok ? 'Disponible' : result.authRequired ? 'Credenciales' : 'Revisar red'}</strong><small>Estado</small></div></div>
      ${result.ok ? '' : `<p class="screen-error" title="${escapeHtml(result.error || '')}">${escapeHtml(result.error || 'No se ha podido consultar esta pantalla.')}</p>`}
      <button class="manage-screen secondary-button" type="button" data-manage-host="${result.host}">Administrar esta pantalla</button>
    </article>`
  }).join('')
}

function renderOps() {
  const diagnostics = state.diagnostics
  if (!diagnostics.length) {
    el.diagnosticsList.innerHTML = '<div class="empty-state"><strong>Sin diagnostico reciente</strong><span>Ejecuta una revision para ver API, red y agente.</span></div>'
    el.alertsList.innerHTML = '<div class="empty-state"><strong>Sin alertas cargadas</strong><span>Las incidencias apareceran aqui.</span></div>'
  } else {
    el.diagnosticsList.innerHTML = diagnostics.map((item) => {
      const record = hostRecord(item.host)
      const cls = item.severity === 'ok' ? 'active' : item.severity === 'maintenance' ? 'maintenance' : item.severity === 'warning' ? 'scheduled' : 'inactive'
      const monitor = item.monitor?.connected ? 'Agente conectado' : monitorHint(item.monitor)
      return `<article class="diagnostic-row ${cls}">
        <div><strong>${escapeHtml(record.name)}</strong><small>${escapeHtml(item.host)} - ${escapeHtml(item.message || item.error || '')}</small></div>
        <span class="status-pill ${cls}">${diagnosticStatus(item)}</span>
        <small>${escapeHtml(monitor)}</small>
        <button class="secondary-button compact" type="button" data-agent-host="${item.host}">Agente</button>
        <button class="secondary-button compact" type="button" data-manage-host="${item.host}">Administrar</button>
      </article>`
    }).join('')
    const alerts = diagnostics.filter((item) => !item.ok || item.severity === 'maintenance')
    el.alertsList.innerHTML = alerts.length ? alerts.map((item) => {
      const record = hostRecord(item.host)
      return `<article class="alert-row ${item.severity === 'error' ? 'danger' : 'warn'}">
        <strong>${escapeHtml(record.name)}</strong>
        <span>${escapeHtml(item.message || item.error || 'Revisar pantalla')}</span>
      </article>`
    }).join('') : '<div class="empty-state"><strong>Sin alertas</strong><span>Las pantallas seleccionadas estan sin incidencias criticas.</span></div>'
  }
  el.historyList.innerHTML = state.history.length ? state.history.map((event) => `
    <article class="history-row">
      <time>${formatDate(event.time, '')}</time>
      <strong>${escapeHtml(event.message || event.kind)}</strong>
      <small>${escapeHtml(event.host || 'Panel')}</small>
    </article>`).join('') : '<div class="empty-state"><strong>Sin historial</strong><span>Las acciones importantes se registraran aqui.</span></div>'
  renderUsers()
}

function roleLabel(role) {
  return role === 'admin' ? 'Administrador' : role === 'operator' ? 'Operador' : 'Solo lectura'
}

function renderUsers() {
  if (!el.usersPanel || !el.usersList) return
  el.usersPanel.hidden = !canAdmin()
  if (!canAdmin()) return
  if (!state.users.length) {
    el.usersList.innerHTML = '<div class="empty-manage"><strong>Sin usuarios cargados</strong><span>Actualiza el centro operativo para consultar las cuentas.</span></div>'
    return
  }
  el.usersList.innerHTML = state.users.map((user) => `
    <article class="user-row" data-user-email="${escapeHtml(user.email)}">
      <div class="user-identity">
        <strong>${escapeHtml(user.email)}</strong>
        <small>${user.active ? 'Acceso activo' : 'Acceso bloqueado'} - ${roleLabel(user.role)}</small>
      </div>
      <label>Rol
        <select class="user-role-input">
          <option value="admin"${user.role === 'admin' ? ' selected' : ''}>Administrador</option>
          <option value="operator"${user.role === 'operator' ? ' selected' : ''}>Operador</option>
          <option value="viewer"${user.role === 'viewer' ? ' selected' : ''}>Solo lectura</option>
        </select>
      </label>
      <label class="user-active-toggle"><span>Activo</span><input class="user-active-input" type="checkbox" ${user.active ? 'checked' : ''}></label>
      <label>Nueva contrasena<input class="user-password-input" type="password" autocomplete="new-password" placeholder="Dejar igual"></label>
      <div class="user-actions">
        <button class="secondary-button compact" type="button" data-user-action="save" data-email="${escapeHtml(user.email)}">Guardar</button>
        <button class="row-button danger" type="button" data-user-action="delete" data-email="${escapeHtml(user.email)}" title="Eliminar usuario" aria-label="Eliminar usuario">&#10005;</button>
      </div>
    </article>`).join('')
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
    if (!result.ok) return monitorShell(record, host, `<div class="monitor-placeholder error"><span>!</span><strong>Screenly no responde</strong><small>${escapeHtml(result.error || 'Revisa la red y que la Raspberry este encendida')}</small></div>`, 'Sin conexion', 'inactive')
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
    } else if (result.previewStream) {
      visual = `<img class="monitor-image" src="${escapeHtml(result.previewStream)}" alt="Vista actual de ${name}">`
    } else if (result.previewUrl && String(result.previewUrl).startsWith('http')) {
      visual = `<img class="monitor-image" src="${escapeHtml(result.previewUrl)}" alt="Vista actual de ${name}">`
    } else {
      visual = `<div class="monitor-video"><span class="video-glyph">&#9654;</span><span>${escapeHtml(type.label)}</span><small>${escapeHtml(monitorHint(result.monitor))}</small></div>`
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

function monitorHint(monitor) {
  if (!monitor || monitor.reason === 'unconfigured') return 'Instala el agente para ver imagen y posicion sincronizadas'
  if (monitor.reason === 'unauthorized') return 'El agente responde, pero su token no coincide'
  if (!monitor.connected || monitor.reason === 'unreachable') return 'El agente no responde en el puerto 8765'
  if (monitor.reason === 'player_unavailable') return 'El agente esta activo, pero OMXPlayer no responde'
  if (monitor.reason === 'asset_not_local') return 'El contenido actual no es un archivo local'
  return 'El agente esta conectado, pero no ofrece telemetria para este contenido'
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
  el.bulkEnableBtn.hidden = !canOperate()
  el.bulkDisableBtn.hidden = !canOperate()
  el.bulkScheduleBtn.hidden = !canOperate()
  el.bulkDeleteBtn.hidden = !canAdmin()
}

async function readJsonResponse(response, fallback) {
  const text = await response.text()
  let data = {}
  try { data = text ? JSON.parse(text) : {} } catch { data = {} }
  if (!response.ok) throw new Error(data.error || fallback)
  return data
}

async function postJson(action, payload, options = {}) {
  const response = await fetch(`/api/${action}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    signal: options.signal,
  })
  return readJsonResponse(response, 'El servidor local no pudo completar la operacion')
}

async function fleetRequest(url, options = {}) {
  const response = await fetch(url, options)
  return readJsonResponse(response, 'No se pudo actualizar la flota')
}

async function loadHosts() {
  const data = await fleetRequest('/api/hosts')
  state.hosts = Array.isArray(data.hosts) ? data.hosts : []
  const allowed = new Set(fleetHosts())
  state.results = state.results.filter((result) => allowed.has(result.host))
  if (state.screen !== 'all' && !allowed.has(state.screen)) state.screen = 'all'
  renderHosts()
  renderAuthScopeNote()
  updatePageContext()
}

async function loadSession() {
  try {
    const data = await fleetRequest('/api/session')
    state.role = data.role || 'admin'
  } catch {
    state.role = 'admin'
  }
  el.addAssetBtn.hidden = !canOperate() || state.view !== 'library'
  el.manageHostsBtn.hidden = !canAdmin()
  if (el.usersPanel) el.usersPanel.hidden = !canAdmin()
}

async function loadSettings() {
  try {
    const data = await fleetRequest('/api/settings')
    state.settings = data.settings || state.settings
    const auth = state.settings.defaultAuth || {}
    el.useAuth.checked = Boolean(auth.enabled)
    el.authFields.hidden = !el.useAuth.checked
    el.username.value = auth.username || ''
    el.password.value = ''
    el.password.placeholder = auth.hasPassword
      ? 'Contrasena ya guardada. Escribe aqui solo si quieres cambiarla'
      : 'Escribe una contrasena para guardarla'
    if (auth.apiVersion) el.apiVersion.value = auth.apiVersion
    renderGlobalAuthStatus()
  } catch {
    renderGlobalAuthStatus()
  }
}

function renderGlobalAuthStatus() {
  const auth = globalAuthRecord()
  if (!auth.enabled) {
    el.globalAuthStatus.textContent = 'Sin credenciales guardadas.'
    renderAuthScopeNote()
    return
  }
  el.globalAuthStatus.textContent = auth.hasPassword
    ? `Globales guardadas para las Raspberry sin acceso propio: ${auth.username || 'usuario'}. No hace falta volver a escribir la contrasena.`
    : `Hay un usuario global guardado (${auth.username || 'sin usuario'}), pero falta la contrasena.`
  renderAuthScopeNote()
}

function renderAuthScopeNote() {
  if (!el.authScopeNote) return
  const hosts = selectedHosts()
  if (hosts.length === 1) {
    const authState = hostAuthState(hosts[0])
    el.authScopeNote.textContent = `${hostRecord(hosts[0]).name}: ${authState.hint}`
    return
  }
  el.authScopeNote.textContent = 'Configura un acceso global o usa uno propio por Raspberry.'
}

async function saveGlobalAuth() {
  try {
    const auth = {
      enabled: el.useAuth.checked,
      username: el.username.value.trim(),
      apiVersion: el.apiVersion.value,
    }
    if (el.password.value) auth.password = el.password.value
    const data = await fleetRequest('/api/settings', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ defaultAuth: auth }),
    })
    state.settings = data.settings || state.settings
    renderGlobalAuthStatus()
    renderHosts()
    toast('Credenciales guardadas', 'Se usaran en las Raspberry sin credenciales propias.')
    await refreshActiveView(false)
  } catch (error) {
    toast('No se pudo guardar', error.message, 'error')
  }
}

async function clearGlobalAuth() {
  try {
    const data = await fleetRequest('/api/settings', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ defaultAuth: { enabled: false, clearPassword: true } }),
    })
    state.settings = data.settings || state.settings
    el.useAuth.checked = false
    el.password.value = ''
    el.authFields.hidden = true
    renderGlobalAuthStatus()
    renderHosts()
    toast('Credenciales quitadas', 'El panel dejara de usar credenciales globales.')
    await refreshActiveView(false)
  } catch (error) {
    toast('No se pudo quitar', error.message, 'error')
  }
}

function renderHostManager() {
  if (!el.hostManageList) return
  if (!state.hosts.length) {
    el.hostManageList.innerHTML = '<div class="empty-manage"><strong>No hay pantallas</strong><span>Anade la primera Raspberry con el formulario inferior.</span></div>'
    return
  }
  el.hostManageList.innerHTML = state.hosts.map(({ host, name, auth = {}, maintenance = false, notes = '' }) => `
    <div class="host-manage-row" data-host-row="${host}">
      <span class="host-manage-dot"></span>
      <label><span>Nombre</span><input class="host-name-input" value="${escapeHtml(name)}" maxlength="60"></label>
      <code>${host}</code>
      <label><span>API</span><select class="host-api-input"><option value="auto">Auto</option><option value="v1.2">v1.2</option><option value="v2">v2</option></select></label>
      <label class="host-auth-check"><span>Auth</span><input class="host-auth-enabled" type="checkbox" ${auth.enabled ? 'checked' : ''}></label>
      <label><span>Usuario propio</span><input class="host-auth-user" value="${escapeHtml(auth.username || '')}" placeholder="usa global si se deja vacio"></label>
      <label><span>Contrasena propia</span><input class="host-auth-password" type="password" placeholder="${auth.hasPassword ? 'Guardada' : 'usa global'}"></label>
      <label class="host-auth-check"><span>Mantenimiento</span><input class="host-maintenance" type="checkbox" ${maintenance ? 'checked' : ''}></label>
      <label class="host-notes"><span>Notas</span><input class="host-notes-input" value="${escapeHtml(notes)}" maxlength="240" placeholder="Ubicacion o incidencia"></label>
      <small class="host-credentials-note"></small>
      <div class="host-row-actions">
        <button class="row-button" type="button" data-host-action="rename" data-host="${host}" title="Guardar pantalla" aria-label="Guardar pantalla">&#10003;</button>
        <button class="row-button" type="button" data-host-action="use-global" data-host="${host}" title="Usar credenciales globales" aria-label="Usar credenciales globales">&#8634;</button>
        <button class="row-button danger" type="button" data-host-action="remove" data-host="${host}" title="Eliminar pantalla" aria-label="Eliminar pantalla">&#10005;</button>
      </div>
    </div>`).join('')
  state.hosts.forEach(({ host, auth = {} }) => {
    const row = el.hostManageList.querySelector(`[data-host-row="${CSS.escape(host)}"]`)
    const select = row?.querySelector('.host-api-input')
    if (select) select.value = auth.apiVersion || 'auto'
    if (row) syncHostManageRow(row, host)
  })
}

function syncHostManageRow(row, host) {
  const enabled = row.querySelector('.host-auth-enabled')?.checked
  const user = row.querySelector('.host-auth-user')
  const password = row.querySelector('.host-auth-password')
  if (user) user.disabled = !enabled
  if (password) password.disabled = !enabled
  const note = row.querySelector('.host-credentials-note')
  const authState = hostAuthState(host)
  if (!note) return
  if (enabled) {
    note.textContent = password?.value || hostRecord(host).auth?.hasPassword
      ? 'Esta Raspberry usara sus credenciales propias guardadas.'
      : 'Activa las credenciales propias y escribe una contrasena para reemplazar la actual.'
    return
  }
  note.textContent = authState.mode === 'global'
    ? `Esta Raspberry heredara las globales guardadas (${globalAuthRecord().username || 'usuario'}).`
    : 'Sin credenciales propias: usara las globales si existen.'
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
    await refreshFleet(false)
  } catch (error) {
    toast('No se pudo anadir', error.message, 'error')
  }
}

async function renameFleetHost(host, row) {
  const input = row.querySelector('.host-name-input')
  try {
    const password = row.querySelector('.host-auth-password').value
    const auth = {
      enabled: row.querySelector('.host-auth-enabled').checked,
      username: row.querySelector('.host-auth-user').value.trim(),
      apiVersion: row.querySelector('.host-api-input').value,
    }
    if (password) auth.password = password
    const data = await fleetRequest(`/api/hosts/${encodeURIComponent(host)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: input.value.trim(),
        maintenance: row.querySelector('.host-maintenance').checked,
        notes: row.querySelector('.host-notes-input').value.trim(),
        auth,
      }),
    })
    await loadHosts()
    toast('Pantalla actualizada', data.host.name)
  } catch (error) {
    toast('No se pudo guardar', error.message, 'error')
  }
}

async function useGlobalForHost(host) {
  try {
    const data = await fleetRequest(`/api/hosts/${encodeURIComponent(host)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        auth: { enabled: false, username: '', clearPassword: true, apiVersion: 'auto' },
      }),
    })
    await loadHosts()
    toast('Credenciales individuales quitadas', `${data.host.name} volvera a usar las globales si existen.`)
    await refreshActiveView(false)
  } catch (error) {
    toast('No se pudo cambiar', error.message, 'error')
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
    if (selectedHosts().length) await refreshFleet(false); else render()
  } catch (error) {
    toast('No se pudo eliminar', error.message, 'error')
  }
}

async function refreshFleet(showToast = true) {
  const hosts = selectedHosts()
  const token = ++refreshToken
  if (fleetAbortController) fleetAbortController.abort()
  fleetAbortController = new AbortController()
  if (!hosts.length) {
    state.results = []
    render()
    setLoading(false)
    return
  }
  setLoading(true)
  try {
    const data = await postJson('list', { hosts, ...authPayload() }, { signal: fleetAbortController.signal })
    if (token !== refreshToken) return
    state.results = data.results || []
    state.selected.clear()
    el.lastUpdated.textContent = `Actualizado ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
    render()
    const online = state.results.filter((result) => result.ok).length
    if (showToast) toast('Consulta terminada', `${online} de ${state.results.length} pantallas en linea.`, online ? 'success' : 'error')
  } catch (error) {
    if (token !== refreshToken) return
    if (error.name !== 'AbortError') toast('No se pudo actualizar', error.message, 'error')
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
    if (!document.hidden && !document.querySelector('dialog[open]')) refreshNowPlaying(false)
  }, 1500)
}

function refreshActiveView(showToast = true) {
  if (state.view === 'monitor') return refreshNowPlaying(showToast)
  if (state.view === 'ops') return refreshDiagnostics(showToast)
  return refreshFleet(showToast)
}

function scheduleSelectionRefresh() {
  if (selectionRefreshTimer) clearTimeout(selectionRefreshTimer)
  selectionRefreshTimer = setTimeout(() => {
    selectionRefreshTimer = null
    refreshActiveView(false)
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
  el.uploadStatus.textContent = ''
  el.assetDialog.showModal()
}

async function submitAsset(event) {
  event.preventDefault()
  const hosts = selectedHosts()
  if (!hosts.length) return toast('Sin destinos', 'Selecciona al menos una pantalla.', 'error')
  if (!validSchedule(el.startDate.value, el.endDate.value)) return toast('Programacion no valida', 'La fecha de fin debe ser posterior a la fecha de inicio.', 'error')
  el.submitAssetBtn.disabled = true
  el.submitAssetBtn.textContent = 'Enviando...'
  el.assetForm.setAttribute('aria-busy', 'true')
  el.uploadStatus.textContent = `Enviando a ${hosts.length} pantalla${hosts.length === 1 ? '' : 's'}. No cierres esta ventana.`
  try {
    let payload
    if (state.source === 'file') {
      if (!el.assetFile.files[0]) throw new Error('Selecciona un archivo de video o imagen.')
      const form = new FormData(el.assetForm)
      const auth = authPayload()
      form.set('hosts', hosts.join(','))
      form.set('apiVersion', auth.apiVersion)
      form.delete('username')
      form.delete('password')
      if (auth.username && auth.password) {
        form.set('username', auth.username)
        form.set('password', auth.password)
      }
      form.set('enabled', el.enabled.checked ? '1' : '0')
      form.set('skipAssetCheck', '1')
      form.set('duplicatePolicy', el.avoidDuplicates.checked ? 'skip' : 'allow')
      payload = await uploadWithProgress(form, hosts.length)
    } else {
      if (!el.assetUrl.value.trim()) throw new Error('Indica una direccion web valida.')
      payload = await postJson('url', {
        hosts, url: el.assetUrl.value.trim(), name: el.assetName.value.trim(),
        startDate: el.startDate.value, endDate: el.endDate.value,
        duration: Number(el.duration.value || 0), enabled: el.enabled.checked,
        duplicatePolicy: el.avoidDuplicates.checked ? 'skip' : 'allow',
        ...authPayload(),
      })
    }
    const { results, ok, failed, details } = resultSummary(payload, hosts.length)
    if (failed.length || results.length !== hosts.length) {
      await refreshFleet(false)
      throw new Error(`${ok} de ${hosts.length} pantallas completadas.${details ? ` ${details}` : ''}`)
    }
    toast('Contenido anadido', `Disponible en ${ok} pantalla${ok === 1 ? '' : 's'}.`, 'success')
    el.assetDialog.close()
    el.assetForm.reset()
    el.endDate.value = '9999-01-01T00:00'
    el.enabled.checked = true
    el.avoidDuplicates.checked = true
    await refreshFleet(false)
  } catch (error) {
    toast('No se pudo anadir', error.message, 'error')
    el.uploadStatus.textContent = error.message
  } finally {
    el.submitAssetBtn.disabled = false
    el.submitAssetBtn.textContent = 'Anadir contenido'
    el.assetForm.removeAttribute('aria-busy')
  }
}

function uploadWithProgress(form, hostCount) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/upload')
    xhr.upload.addEventListener('progress', (event) => {
      if (!event.lengthComputable) return
      const percent = Math.round((event.loaded / event.total) * 100)
      el.uploadStatus.textContent = `Subiendo al servidor ${percent}%. Despues se enviara a ${hostCount} pantalla${hostCount === 1 ? '' : 's'}.`
    })
    xhr.addEventListener('load', () => {
      let payload = {}
      try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : {} } catch { payload = {} }
      if (xhr.status < 200 || xhr.status >= 300) reject(new Error(payload.error || 'Error durante la subida'))
      else resolve(payload)
    })
    xhr.addEventListener('error', () => reject(new Error('No se pudo comunicar con el servidor durante la subida')))
    xhr.send(form)
  })
}

async function runTargets(operation, targets, extra = {}) {
  if (!targets.length) return
  setBulkBusy(true)
  try {
    const data = await postJson('asset', { operation, targets, ...extra, ...authPayload() })
    const { ok, failed, details } = resultSummary(data, targets.length)
    const type = failed.length ? (ok ? 'warning' : 'error') : 'success'
    const message = `${ok} de ${targets.length} cambios completados.${details ? ` ${details}` : ''}`
    toast(failed.length ? 'Operacion incompleta' : 'Operacion terminada', message, type)
    await refreshFleet(false)
  } catch (error) {
    toast('No se pudo completar', error.message, 'error')
  } finally {
    setBulkBusy(false)
  }
}

function resultSummary(payload, expected) {
  const results = Array.isArray(payload.results) ? payload.results : []
  const ok = results.filter((item) => item.ok).length
  const failed = results.filter((item) => !item.ok)
  if (results.length < expected) failed.push({ host: 'Panel', error: 'Faltan respuestas de algunas pantallas' })
  const details = failed.slice(0, 3).map((item) => `${item.host}: ${item.error || 'Error desconocido'}`).join(' | ')
  return { results, ok, failed, details }
}

function setBulkBusy(busy) {
  ;[el.bulkEnableBtn, el.bulkDisableBtn, el.bulkScheduleBtn, el.bulkDeleteBtn].forEach((button) => { button.disabled = busy })
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
  if (!validSchedule(el.editStartDate.value, el.editEndDate.value)) return toast('Programacion no valida', 'La fecha de fin debe ser posterior a la fecha de inicio.', 'error')
  const update = {
    name: el.editName.value.trim(), start_date: el.editStartDate.value,
    end_date: el.editEndDate.value, duration: Number(el.editDuration.value || 0),
    is_enabled: el.editEnabled.checked,
  }
  el.editDialog.close()
  await runTargets('update', targetFrom(el.editHost.value, el.editAssetId.value), { update })
}

function openBulkSchedule() {
  const targets = selectedTargets()
  if (!targets.length) return
  el.scheduleSummary.textContent = `${targets.length} contenidos seleccionados`
  el.bulkEndDate.value = '9999-01-01T00:00'
  el.bulkEnabled.checked = true
  el.scheduleDialog.showModal()
}

async function submitBulkSchedule(event) {
  event.preventDefault()
  if (!validSchedule(el.bulkStartDate.value, el.bulkEndDate.value)) return toast('Programacion no valida', 'La fecha de fin debe ser posterior a la fecha de inicio.', 'error')
  const update = {
    start_date: el.bulkStartDate.value,
    end_date: el.bulkEndDate.value,
    duration: Number(el.bulkDuration.value || 0),
    is_enabled: el.bulkEnabled.checked,
  }
  el.scheduleDialog.close()
  await runTargets('update', selectedTargets(), { update })
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
  renderAuthScopeNote()
  switchView('library')
  updatePageContext()
  refreshFleet(false)
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
  const ordered = playlistItems(item.host)
  const currentIndex = ordered.findIndex((candidate) => assetKey(candidate.host, candidate.asset) === key)
  const targetIndex = direction === 'up' ? currentIndex - 1 : currentIndex + 1
  if (currentIndex < 0 || targetIndex < 0 || targetIndex >= ordered.length) {
    return toast('Orden sin cambios', direction === 'up' ? 'El contenido ya es el primero.' : 'El contenido ya es el ultimo.')
  }
  const swapped = [...ordered]
  ;[swapped[currentIndex], swapped[targetIndex]] = [swapped[targetIndex], swapped[currentIndex]]
  const previousIds = ordered.map((candidate) => assetId(candidate.asset))
  const swappedIds = swapped.map((candidate) => assetId(candidate.asset))
  state.sort = 'playlist'
  el.sortSelect.value = 'playlist'
  applyPlaylistOrderLocally(item.host, swappedIds)
  renderAssets()
  try {
    await postJson('order', {
      hosts: [item.host], orderedIds: swappedIds, ...authPayload(),
    })
    toast('Playlist actualizada', `${item.asset.name || 'Contenido'} se ha movido.`)
    window.setTimeout(() => refreshFleet(false), 250)
  } catch (error) {
    applyPlaylistOrderLocally(item.host, previousIds)
    renderAssets()
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
    if (!document.hidden && !state.loading && !document.querySelector('dialog[open]')) refreshFleet(false)
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
  document.querySelectorAll('.nav-item').forEach((button) => {
    const active = button.dataset.view === view
    button.classList.toggle('active', active)
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current')
  })
  el.libraryView.classList.toggle('active', view === 'library')
  el.monitorView.classList.toggle('active', view === 'monitor')
  el.screensView.classList.toggle('active', view === 'screens')
  el.opsView.classList.toggle('active', view === 'ops')
  el.pageTitle.textContent = view === 'library' ? 'Biblioteca de contenidos' : view === 'monitor' ? 'En pantalla ahora' : view === 'screens' ? 'Estado de pantallas' : 'Centro operativo'
  el.pageSubtitle.textContent = view === 'library' ? `Gestion conjunta de ${selectedHosts().length} pantallas` : view === 'monitor' ? 'Supervision en directo de los reproductores seleccionados' : view === 'screens' ? 'Supervision de la flota local' : 'Diagnostico, alertas e historial'
  if (view === 'library') updatePageContext()
  else el.playbackControls.hidden = true
  el.addAssetBtn.hidden = view !== 'library' || !canOperate()
  if (view === 'monitor') {
    renderMonitor()
    refreshNowPlaying(false)
  }
  if (view === 'ops') {
    refreshDiagnostics(false)
    loadUsers()
  }
  configureMonitorTimer()
  closeSidebar()
}

async function refreshDiagnostics(showToast = true) {
  const hosts = selectedHosts()
  if (!hosts.length) {
    state.diagnostics = []
    renderOps()
    return
  }
  try {
    const data = await postJson('diagnostics', { hosts, ...authPayload() })
    state.diagnostics = data.results || []
    await loadHistory()
    renderOps()
    if (showToast) toast('Diagnostico terminado', `${state.diagnostics.length} pantallas revisadas.`)
  } catch (error) {
    toast('No se pudo diagnosticar', error.message, 'error')
  }
}

async function loadHistory() {
  try {
    const data = await fleetRequest('/api/history')
    state.history = data.events || []
  } catch {
    state.history = []
  }
}

async function loadUsers() {
  if (!canAdmin()) {
    state.users = []
    renderUsers()
    return
  }
  try {
    const data = await fleetRequest('/api/users')
    state.users = Array.isArray(data.users) ? data.users : []
  } catch (error) {
    state.users = []
    toast('No se pudieron cargar usuarios', error.message, 'error')
  }
  renderUsers()
}

async function addUser(event) {
  event.preventDefault()
  if (!canAdmin()) return
  try {
    const data = await fleetRequest('/api/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email: el.newUserEmail.value.trim(),
        password: el.newUserPassword.value,
        role: el.newUserRole.value,
        active: true,
      }),
    })
    el.userAddForm.reset()
    state.users.push(data.user)
    renderUsers()
    await loadHistory()
    renderOps()
    toast('Usuario creado', `${data.user.email} ya puede entrar como ${roleLabel(data.user.role)}.`, 'success')
  } catch (error) {
    toast('No se pudo crear usuario', error.message, 'error')
  }
}

async function saveUser(email, row) {
  if (!canAdmin()) return
  try {
    const password = row.querySelector('.user-password-input').value
    const payload = {
      role: row.querySelector('.user-role-input').value,
      active: row.querySelector('.user-active-input').checked,
    }
    if (password) payload.password = password
    const data = await fleetRequest(`/api/users/${encodeURIComponent(email)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    state.users = state.users.map((user) => user.email === email ? data.user : user)
    renderUsers()
    await loadHistory()
    renderOps()
    toast('Usuario actualizado', data.user.email, 'success')
  } catch (error) {
    toast('No se pudo guardar usuario', error.message, 'error')
  }
}

async function deleteUser(email) {
  if (!canAdmin()) return
  const accepted = await confirmAction('Eliminar usuario', `Se eliminara la cuenta ${email}.`, 'Eliminar')
  if (!accepted) return
  try {
    await fleetRequest(`/api/users/${encodeURIComponent(email)}`, { method: 'DELETE' })
    state.users = state.users.filter((user) => user.email !== email)
    renderUsers()
    await loadHistory()
    renderOps()
    toast('Usuario eliminado', email)
  } catch (error) {
    toast('No se pudo eliminar usuario', error.message, 'error')
  }
}

function toggleSidebar() {
  const sidebar = document.querySelector('.sidebar')
  const open = !sidebar.classList.contains('open')
  sidebar.classList.toggle('open', open)
  sidebar.dataset.open = String(open)
  sidebar.style.transform = open ? 'translateX(0)' : ''
  el.sidebarBackdrop.classList.toggle('visible', open)
  el.sidebarBackdrop.dataset.visible = String(open)
  el.sidebarBackdrop.style.visibility = open ? 'visible' : ''
  el.sidebarBackdrop.style.opacity = open ? '1' : ''
  el.sidebarBackdrop.style.pointerEvents = open ? 'auto' : ''
  el.mobileMenuBtn.setAttribute('aria-expanded', String(open))
}

function closeSidebar() {
  const sidebar = document.querySelector('.sidebar')
  sidebar.classList.remove('open')
  sidebar.dataset.open = 'false'
  sidebar.style.transform = ''
  el.sidebarBackdrop.classList.remove('visible')
  el.sidebarBackdrop.dataset.visible = 'false'
  el.sidebarBackdrop.style.visibility = ''
  el.sidebarBackdrop.style.opacity = ''
  el.sidebarBackdrop.style.pointerEvents = ''
  el.mobileMenuBtn.setAttribute('aria-expanded', 'false')
}

function switchSource(source) {
  state.source = source
  document.querySelectorAll('[data-source]').forEach((button) => button.classList.toggle('active', button.dataset.source === source))
  el.fileSource.classList.toggle('active', source === 'file')
  el.urlSource.classList.toggle('active', source === 'url')
}

function toast(title, message, type = 'success') {
  const node = document.createElement('div')
  node.className = `toast ${type}`
  node.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span>`
  el.toastRegion.append(node)
  while (el.toastRegion.children.length > 4) el.toastRegion.firstElementChild.remove()
  setTimeout(() => node.remove(), 4500)
}

function diagnosticLabel(result) {
  if (result.authRequired) return 'API protegida'
  if (Number(result.status) === 0) return 'Sin red'
  return result.error || 'Error API'
}

function diagnosticStatus(item) {
  if (item.maintenance) return 'Mantenimiento'
  if (item.ok) return 'Correcta'
  if (item.status === 'api_protegida') return 'API protegida'
  if (item.status === 'sin_red') return 'Sin red'
  return 'Revisar'
}

async function copyAgentCommand(host) {
  const command = `INSTALAR_AGENTE.bat`
  try {
    await navigator.clipboard.writeText(command)
    toast('Comando copiado', `Ejecuta ${command} y escribe la IP ${host} para actualizar el agente.`)
  } catch {
    toast('Actualizar agente', `Ejecuta INSTALAR_AGENTE.bat y escribe la IP ${host}.`)
  }
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

function formatBytes(value) {
  const bytes = Number(value || 0)
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function parseDate(value) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function validSchedule(startValue, endValue) {
  const start = parseDate(startValue)
  const end = parseDate(endValue)
  return !start || !end || end.getTime() >= start.getTime()
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
  document.querySelectorAll('[data-filter]').forEach((item) => {
    const active = item === button
    item.classList.toggle('active', active)
    item.setAttribute('aria-selected', String(active))
  })
  renderAssets()
}))

el.refreshBtn.addEventListener('click', () => refreshActiveView(true))
el.manageHostsBtn.addEventListener('click', () => { renderHostManager(); el.fleetDialog.showModal() })
el.editSelectedAuthBtn.addEventListener('click', () => {
  renderHostManager()
  el.fleetDialog.showModal()
  const hosts = selectedHosts()
  if (hosts.length === 1) {
    const row = el.hostManageList.querySelector(`[data-host-row="${CSS.escape(hosts[0])}"]`)
    row?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }
})
el.hostAddForm.addEventListener('submit', addFleetHost)
el.hostManageList.addEventListener('change', (event) => {
  const row = event.target.closest('[data-host-row]')
  if (!row) return
  syncHostManageRow(row, row.dataset.hostRow)
})
el.hostManageList.addEventListener('click', (event) => {
  const button = event.target.closest('[data-host-action]')
  if (!button) return
  const row = button.closest('[data-host-row]')
  if (button.dataset.hostAction === 'rename') renameFleetHost(button.dataset.host, row)
  if (button.dataset.hostAction === 'use-global') useGlobalForHost(button.dataset.host)
  if (button.dataset.hostAction === 'remove') removeFleetHost(button.dataset.host)
})
el.previousAssetBtn.addEventListener('click', () => controlPlayback('previous'))
el.nextAssetBtn.addEventListener('click', () => controlPlayback('next'))
el.addAssetBtn.addEventListener('click', openAssetDialog)
el.assetForm.addEventListener('submit', submitAsset)
el.editForm.addEventListener('submit', submitEdit)
el.scheduleForm.addEventListener('submit', submitBulkSchedule)
el.userAddForm.addEventListener('submit', addUser)
el.usersList.addEventListener('click', (event) => {
  const button = event.target.closest('[data-user-action]')
  if (!button) return
  const email = button.dataset.email
  const row = button.closest('.user-row')
  if (button.dataset.userAction === 'save') saveUser(email, row)
  if (button.dataset.userAction === 'delete') deleteUser(email)
})
el.mobileMenuBtn.addEventListener('click', toggleSidebar)
el.sidebarBackdrop.addEventListener('click', closeSidebar)
el.searchInput.addEventListener('input', () => { state.search = el.searchInput.value; renderAssets() })
el.screenFilter.addEventListener('change', () => { state.screen = el.screenFilter.value; renderAssets() })
el.sortSelect.addEventListener('change', () => { state.sort = el.sortSelect.value; renderAssets() })
el.autoRefreshToggle.addEventListener('change', configureAutoRefresh)
el.saveGlobalAuthBtn.addEventListener('click', saveGlobalAuth)
el.clearGlobalAuthBtn.addEventListener('click', clearGlobalAuth)
el.useAuth.addEventListener('change', () => {
  el.authFields.hidden = !el.useAuth.checked
  if (!el.useAuth.checked) el.password.value = ''
  refreshActiveView()
})
el.assetFile.addEventListener('change', () => {
  const file = el.assetFile.files[0]
  el.fileName.textContent = file ? `${file.name} - ${formatBytes(file.size)}` : 'MP4, MOV, WEBM, JPG o PNG'
  if (file && !el.assetName.value.trim()) el.assetName.value = file.name.replace(/\.[^.]+$/, '')
})
el.hostList.addEventListener('change', () => {
  state.selected.clear()
  state.screen = 'all'
  el.screenFilter.value = 'all'
  updateBulkBar()
  renderAuthScopeNote()
  updatePageContext()
  if (state.view === 'monitor') renderMonitor()
  renderAssets()
  el.targetSummary.textContent = `${selectedHosts().length} pantallas seleccionadas`
  updateSelectAllButton()
  scheduleSelectionRefresh()
})
el.hostList.addEventListener('click', (event) => {
  const button = event.target.closest('[data-solo]')
  if (button) manageOnly(button.dataset.solo)
})
el.selectAllBtn.addEventListener('click', () => {
  const checks = [...document.querySelectorAll('.host-check')]
  const select = checks.some((input) => !input.checked)
  checks.forEach((input) => { input.checked = select })
  state.screen = 'all'
  el.screenFilter.value = 'all'
  state.selected.clear()
  updateSelectAllButton()
  renderAuthScopeNote()
  updatePageContext()
  el.targetSummary.textContent = `${selectedHosts().length} pantallas seleccionadas`
  refreshActiveView(false)
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
el.bulkScheduleBtn.addEventListener('click', openBulkSchedule)
el.bulkDeleteBtn.addEventListener('click', async () => {
  const targets = selectedTargets()
  if (await confirmAction('Eliminar contenidos', `Se eliminaran ${targets.length} contenidos de sus pantallas.`)) runTargets('delete', targets)
})
el.statusGrid.addEventListener('click', (event) => {
  const button = event.target.closest('[data-manage-host]')
  if (button) manageOnly(button.dataset.manageHost)
})
el.runDiagnosticsBtn.addEventListener('click', () => refreshDiagnostics(true))
el.diagnosticsList.addEventListener('click', (event) => {
  const agentButton = event.target.closest('[data-agent-host]')
  if (agentButton) return copyAgentCommand(agentButton.dataset.agentHost)
  const button = event.target.closest('[data-manage-host]')
  if (button) manageOnly(button.dataset.manageHost)
})
el.monitorGrid.addEventListener('click', (event) => {
  const button = event.target.closest('[data-monitor-host]')
  if (button) manageOnly(button.dataset.monitorHost)
})
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeSidebar()
})
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.view === 'monitor') refreshNowPlaying(false)
})
el.logoutBtn.addEventListener('click', async () => {
  try {
    await fetch('/api/logout', { method: 'POST' })
  } finally {
    window.location.replace('/login')
  }
})

async function initialize() {
  try {
    await loadSession()
    await loadSettings()
    el.autoRefreshToggle.checked = localStorage.getItem('fleetboard-auto-refresh') === '1'
    configureAutoRefresh()
    await loadHosts()
    if (state.hosts.length) await refreshFleet(false)
    await loadHistory()
    if (!state.hosts.length) render()
  } catch (error) {
    toast('No se pudo iniciar', error.message, 'error')
  }
}

initialize()
