const state = {
  messages: {}, presets: {}, settings: {}, status: null, files: [], selectedFiles: new Set(),
  maps: {}, markers: {}, tractionDirection: null, tractionLine: null, predictionLine: null,
  mapLocks: { static: false, tractionStart: false, tractionDirection: false, preset: false },
  toastTimer: null, lastTaskId: null, lastTaskStatus: null, taskRequestPending: false,
  taskLogWasLong: false
};

function value(path, source = state.messages) {
  return path.split('.').reduce((current, key) => current?.[key], source);
}

function t(path, params = {}) {
  let message = value(path) ?? value('errors.unknown') ?? path;
  Object.entries(params).forEach(([key, item]) => { message = message.replace(`{${key}}`, item); });
  return message;
}

function applyTranslations() {
  document.querySelectorAll('[data-i18n]').forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll('[data-i18n-aria-label]').forEach((node) => {
    node.setAttribute('aria-label', t(node.dataset.i18nAriaLabel));
  });
}

function configureSettingLimits() {
  const limits = [
    ['#setting-frequency', 1000000, 6000000000, 1, 'limits.frequency'],
    ['#setting-sample-rate', 1000000, 20000000, 1, 'limits.sampleRate'],
    ['#setting-gain', 0, 47, 1, 'limits.txGain'],
    ['#setting-lna-gain', 0, 40, 8, 'limits.lnaGain'],
    ['#setting-vga-gain', 0, 62, 2, 'limits.vgaGain'],
    ['#setting-speed', 0.01, 100, 0.01, 'limits.defaultSpeed'],
    ['#setting-altitude', -1000, 100000, 0.1, 'limits.defaultAltitude'],
    ['#setting-static-duration', 1, 86400, 1, 'limits.staticDuration'],
    ['#setting-traction-duration', 55, 86400, 1, 'limits.tractionDuration'],
    ['#setting-update-rate', 10, 10, 1, null],
    ['#setting-drift-heading', 0, 360, 0.1, 'limits.driftHeading'],
    ['#setting-drift-jitter', 0, 1000, 0.1, 'limits.driftAltitudeJitter'],
    ['#setting-ephemeris-max-files', 1, 100, 1, 'limits.ephemerisMaxFiles']
  ];
  limits.forEach(([selector, minimum, maximum, step, hintKey]) => {
    const input = document.querySelector(selector);
    input.min = minimum;
    if (maximum === null) input.removeAttribute('max');
    else input.max = maximum;
    input.step = step;
    if (hintKey && !input.closest('label').querySelector('.limit-hint')) {
      const hint = document.createElement('small');
      hint.className = 'limit-hint'; hint.textContent = t(hintKey);
      input.closest('label').append(hint);
    }
  });
}

function toast(message, error = false) {
  const node = document.querySelector('#toast');
  node.textContent = message;
  node.classList.toggle('error', error);
  node.classList.add('show');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => node.classList.remove('show'), 3500);
}

async function api(path, options = {}) {
  const config = { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } };
  const response = await fetch(path, config);
  if (!response.ok) {
    let detail = 'unknown';
    try {
      const payload = await response.json();
      if (typeof payload.detail === 'string') {
        detail = payload.detail;
      } else if (Array.isArray(payload.detail)) {
        const messages = payload.detail.map((item) => item.msg || '').join(' ');
        const knownCodes = Object.keys(value('errors'));
        detail = knownCodes.find((code) => messages.includes(code)) || 'validation_failed';
      }
    } catch (_) { detail = 'unknown'; }
    throw new Error(t(`errors.${detail}`));
  }
  return response.status === 204 ? null : response.json();
}

function number(id) { return Number(document.querySelector(id).value); }
function setText(id, content) { document.querySelector(id).textContent = content; }
function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return t('status.none');
  const units = value('units.bytes');
  let size = bytes; let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.ceil(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return [hours, minutes, remainder]
    .filter((_, index) => hours > 0 || index > 0)
    .map((part) => String(part).padStart(2, '0'))
    .join(':');
}

function showPage(page) {
  document.querySelectorAll('.nav-item').forEach((node) => node.classList.toggle('active', node.dataset.page === page));
  document.querySelectorAll('[data-page-panel]').forEach((node) => node.classList.toggle('active', node.dataset.pagePanel === page));
  setText('#page-title', t(`pages.${page}.title`));
  setText('#page-description', t(`pages.${page}.description`));
  if (page === 'files' && state.messages.files) {
    loadFiles().catch((error) => toast(error.message, true));
  }
  setTimeout(() => Object.values(state.maps).forEach((map) => map.invalidateSize()), 50);
}

function createMap(id) {
  const map = L.map(id, { worldCopyJump: false }).setView([23.6978, 120.9605], 7);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: t('map.attribution')
  }).addTo(map);
  return map;
}

function normalizeMapPoint(latlng) {
  return {
    lat: Math.max(-90, Math.min(90, latlng.lat)),
    lon: ((latlng.lng + 180) % 360 + 360) % 360 - 180
  };
}

function longitudeNearMap(lon, map) {
  return lon + Math.round((map.getCenter().lng - lon) / 360) * 360;
}

function placeMarker(mapName, lat, lon) {
  const map = state.maps[mapName];
  if (!state.markers[mapName]) state.markers[mapName] = L.marker([lat, lon]).addTo(map);
  else state.markers[mapName].setLatLng([lat, lon]);
  map.panTo([lat, lon]);
}

function focusMapMarker(mapName) {
  const marker = state.markers[mapName];
  if (!marker) return;
  state.maps[mapName].setView(marker.getLatLng(), 16, { animate: true });
}

function setMapTip(id, messageKey, locked = false) {
  const tip = document.querySelector(id);
  setText(id, t(messageKey));
  tip.classList.toggle('locked', locked);
}

function updateMapLockButtons() {
  document.querySelector('#static-reset-position').disabled = !state.mapLocks.static;
  document.querySelector('#static-focus-position').disabled = !state.mapLocks.static;
  document.querySelector('#traction-reset-direction').disabled = !state.mapLocks.tractionDirection;
  document.querySelector('#traction-reset-all').disabled = !state.mapLocks.tractionStart;
  document.querySelector('#traction-focus-start').disabled = !state.mapLocks.tractionStart;
  document.querySelector('#preset-reset-position').disabled = !state.mapLocks.preset;
  document.querySelector('#preset-focus-position').disabled = !state.mapLocks.preset;
}

function setupMaps() {
  if (typeof L === 'undefined') { toast(t('messages.mapUnavailable'), true); return; }
  state.maps.static = createMap('static-map');
  state.maps.static.on('click', ({ latlng }) => {
    if (state.mapLocks.static) return;
    const point = normalizeMapPoint(latlng);
    document.querySelector('#static-lat').value = point.lat.toFixed(7);
    document.querySelector('#static-lon').value = point.lon.toFixed(7);
    document.querySelector('#static-preset').value = '';
    placeMarker('static', latlng.lat, latlng.lng);
    state.mapLocks.static = true;
    setMapTip('#static-map-tip', 'mapState.positionLocked', true);
    updateMapLockButtons();
  });
  state.maps.traction = createMap('traction-map');
  state.maps.traction.on('click', ({ latlng }) => {
    const point = normalizeMapPoint(latlng);
    const latInput = document.querySelector('#traction-lat');
    const lonInput = document.querySelector('#traction-lon');
    if (!state.mapLocks.tractionStart) {
      latInput.value = point.lat.toFixed(7); lonInput.value = point.lon.toFixed(7);
      placeMarker('traction', latlng.lat, latlng.lng);
      state.mapLocks.tractionStart = true;
      setText('#traction-map-tip', t('traction.clickDirection'));
      updateMapLockButtons();
      syncTractionDirectionFromHeading();
    } else if (!state.mapLocks.tractionDirection) {
      const heading = bearing(number('#traction-lat'), number('#traction-lon'), point.lat, point.lon);
      document.querySelector('#traction-heading').value = heading.toFixed(1);
      setTractionDirection(point);
    }
  });
  state.maps.preset = createMap('preset-map');
  state.maps.preset.on('click', ({ latlng }) => {
    if (state.mapLocks.preset) return;
    const point = normalizeMapPoint(latlng);
    document.querySelector('#preset-lat').value = point.lat.toFixed(7);
    document.querySelector('#preset-lon').value = point.lon.toFixed(7);
    placeMarker('preset', latlng.lat, latlng.lng);
    state.mapLocks.preset = true;
    setMapTip('#preset-map-tip', 'mapState.positionLocked', true);
    updateMapLockButtons();
  });
  updateMapLockButtons();
}

function removeMapLayer(name) {
  const layer = state.markers[name];
  if (layer) layer.remove();
  state.markers[name] = null;
}

function resetTractionDirection(clearHeading = true) {
  state.tractionDirection = null;
  state.mapLocks.tractionDirection = false;
  removeMapLayer('direction');
  if (state.tractionLine) state.tractionLine.remove();
  if (state.predictionLine) state.predictionLine.remove();
  state.tractionLine = null; state.predictionLine = null;
  if (clearHeading) document.querySelector('#traction-heading').value = '';
  setText('#traction-prediction', t('status.none'));
  document.querySelector('#traction-map-tip').classList.remove('locked');
  setText('#traction-map-tip', state.mapLocks.tractionStart ? t('traction.clickDirection') : t('traction.clickStart'));
  updateMapLockButtons();
}

function resetTractionStart() {
  resetTractionDirection();
  removeMapLayer('traction');
  state.mapLocks.tractionStart = false;
  document.querySelector('#traction-lat').value = '';
  document.querySelector('#traction-lon').value = '';
  document.querySelector('#traction-preset').value = '';
  setText('#traction-map-tip', t('traction.clickStart'));
  updateMapLockButtons();
}

function resetStaticPosition() {
  state.mapLocks.static = false;
  removeMapLayer('static');
  document.querySelector('#static-lat').value = '';
  document.querySelector('#static-lon').value = '';
  document.querySelector('#static-preset').value = '';
  setMapTip('#static-map-tip', 'mapState.selectPosition');
  updateMapLockButtons();
}

function resetPresetPosition() {
  state.mapLocks.preset = false;
  removeMapLayer('preset');
  document.querySelector('#preset-lat').value = '';
  document.querySelector('#preset-lon').value = '';
  setMapTip('#preset-map-tip', 'mapState.selectPosition');
  updateMapLockButtons();
}

function bearing(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const p1 = lat1 * rad; const p2 = lat2 * rad; const delta = (lon2 - lon1) * rad;
  return (Math.atan2(Math.sin(delta) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(delta)) / rad + 360) % 360;
}

function destination(lat, lon, heading, distance) {
  const radius = 6378137; const angular = distance / radius; const rad = Math.PI / 180;
  const p1 = lat * rad; const lambda1 = lon * rad; const theta = heading * rad;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(angular) + Math.cos(p1) * Math.sin(angular) * Math.cos(theta));
  const lambda2 = lambda1 + Math.atan2(Math.sin(theta) * Math.sin(angular) * Math.cos(p1), Math.cos(angular) - Math.sin(p1) * Math.sin(p2));
  return { lat: p2 / rad, lon: ((lambda2 / rad + 540) % 360) - 180 };
}

function setTractionDirection(point) {
  state.tractionDirection = point;
  if (!state.markers.direction) {
    state.markers.direction = L.circleMarker(
      [point.lat, point.lon],
      { radius: 7, color: '#d79b34' }
    ).addTo(state.maps.traction);
  } else {
    state.markers.direction.setLatLng([point.lat, point.lon]);
  }
  state.mapLocks.tractionDirection = true;
  document.querySelector('#traction-map-tip').classList.add('locked');
  setText('#traction-map-tip', t('traction.directionReady'));
  updateTractionPrediction();
  updateMapLockButtons();
}

function syncTractionDirectionFromHeading() {
  const input = document.querySelector('#traction-heading');
  const heading = Number(input.value);
  if (input.value === '' || !Number.isFinite(heading) || heading < 0 || heading > 360) {
    resetTractionDirection(false);
    return;
  }
  if (!state.mapLocks.tractionStart || !state.markers.traction) return;
  const point = destination(
    number('#traction-lat'),
    number('#traction-lon'),
    heading % 360,
    1000
  );
  setTractionDirection(point);
}

function tractionValues() {
  return { lat: number('#traction-lat'), lon: number('#traction-lon'), alt: number('#traction-alt'), speed: number('#traction-speed'), ramp: number('#traction-ramp'), duration: number('#traction-duration'), hold: number('#traction-hold'), final_hold: number('#traction-final-hold') };
}

function minimumTractionDuration() {
  return number('#traction-hold') + number('#traction-final-hold') + 2 * number('#traction-ramp');
}

function validateTractionDuration(showMessage = false) {
  const durationInput = document.querySelector('#traction-duration');
  const minimum = minimumTractionDuration();
  durationInput.min = minimum;
  const message = t('traction.minimumDuration', { seconds: Number(minimum.toFixed(1)) });
  durationInput.title = message;
  const valid = number('#traction-duration') >= minimum;
  durationInput.setCustomValidity(valid ? '' : message);
  if (!valid && showMessage) toast(message, true);
  return valid;
}

function updateTractionPrediction() {
  validateTractionDuration();
  if (!state.tractionDirection || !state.markers.traction) { setText('#traction-prediction', t('status.none')); return; }
  const form = tractionValues();
  const active = form.duration - form.hold - form.final_hold;
  const cruise = Math.max(0, active - 2 * form.ramp);
  const distance = Math.max(0, form.speed * (form.ramp + cruise));
  const heading = bearing(form.lat, form.lon, state.tractionDirection.lat, state.tractionDirection.lon);
  const end = destination(form.lat, form.lon, heading, distance);
  setText('#traction-prediction', `${end.lat.toFixed(7)}, ${end.lon.toFixed(7)} · ${t('messages.heading', { heading: heading.toFixed(1) })}`);
  const displayStartLon = longitudeNearMap(form.lon, state.maps.traction);
  const displayDirectionLon = longitudeNearMap(state.tractionDirection.lon, state.maps.traction);
  const displayEndLon = longitudeNearMap(end.lon, state.maps.traction);
  const directionPoints = [[form.lat, displayStartLon], [state.tractionDirection.lat, displayDirectionLon]];
  const predictionPoints = [[form.lat, displayStartLon], [end.lat, displayEndLon]];
  if (!state.tractionLine) state.tractionLine = L.polyline(directionPoints, { color: '#d79b34', dashArray: '6 8' }).addTo(state.maps.traction);
  else state.tractionLine.setLatLngs(directionPoints);
  if (!state.predictionLine) state.predictionLine = L.polyline(predictionPoints, { color: '#146b4a', weight: 4 }).addTo(state.maps.traction);
  else state.predictionLine.setLatLngs(predictionPoints);
}

async function loadHardware(notify = false) {
  const refreshButton = document.querySelector('#refresh-hardware');
  refreshButton.disabled = true;
  setText('#hardware-pill span:last-child', t('hardware.checking'));
  try {
    const hardware = await api('/api/hardware');
    const key = hardware.connected ? 'connected' : hardware.mode === 'dfu' ? 'dfuMode' : hardware.installed ? 'disconnected' : 'notInstalled';
    const label = t(`hardware.${key}`);
    setText('#hardware-pill span:last-child', label); setText('#overview-hardware', label);
    setText('#hardware-detail', t(hardware.connected ? 'hardware.ready' : hardware.mode === 'dfu' ? 'hardware.dfuUnavailable' : 'hardware.unavailable'));
    document.querySelector('#hardware-pill .dot').className = `dot ${hardware.connected ? 'live' : 'error'}`;
    const details = hardware.details || {};
    const fields = {
      '#hardware-board-id': details.board_id,
      '#hardware-firmware': details.firmware_version,
      '#hardware-revision': details.hardware_revision
    };
    Object.entries(fields).forEach(([selector, content]) => setText(selector, content || t('status.none')));
    setText('#hardware-output', hardware.output || t('status.none'));
    if (notify) toast(t('messages.hardwareRefreshed'));
  } catch (error) {
    const label = t('hardware.disconnected');
    setText('#hardware-pill span:last-child', label);
    setText('#overview-hardware', label);
    setText('#hardware-detail', t('hardware.unavailable'));
    document.querySelector('#hardware-pill .dot').className = 'dot error';
    toast(error.message, true);
  } finally {
    refreshButton.disabled = false;
  }
}

async function loadSoftwareInfo() {
  try {
    const software = await api('/api/software');
    setText('#software-name', software.name);
    setText('#software-version', software.version_label || software.version);
    setText('#system-software-version', software.version);
    document.title = software.name;
  } catch (_) {
    setText('#software-version', '');
    setText('#system-software-version', t('status.none'));
  }
}

function renderStatus(payload) {
  state.status = payload;
  const task = payload.task;
  const taskLabel = t(`task.${task.status}`);
  setText('#overview-task', taskLabel); setText('#task-kind', task.kind ? t(`task.kinds.${task.kind}`) : t('status.none'));
  setText('#task-badge', taskLabel); document.querySelector('#task-badge').className = `badge ${task.status}`;
  const logs = task.logs || [];
  const logText = logs.length ? logs.map((entry) => entry.message).join('\n') : t('task.noLogs');
  setText('#task-log', logText);
  const taskLog = document.querySelector('.task-output');
  const logIsLong = logText.length > 1200 || logText.split('\n').length > 8;
  if (logIsLong !== state.taskLogWasLong || task.id !== state.lastTaskId) {
    taskLog.open = !logIsLong;
  }
  taskLog.classList.toggle('compact', !logIsLong);
  state.taskLogWasLong = logIsLong;
  const progressPanel = document.querySelector('#task-progress');
  const progress = Number(task.progress || 0);
  const taskActive = ['queued', 'running'].includes(task.status);
  const generationActive = taskActive && ['static_generation', 'traction_generation'].includes(task.kind);
  const finishedAge = task.finished_at ? Date.now() - new Date(task.finished_at).getTime() : 0;
  const showTaskProgress = Boolean(task.id)
    && !payload.rf.running
    && (taskActive || finishedAge < 8000);
  progressPanel.classList.toggle('visible', showTaskProgress);
  document.querySelector('#progress-bar').style.width = `${progress}%`;
  setText('#progress-value', t('progress.percentage', { value: progress.toFixed(progress % 1 ? 1 : 0) }));
  let progressKey = `progress.${task.progress_phase || (task.status === 'idle' ? 'idle' : 'preparing')}`;
  if (task.kind === 'ephemeris' && taskActive) progressKey = 'progress.ephemeris';
  if (task.kind === 'ephemeris' && task.status === 'completed') progressKey = 'progress.ephemerisCompleted';
  setText('#progress-title', t(progressKey));
  const cancelTaskButton = document.querySelector('#cancel-task');
  cancelTaskButton.hidden = !generationActive;
  cancelTaskButton.disabled = task.progress_phase === 'cancelling';
  const rfKey = payload.rf.running
    ? (payload.rf.mode === 'jam' ? 'jamming' : 'transmitting')
    : payload.rf.mode === 'transmit' && payload.rf.status === 'completed'
      ? 'transmitCompleted'
      : 'stopped';
  const rfText = t(`status.${rfKey}`);
  setText('#rf-status', rfText); setText('#overview-rf', rfText); setText('#rf-detail', payload.rf.running ? rfText : t('status.none')); setText('#jam-state', rfText);
  document.querySelector('#rf-dot').className = `dot ${payload.rf.running ? 'live' : ''}`;
  document.querySelector('#global-stop').disabled = !payload.rf.running;
  const workBusy = taskActive || payload.rf.running || state.taskRequestPending;
  document.querySelector('#update-ephemeris').disabled = workBusy;
  document.querySelector('#static-form button[type="submit"]').disabled = workBusy;
  document.querySelector('#traction-form button[type="submit"]').disabled = workBusy;
  const rfProgressPanel = document.querySelector('#rf-progress');
  const showRfProgress = !showTaskProgress
    && payload.rf.mode === 'transmit'
    && payload.rf.status === 'running';
  rfProgressPanel.classList.toggle('visible', showRfProgress);
  if (showRfProgress) {
    const rfProgress = Math.max(0, Math.min(100, Number(payload.rf.progress || 0)));
    document.querySelector('#rf-progress-bar').style.width = `${rfProgress}%`;
    setText('#rf-progress-title', t('transmit.progress'));
    const percentage = t('progress.percentage', { value: rfProgress.toFixed(1) });
    setText('#rf-countdown', `${t('transmit.remaining', { time: formatDuration(payload.rf.remaining) })} · ${percentage}`);
    setText('#rf-time-detail', t('transmit.timeDetail', {
      elapsed: formatDuration(payload.rf.elapsed),
      duration: formatDuration(payload.rf.duration)
    }));
  }
  document.querySelectorAll('.transmit-button').forEach((button) => { button.disabled = !payload.generated_file || payload.rf.running || ['queued','running'].includes(task.status); });
  document.querySelector('#start-jam').disabled = payload.rf.running || ['queued','running'].includes(task.status);
  if (payload.generated_file) { setText('#overview-file', payload.generated_file.name); setText('#file-detail', t('messages.fileSize', { size: formatBytes(payload.generated_file.size) })); }
  else { setText('#overview-file', t('status.none')); setText('#file-detail', ''); }
  if (state.lastTaskId === task.id && state.lastTaskStatus !== task.status && task.status === 'completed') {
    toast(t(task.kind === 'ephemeris' ? 'messages.ephemerisCompleted' : 'messages.generationCompleted'));
    if (task.kind === 'ephemeris' && task.result) renderEphemeris(task.result);
    if (['static_generation', 'traction_generation'].includes(task.kind)) loadFiles();
  }
  if (state.lastTaskId === task.id && state.lastTaskStatus !== task.status && task.status === 'failed') {
    const errorKey = `errors.${task.error}`;
    toast(value(errorKey) ? t(errorKey) : task.error || t('messages.requestFailed'), true);
  }
  if (state.lastTaskId === task.id && state.lastTaskStatus !== task.status && task.status === 'cancelled') {
    toast(t('messages.taskCancelled'));
  }
  state.lastTaskId = task.id; state.lastTaskStatus = task.status;
  if (document.querySelector('[data-page-panel="files"]')?.classList.contains('active')) {
    if (generationActive) loadFiles().catch(() => {});
    else renderFiles();
  }
}

async function pollStatus() {
  try { renderStatus(await api('/api/status')); } catch (_) { /* 下一輪自動重試 */ }
}

function renderEphemeris(result) {
  setText('#ephemeris-path', result.path || t('status.none'));
  const range = result.earliest && result.latest ? `${new Date(result.earliest).toLocaleString()} – ${new Date(result.latest).toLocaleString()}` : t('status.none');
  setText('#ephemeris-range', range);
}

async function startTask(path, body) {
  if (state.taskRequestPending) return;
  state.taskRequestPending = true;
  document.querySelector('#update-ephemeris').disabled = true;
  document.querySelector('#static-form button[type="submit"]').disabled = true;
  document.querySelector('#traction-form button[type="submit"]').disabled = true;
  try {
    const result = await api(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined });
    state.lastTaskId = result.task_id; state.lastTaskStatus = 'queued';
    document.querySelector('#rf-progress').classList.remove('visible');
    document.querySelector('#task-progress').classList.add('visible');
    document.querySelector('#progress-bar').style.width = '0%';
    setText('#progress-value', t('progress.percentage', { value: '0' }));
    setText('#progress-title', t('progress.preparing'));
    toast(t('messages.taskStarted'));
    await pollStatus();
  } finally {
    state.taskRequestPending = false;
    if (state.status) renderStatus(state.status);
  }
}

async function loadSettings() {
  state.settings = await api('/api/settings');
  document.querySelector('#setting-frequency').value = state.settings.default_freq;
  document.querySelector('#setting-sample-rate').value = state.settings.sample_rate;
  document.querySelector('#setting-gain').value = state.settings.tx_gain;
  document.querySelector('#setting-lna-gain').value = state.settings.lna_gain;
  document.querySelector('#setting-vga-gain').value = state.settings.vga_gain;
  document.querySelector('#setting-speed').value = state.settings.default_speed_mps;
  document.querySelector('#setting-altitude').value = state.settings.default_height;
  document.querySelector('#setting-static-duration').value = state.settings.static_duration_s;
  document.querySelector('#setting-traction-duration').value = state.settings.traction_duration_s;
  document.querySelector('#setting-update-rate').value = state.settings.update_rate_hz;
  document.querySelector('#setting-drift-heading').value = state.settings.drift_heading_deg;
  document.querySelector('#setting-drift-jitter').value = state.settings.drift_alt_jitter_m;
  document.querySelector('#setting-ephemeris-directory').value = state.settings.ephemeris_save_dir;
  document.querySelector('#setting-ephemeris-max-files').value = state.settings.ephemeris_max_files;
  document.querySelector('#static-alt').value ||= state.settings.default_height;
  document.querySelector('#traction-alt').value ||= state.settings.default_height;
  document.querySelector('#static-duration').value = state.settings.static_duration_s;
  document.querySelector('#traction-duration').value = state.settings.traction_duration_s;
  updateTractionPrediction();
  setText('#jam-gain', `${state.settings.tx_gain} ${t('units.db')}`);
}

async function loadEphemeris() { renderEphemeris(await api('/api/ephemeris')); }

function fileDeletionBlocked(file) {
  const taskActive = ['queued', 'running'].includes(state.status?.task?.status);
  const activeTransmission = state.status?.rf?.running && file.current;
  return taskActive || activeTransmission;
}

function renderFiles() {
  const body = document.querySelector('#file-table');
  if (!body) return;
  body.replaceChildren();
  const existingNames = new Set(state.files.map((file) => file.name));
  state.selectedFiles = new Set([...state.selectedFiles].filter((name) => existingNames.has(name)));
  const selectableFiles = state.files.filter((file) => !fileDeletionBlocked(file));
  const selectedCount = state.selectedFiles.size;
  const selectAll = document.querySelector('#select-all-files');
  const allSelectableSelected = selectableFiles.length > 0
    && selectableFiles.every((file) => state.selectedFiles.has(file.name));
  selectAll.checked = allSelectableSelected;
  selectAll.indeterminate = selectedCount > 0 && !allSelectableSelected;
  selectAll.disabled = selectableFiles.length === 0;
  setText('#file-selection-summary', t('files.selected', {
    selected: selectedCount,
    total: state.files.length
  }));
  document.querySelector('#delete-files').disabled = selectedCount === 0
    || ['queued', 'running'].includes(state.status?.task?.status);

  if (!state.files.length) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 5; cell.className = 'empty-row'; cell.textContent = t('files.empty');
    return;
  }
  state.files.forEach((file) => {
    const row = body.insertRow();
    const selectionCell = row.insertCell(); selectionCell.className = 'checkbox-cell';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.className = 'file-checkbox';
    checkbox.checked = state.selectedFiles.has(file.name);
    checkbox.disabled = fileDeletionBlocked(file);
    checkbox.setAttribute('aria-label', file.name);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selectedFiles.add(file.name);
      else state.selectedFiles.delete(file.name);
      renderFiles();
    });
    selectionCell.append(checkbox);
    row.insertCell().textContent = file.name;
    row.insertCell().textContent = formatBytes(file.size);
    row.insertCell().textContent = new Date(file.modified_at).toLocaleString();
    const statusCell = row.insertCell();
    statusCell.textContent = t(file.current ? 'files.current' : 'files.available');
    statusCell.classList.toggle('file-current', file.current);
  });
}

async function loadFiles(notify = false) {
  const result = await api('/api/files');
  state.files = result.files || [];
  renderFiles();
  if (notify) toast(t('messages.filesRefreshed'));
}

async function deleteSelectedFiles() {
  const names = [...state.selectedFiles];
  if (!names.length || !window.confirm(t('files.deleteConfirm', { count: names.length }))) return;
  const result = await api('/api/files', {
    method: 'DELETE',
    body: JSON.stringify({ names })
  });
  state.selectedFiles.clear();
  toast(t('messages.filesDeleted', { count: result.deleted.length }));
  await Promise.all([loadFiles(), pollStatus()]);
}

function populatePresetSelect(id) {
  const select = document.querySelector(id); const current = select.value;
  select.replaceChildren();
  const manual = document.createElement('option'); manual.value = ''; manual.textContent = t('preset.manual'); select.append(manual);
  Object.keys(state.presets).forEach((name) => { const option = document.createElement('option'); option.value = name; option.textContent = name; select.append(option); });
  if (state.presets[current]) select.value = current;
}

function renderPresets() {
  populatePresetSelect('#static-preset'); populatePresetSelect('#traction-preset');
  const body = document.querySelector('#preset-table'); body.replaceChildren();
  const entries = Object.entries(state.presets);
  if (!entries.length) { const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 5; cell.className = 'empty-row'; cell.textContent = t('preset.empty'); return; }
  entries.forEach(([name, point]) => {
    const row = body.insertRow();
    [name, point.lat.toFixed(6), point.lon.toFixed(6), point.alt.toFixed(1)].forEach((content) => { row.insertCell().textContent = content; });
    const actions = row.insertCell(); actions.className = 'table-actions';
    const edit = document.createElement('button'); edit.className = 'mini'; edit.textContent = t('actions.edit'); edit.addEventListener('click', () => editPreset(name));
    const remove = document.createElement('button'); remove.className = 'mini delete'; remove.textContent = t('actions.delete'); remove.addEventListener('click', () => deletePreset(name));
    actions.append(edit, remove);
  });
}

async function loadPresets() { state.presets = await api('/api/presets'); renderPresets(); }
function editPreset(name) {
  const point = state.presets[name];
  document.querySelector('#preset-original-name').value = name; document.querySelector('#preset-name').value = name;
  document.querySelector('#preset-lat').value = point.lat; document.querySelector('#preset-lon').value = point.lon; document.querySelector('#preset-alt').value = point.alt;
  setText('#preset-form-title', t('preset.editTitle'));
  placeMarker('preset', point.lat, point.lon);
  state.mapLocks.preset = true;
  setMapTip('#preset-map-tip', 'mapState.positionLocked', true);
  updateMapLockButtons();
  state.maps.preset.setView([point.lat, point.lon], 14);
}
function resetPresetForm() { document.querySelector('#preset-form').reset(); document.querySelector('#preset-original-name').value = ''; resetPresetPosition(); setText('#preset-form-title', t('preset.addTitle')); }
async function deletePreset(name) {
  try { await api(`/api/presets/${encodeURIComponent(name)}`, { method: 'DELETE' }); toast(t('messages.presetDeleted')); await loadPresets(); resetPresetForm(); } catch (error) { toast(error.message, true); }
}

function choosePreset(mode, name) {
  if (!name) return;
  const point = state.presets[name];
  if (mode === 'traction') resetTractionDirection();
  document.querySelector(`#${mode}-lat`).value = point.lat; document.querySelector(`#${mode}-lon`).value = point.lon; document.querySelector(`#${mode}-alt`).value = point.alt;
  placeMarker(mode, point.lat, point.lon);
  if (mode === 'static') {
    state.mapLocks.static = true;
    setMapTip('#static-map-tip', 'mapState.positionLocked', true);
  }
  if (mode === 'traction') {
    state.mapLocks.tractionStart = true;
    setText('#traction-map-tip', t('traction.clickDirection'));
  }
  updateMapLockButtons();
}

function bindEvents() {
  document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => showPage(button.dataset.page)));
  document.querySelector('#refresh-hardware').addEventListener('click', () => loadHardware(true));
  document.querySelector('#refresh-files').addEventListener('click', async () => {
    try { await loadFiles(true); } catch (error) { toast(error.message, true); }
  });
  document.querySelector('#select-all-files').addEventListener('change', (event) => {
    if (event.target.checked) {
      state.files.filter((file) => !fileDeletionBlocked(file))
        .forEach((file) => state.selectedFiles.add(file.name));
    } else {
      state.selectedFiles.clear();
    }
    renderFiles();
  });
  document.querySelector('#delete-files').addEventListener('click', async () => {
    try { await deleteSelectedFiles(); } catch (error) { toast(error.message, true); }
  });
  document.querySelector('#update-ephemeris').addEventListener('click', async () => { try { await startTask('/api/ephemeris/update'); } catch (error) { toast(error.message, true); } });
  document.querySelector('#cancel-task').addEventListener('click', async () => {
    const button = document.querySelector('#cancel-task');
    button.disabled = true;
    try {
      await api('/api/tasks/cancel', { method: 'POST' });
      await pollStatus();
    } catch (error) {
      button.disabled = false;
      toast(error.message, true);
    }
  });
  document.querySelector('#static-preset').addEventListener('change', (event) => choosePreset('static', event.target.value));
  document.querySelector('#traction-preset').addEventListener('change', (event) => choosePreset('traction', event.target.value));
  document.querySelector('#static-reset-position').addEventListener('click', resetStaticPosition);
  document.querySelector('#static-focus-position').addEventListener('click', () => focusMapMarker('static'));
  document.querySelector('#traction-reset-direction').addEventListener('click', resetTractionDirection);
  document.querySelector('#traction-reset-all').addEventListener('click', resetTractionStart);
  document.querySelector('#traction-focus-start').addEventListener('click', () => focusMapMarker('traction'));
  document.querySelector('#traction-heading').addEventListener('input', syncTractionDirectionFromHeading);
  document.querySelector('#preset-reset-position').addEventListener('click', resetPresetPosition);
  document.querySelector('#preset-focus-position').addEventListener('click', () => focusMapMarker('preset'));
  document.querySelector('#static-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try { await startTask('/api/generate/static', { lat: number('#static-lat'), lon: number('#static-lon'), alt: number('#static-alt'), duration: number('#static-duration'), time_mode: document.querySelector('#static-time').value }); } catch (error) { toast(error.message, true); }
  });
  document.querySelector('#traction-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.tractionDirection) { toast(t('messages.selectStartAndDirection'), true); return; }
    if (!validateTractionDuration(true)) { document.querySelector('#traction-duration').focus(); return; }
    try { const body = tractionValues(); body.direction_lat = state.tractionDirection.lat; body.direction_lon = state.tractionDirection.lon; body.time_mode = document.querySelector('#traction-time').value; await startTask('/api/generate/traction', body); } catch (error) { toast(error.message, true); }
  });
  ['#traction-speed','#traction-ramp','#traction-duration','#traction-hold','#traction-final-hold'].forEach((id) => document.querySelector(id).addEventListener('input', updateTractionPrediction));
  document.querySelectorAll('.transmit-button').forEach((button) => button.addEventListener('click', async () => { try { await api('/api/rf/transmit', { method: 'POST' }); toast(t('messages.transmitStarted')); await pollStatus(); } catch (error) { toast(error.message, true); } }));
  document.querySelector('#start-jam').addEventListener('click', async () => { try { await api('/api/rf/jam', { method: 'POST' }); toast(t('messages.jamStarted')); await pollStatus(); } catch (error) { toast(error.message, true); } });
  document.querySelectorAll('#global-stop,.stop-button').forEach((button) => button.addEventListener('click', async () => { try { await api('/api/rf/stop', { method: 'POST' }); toast(t('messages.rfStopped')); await pollStatus(); } catch (error) { toast(error.message, true); } }));
  document.querySelector('#preset-cancel').addEventListener('click', resetPresetForm);
  document.querySelector('#preset-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const original = document.querySelector('#preset-original-name').value;
    const body = { name: document.querySelector('#preset-name').value.trim(), new_name: document.querySelector('#preset-name').value.trim(), lat: number('#preset-lat'), lon: number('#preset-lon'), alt: number('#preset-alt') };
    try { if (original) { delete body.name; await api(`/api/presets/${encodeURIComponent(original)}`, { method: 'PUT', body: JSON.stringify(body) }); toast(t('messages.presetUpdated')); } else { delete body.new_name; await api('/api/presets', { method: 'POST', body: JSON.stringify(body) }); toast(t('messages.presetCreated')); } await loadPresets(); resetPresetForm(); } catch (error) { toast(error.message, true); }
  });
  document.querySelector('#settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const payload = {
        default_freq: number('#setting-frequency'),
        sample_rate: number('#setting-sample-rate'),
        tx_gain: number('#setting-gain'),
        lna_gain: number('#setting-lna-gain'),
        vga_gain: number('#setting-vga-gain'),
        default_speed_mps: number('#setting-speed'),
        default_height: number('#setting-altitude'),
        static_duration_s: number('#setting-static-duration'),
        traction_duration_s: number('#setting-traction-duration'),
        update_rate_hz: number('#setting-update-rate'),
        drift_heading_deg: number('#setting-drift-heading'),
        drift_alt_jitter_m: number('#setting-drift-jitter'),
        ephemeris_save_dir: document.querySelector('#setting-ephemeris-directory').value.trim(),
        ephemeris_max_files: number('#setting-ephemeris-max-files')
      };
      state.settings = await api('/api/settings', {
        method: 'PUT',
        body: JSON.stringify(payload)
      });
      document.querySelector('#static-duration').value = state.settings.static_duration_s;
      document.querySelector('#traction-duration').value = state.settings.traction_duration_s;
      updateTractionPrediction();
      setText('#jam-gain', `${state.settings.tx_gain} ${t('units.db')}`);
      toast(t('messages.settingsSaved'));
    } catch (error) {
      toast(error.message, true);
    }
  });
}

async function init() {
  try {
    state.messages = await fetch(
      '/assets/locales/zh-TW.json',
      { cache: 'no-store' }
    ).then((response) => response.json());
    applyTranslations(); configureSettingLimits(); showPage('overview'); bindEvents(); setupMaps(); validateTractionDuration();
    await Promise.all([loadSoftwareInfo(), loadHardware(), loadSettings(), loadPresets(), loadEphemeris(), loadFiles(), pollStatus()]);
    setInterval(pollStatus, 1000);
  } catch (error) { toast(error.message || t('messages.requestFailed'), true); }
}

document.addEventListener('DOMContentLoaded', init);
