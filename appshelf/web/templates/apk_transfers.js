/* Resumable APK uploads. Shared page/service-worker code; deliberately no fetch interception or page cache. */
(function (root) {
  'use strict';
  var worker = typeof document === 'undefined';
  if (!root.indexedDB || !root.crypto || !root.crypto.subtle) return;
  var currentUser = worker ? 0 : Number(document.currentScript.dataset.uploadUser || 0);
  if (!worker && !currentUser) return;
  if (!worker) root.ApkUploads = true;
  var opened, registration, tray;
  var running = new Map(), controllers = new Map(), localFiles = new Map(), views = new Map(), intents = new Map();
  function random() { return Array.from(crypto.getRandomValues(new Uint8Array(16)), function (n) { return n.toString(16).padStart(2, '0'); }).join(''); }
  function open() {
    if (!opened) opened = new Promise(function (resolve, reject) {
      var request = indexedDB.open('appshelf-apk-uploads', 1);
      request.onupgradeneeded = function () { request.result.createObjectStore('uploads', {keyPath: 'key'}); };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
    return opened;
  }
  async function read(key) {
    var db = await open();
    return new Promise(function (resolve, reject) {
      var store = db.transaction('uploads').objectStore('uploads');
      var request = key === undefined ? store.getAll() : store.get(key);
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }
  async function edit(key, change) {
    var db = await open();
    return new Promise(function (resolve, reject) {
      var tx = db.transaction('uploads', 'readwrite'), store = tx.objectStore('uploads'), value, failure;
      var request = store.get(key);
      request.onsuccess = function () {
        try {
          value = change(request.result);
          if (value === null) store.delete(key);
          else if (value !== undefined) store.put(value);
        } catch (error) { failure = error; tx.abort(); }
      };
      tx.oncomplete = function () { resolve(value); };
      tx.onabort = tx.onerror = function () { reject(failure || tx.error || new Error('Не удалось сохранить состояние загрузки')); };
    });
  }
  function size(bytes) {
    if (bytes < 1048576) return Math.ceil(bytes / 1024) + ' КБ';
    return (bytes / 1048576).toLocaleString('ru-RU', {maximumFractionDigits: 1}) + ' МБ';
  }
  async function digest(raw) {
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', raw)), function (n) { return n.toString(16).padStart(2, '0'); }).join('');
  }
  async function fingerprint(file) {
    var first = new Uint8Array(await file.slice(0, 65536).arrayBuffer());
    var last = new Uint8Array(await file.slice(Math.max(0, file.size - 65536)).arrayBuffer());
    var length = new TextEncoder().encode(String(file.size)), sample = new Uint8Array(first.length + last.length + length.length);
    sample.set(first); sample.set(last, first.length); sample.set(length, first.length + last.length);
    return digest(sample);
  }
  function url(record, suffix) {
    var target = new URL(record.endpoint, root.location.origin);
    if (target.origin !== root.location.origin || !/^\/android\/(?:public\/)?uploads$/.test(target.pathname)
        || target.searchParams.get('person') !== String(record.owner)) throw new Error('Неверная полка загрузки');
    target.pathname += suffix || ''; return target.href;
  }
  async function notify(key) {
    try {
      if (worker) {
        var clients = await root.clients.matchAll({type: 'window', includeUncontrolled: true});
        clients.forEach(function (client) { client.postMessage({type: 'apk-upload-update', key: key}); });
      } else await refresh(key);
    } catch (error) { /* A closed page must not interrupt the transfer. */ }
  }
  async function update(key, values) {
    var result = await edit(key, function (record) {
      if (!record) return record;
      var next = Object.assign({}, values);
      if (record.state === 'paused' && !next.state) delete next.message;
      Object.assign(record, next, {updated: Date.now()}); return record;
    });
    await notify(key); return result;
  }
  async function request(record, suffix, options) {
    var abort = new AbortController(), key = record.key;
    if (!controllers.has(key)) controllers.set(key, new Set());
    var group = controllers.get(key); group.add(abort);
    var timer = setTimeout(function () { abort.abort(); }, 90000);
    try {
      var response = await fetch(url(record, suffix), Object.assign({cache: 'no-store', credentials: 'same-origin', signal: abort.signal}, options));
      var json = (response.headers.get('content-type') || '').includes('application/json');
      var value = json ? await response.json() : {};
      if (!response.ok || !json) {
        var detail = typeof value.detail === 'string' ? value.detail : '';
        var error = new Error(value.error || detail || (response.status === 401 || response.ok ? 'Войдите снова, чтобы продолжить загрузку' : 'Сервер не принял часть файла (HTTP ' + response.status + ')'));
        error.status = response.ok ? 401 : response.status; throw error;
      }
      return value;
    } finally { clearTimeout(timer); group.delete(abort); }
  }
  function abort(key) { if (controllers.has(key)) controllers.get(key).forEach(function (control) { control.abort(); }); }
  async function execute(key, supplied) {
    var token = random(), accepted = false;
    var record = await edit(key, function (r) {
      if (!r || ['paused', 'done', 'error'].includes(r.state) || r.leaseUntil > Date.now()) return r;
      accepted = true; r.lease = token; r.leaseUntil = Date.now() + 20000; return r;
    });
    if (!accepted) return;
    var heartbeat = setInterval(function () {
      edit(key, function (r) { if (r && r.lease === token) r.leaseUntil = Date.now() + 20000; return r; }).catch(function () {});
    }, 5000);
    try {
      var file = supplied || record.blob;
      var state = await request(record, record.session ? '/' + record.session : '', record.session ? {} : {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({client_id: record.clientId,
          size: record.size, filename: record.filename, name: record.name, fingerprint: record.fingerprint, package: record.package})
      });
      var current = await read(key);
      if (!current || current.state === 'paused') return;
      record = await update(key, {session: state.id, message: 'Проверяем сохранённые части…'});
      if (state.fingerprint !== record.fingerprint || state.size !== record.size) throw new Error('Выбран другой файл');
      if (!state.job_id) {
        if (!file) { await update(key, {state: 'paused', message: 'Выберите тот же файл, чтобы продолжить с сохранённого места'}); return; }
        var count = Math.ceil(file.size / state.chunk_size), done = Object.assign({}, state.parts), queue = [];
        function length(index) { return Math.min(state.chunk_size, file.size - index * state.chunk_size); }
        function confirmed() { return Object.keys(done).reduce(function (total, index) { return total + length(Number(index)); }, 0); }
        for (var index = 0; index < count; index++) {
          if (done[index]) {
            var saved = await file.slice(index * state.chunk_size, (index + 1) * state.chunk_size).arrayBuffer();
            if (await digest(saved) !== done[index]) {
              var mismatch = new Error('Файл отличается. Для докачки выберите исходный APK; для другого файла начните новую передачу');
              mismatch.status = 409; throw mismatch;
            }
          } else queue.push(index);
        }
        var initial = confirmed(), started = performance.now();
        await update(key, {loaded: initial, message: ''});
        async function send() {
          while (queue.length) {
            var number = queue.shift();
            var current = await read(key);
            if (!current || current.state === 'paused') return;
            var raw = await file.slice(number * state.chunk_size, (number + 1) * state.chunk_size).arrayBuffer();
            var hash = await digest(raw);
            for (var attempt = 0; ; attempt++) {
              current = await read(key);
              if (!current || current.state === 'paused') return;
              try {
                await request(record, '/' + state.id + '/parts/' + number, {method: 'POST',
                  headers: {'Content-Type': 'application/octet-stream', 'X-Chunk-SHA256': hash}, body: raw});
                done[number] = hash;
                var loaded = confirmed(), elapsed = (performance.now() - started) / 1000;
                await update(key, {loaded: loaded, speed: elapsed > 0 ? (loaded - initial) / elapsed : 0, message: ''});
                break;
              } catch (error) {
                current = await read(key);
                if (!current || current.state === 'paused') return;
                if (attempt >= 4 || error.status && error.status < 500 && error.status !== 408) throw error;
                await update(key, {message: 'Связь прервалась. Повторяем только недоставленную часть…'});
                await new Promise(function (resolve) { setTimeout(resolve, Math.min(15000, 1000 * Math.pow(2, attempt))); });
              }
            }
          }
        }
        var results = await Promise.allSettled([send(), send()]);
        var failed = results.find(function (result) { return result.status === 'rejected'; });
        if (failed) throw failed.reason;
        current = await read(key);
        if (!current || current.state === 'paused') return;
        if (confirmed() !== file.size) return;
        await update(key, {message: 'Файл передан. Собираем части на сервере…'});
        state = await request(record, '/' + state.id + '/finish', {method: 'POST'});
      }
      await update(key, {state: 'done', loaded: record.size, blob: null, jobId: state.job_id, message: 'Файл передан на обработку'});
      localFiles.delete(key);
    } catch (error) {
      var latest = await read(key);
      var message = error.name === 'TypeError' || error.name === 'AbortError' ?
        'Связь прервалась. Полученные части сохранены; продолжим после восстановления сети' : error.message;
      if (latest && !['paused', 'done'].includes(latest.state)) await update(key, {state: error.status && error.status < 500 ? 'error' : 'waiting',
        message: message || 'Связь прервалась. Полученные части остаются на сервере'});
    } finally {
      clearInterval(heartbeat); controllers.delete(key);
      await edit(key, function (r) { if (r && r.lease === token) { r.leaseUntil = 0; r.lease = ''; } return r; });
      await notify(key);
    }
  }
  function run(key, file) {
    if (running.has(key)) return running.get(key).then(async function () {
      var record = await read(key);
      if (record && record.state === 'uploading') return run(key, file);
    });
    var task = execute(key, file).finally(function () { running.delete(key); });
    running.set(key, task); return task;
  }
  async function pause(key) {
    await edit(key, function (r) {
      if (r && r.state !== 'done') { r.state = 'paused'; r.message = 'На паузе. Полученные части сохранены'; r.updated = Date.now(); }
      return r;
    });
    abort(key); await notify(key);
  }
  async function cancel(key) {
    await pause(key);
    var record = await read(key);
    if (!record || record.state === 'done') return;
    if (record.session) {
      try { await request(record, '/' + record.session + '/cancel', {method: 'POST'}); }
      catch (error) {
        if (error.status === 409) {
          var state = await request(record, '/' + record.session);
          if (state.job_id) { await update(key, {state: 'done', blob: null, jobId: state.job_id}); localFiles.delete(key); return; }
        }
        if (error.status !== 404 && error.status !== 410) throw error;
      }
    }
    await edit(key, function () { return null; }); localFiles.delete(key); await notify(key);
  }
  if (worker) {
    root.addEventListener('install', function (event) { event.waitUntil(root.skipWaiting()); });
    root.addEventListener('activate', function (event) { event.waitUntil(root.clients.claim()); });
    root.addEventListener('message', function (event) {
      var data = event.data || {};
      if (!event.source || new URL(event.source.url).origin !== root.location.origin) return;
      if (!['apk-upload', 'apk-upload-pause', 'apk-upload-resume', 'apk-upload-cancel'].includes(data.type)) return;
      var previous = intents.get(data.key) || Promise.resolve();
      var transition = previous.catch(function () {}).then(async function () {
        if (data.type === 'apk-upload-pause') await pause(data.key);
        if (data.type === 'apk-upload-resume') await update(data.key, {state: 'uploading', message: 'Продолжаем с сохранённого места…'});
        if (data.type === 'apk-upload-cancel') await cancel(data.key);
      });
      intents.set(data.key, transition);
      event.waitUntil(transition.then(function () {
        if (data.type === 'apk-upload' || data.type === 'apk-upload-resume') return run(data.key);
      }));
    });
    root.addEventListener('sync', function (event) {
      if (event.tag === 'apk-upload') event.waitUntil(read().then(function (records) {
        return Promise.all(records.filter(function (r) { return r.blob && ['uploading', 'waiting'].includes(r.state); }).map(async function (r) {
          await run(r.key);
          var latest = await read(r.key);
          if (latest && latest.state === 'waiting') throw new Error('Retry APK transfer when connectivity returns');
        }));
      }));
    });
    return;
  }
  async function service() {
    if (!navigator.serviceWorker) return null;
    if (!registration) registration = navigator.serviceWorker.register('/pwa/upload-worker.js', {scope: '/'}).then(function () {
      return navigator.serviceWorker.ready;
    }).catch(function () { return null; });
    return registration;
  }
  async function kick(key) {
    var record = await read(key), reg;
    if (!record || ['done', 'paused', 'error'].includes(record.state)) return;
    if (record.blob) reg = await service();
    if (reg && reg.active) {
      reg.active.postMessage({type: 'apk-upload', key: key});
      if (reg.sync) reg.sync.register('apk-upload').catch(function () {});
    } else run(key, localFiles.get(key)).catch(function () {});
  }
  async function resume(key) {
    await update(key, {state: 'uploading', message: 'Продолжаем с сохранённого места…'}); await kick(key);
  }
  function controls(box, key, record) {
    box.querySelector('[data-upload-pause]').onclick = function () {
      var handle = navigator.serviceWorker && navigator.serviceWorker.controller;
      var action = record.state === 'uploading' ? 'pause' : 'resume';
      if (record.blob && handle) { handle.postMessage({type: 'apk-upload-' + action, key: key}); return; }
      (action === 'pause' ? pause(key) : resume(key)).catch(function (error) {
        box.querySelector('[data-upload-status]').textContent = error.message;
      });
    };
    box.querySelector('[data-upload-cancel]').onclick = async function () {
      try {
        var handle = navigator.serviceWorker && navigator.serviceWorker.controller;
        if (record.blob && handle) { handle.postMessage({type: 'apk-upload-cancel', key: key}); return; }
        await cancel(key);
      } catch (error) { box.querySelector('[data-upload-status]').textContent = error.status ? error.message : 'Нет связи с сервером. Передача на паузе; отмену можно повторить после восстановления сети.'; }
    };
  }
  function display(box, record) {
    box.hidden = false;
    var bar = box.querySelector('[data-upload-progress]'), status = box.querySelector('[data-upload-status]');
    var percent = Math.min(100, Math.floor((record.loaded || 0) / record.size * 100));
    bar.value = percent;
    var text = percent + ' % · ' + size(record.loaded || 0) + ' из ' + size(record.size);
    if (record.speed > 0 && record.state === 'uploading') text += ' · ' + size(record.speed) + '/с';
    status.textContent = (record.message || 'Отправляем APK') + ' · ' + text;
    status.className = record.state === 'error' ? 'meta err' : 'meta';
    box.querySelector('[data-upload-controls]').hidden = record.state === 'done';
    var hint = box.querySelector('[data-upload-background]'); hint.hidden = false;
    hint.textContent = record.blob ? 'Отправляем частями. Если Android усыпит браузер, продолжим при возвращении. Прогресс хранится 7 дней.' :
      'Файл не сохранён в браузере. После закрытия страницы выберите тот же APK — продолжим с сохранённой части.';
    box.querySelector('[data-upload-pause]').textContent = record.state === 'uploading' ? 'Пауза' : 'Продолжить';
    box.querySelector('[data-upload-pause]').disabled = record.state === 'paused' && record.leaseUntil > Date.now();
  }
  async function refresh(key) {
    var record = await read(key), view = views.get(key);
    if (view) {
      var form = view.form, box = form.querySelector('[data-upload-feedback]');
      if (!record) {
        box.hidden = true; view.inputs.forEach(function (input) { input.element.disabled = input.disabled; });
        view.button.disabled = false; view.button.textContent = view.label; form.removeAttribute('aria-busy'); views.delete(key); return;
      }
      display(box, record); controls(box, key, record);
      var busy = record.state === 'uploading' || record.state === 'paused' && record.leaseUntil > Date.now();
      view.inputs.forEach(function (input) { input.element.disabled = busy || input.disabled; });
      view.button.disabled = busy; view.button.textContent = busy ? 'Загрузка…' : view.label;
      if (busy) form.setAttribute('aria-busy', 'true'); else form.removeAttribute('aria-busy');
      if (record.state === 'done') {
        if (!view.navigating) { view.navigating = true; location.assign(form.dataset.uploadTarget); }
        return;
      }
    }
    await renderTray();
  }
  async function renderTray() {
    var records = (await read()).filter(function (r) { return r.owner === currentUser && r.state !== 'done' && !views.has(r.key); });
    if (!tray && records.length) {
      tray = document.createElement('section'); tray.className = 'card'; tray.setAttribute('aria-label', 'Загрузки APK');
      document.querySelector('main').prepend(tray);
    }
    if (!tray) return;
    tray.replaceChildren(); tray.hidden = !records.length;
    records.forEach(function (record) {
      var title = document.createElement('b'); title.textContent = record.filename; tray.append(title);
      var box = document.createElement('div'); box.className = 'upload-feedback';
      box.innerHTML = '<progress data-upload-progress max="100" aria-label="Отправка APK на сервер"></progress>' +
        '<p data-upload-status role="status" aria-live="polite" aria-atomic="true"></p>' +
        '<div class="upload-controls" data-upload-controls><button type="button" class="secondary sm" data-upload-pause></button>' +
        '<button type="button" class="secondary sm" data-upload-cancel>Отменить</button></div>' +
        '<p class="hint" data-upload-background>Полученные части хранятся 7 дней. После возвращения продолжим отправку.</p>';
      display(box, record); controls(box, record.key, record); tray.append(box);
      if (!record.blob && !localFiles.has(record.key)) {
        var link = document.createElement('a'); link.href = record.target + '/add?person=' + currentUser;
        link.textContent = 'Выбрать тот же файл для продолжения'; tray.append(link);
      }
    });
  }
  document.querySelectorAll('form[data-apk-upload]').forEach(function (form) {
    form.addEventListener('submit', async function (event) {
      event.preventDefault(); if (form.getAttribute('aria-busy') === 'true') return;
      var file = form.elements.file.files[0], box = form.querySelector('[data-upload-feedback]');
      if (!file) { form.reportValidity(); return; }
      var button = form.querySelector('button'), label = button.textContent;
      var inputs = Array.from(form.querySelectorAll('input')).map(function (input) { return {element: input, disabled: input.disabled}; });
      form.setAttribute('aria-busy', 'true'); button.disabled = true;
      inputs.forEach(function (input) { input.element.disabled = true; });
      views.forEach(function (view, oldKey) { if (view.form === form) views.delete(oldKey); });
      box.hidden = false; box.querySelector('[data-upload-status]').textContent = 'Готовим файл для докачки…';
      box.querySelector('[data-upload-progress]').removeAttribute('value');
      box.querySelector('[data-upload-controls]').hidden = true;
      box.scrollIntoView({block: 'nearest'});
      try {
        if (!file.size || file.size > 1073741824) throw new Error('Выберите непустой APK до 1 ГБ');
        var api = new URL(form.dataset.uploadApi, location.origin); api.searchParams.set('person', currentUser);
        var hash = await fingerprint(file), packageName = form.dataset.uploadPackage || '';
        var key = currentUser + ':' + api.pathname + ':' + packageName + ':' + hash;
        var record = await read(key);
        if (!record || record.state === 'done') record = {key: key, clientId: random(), owner: currentUser,
          endpoint: api.pathname + api.search, package: packageName, target: form.dataset.uploadTarget,
          size: file.size, fingerprint: hash, filename: file.name, name: form.elements.name ? form.elements.name.value : '', loaded: 0};
        record.blob = file; record.state = 'uploading'; record.message = ''; record.updated = Date.now();
        try { await edit(key, function () { return record; }); }
        catch (error) { record.blob = null; await edit(key, function () { return record; }); localFiles.set(key, file); }
        views.set(key, {form: form, button: button, label: label, inputs: inputs});
        await refresh(key); await kick(key);
      } catch (error) {
        form.removeAttribute('aria-busy'); button.disabled = false;
        inputs.forEach(function (input) { input.element.disabled = input.disabled; });
        box.querySelector('[data-upload-status]').textContent = error.message || 'Не удалось подготовить докачку. Попробуйте другой браузер';
        await renderTray();
      }
    });
  });
  if (navigator.serviceWorker) navigator.serviceWorker.addEventListener('message', function (event) {
    if (event.data && event.data.type === 'apk-upload-update') refresh(event.data.key).catch(function () {});
  });
  async function restore() {
    var records = await read();
    for (var record of records) {
      if (Date.now() - record.updated > 7 * 86400000) { await edit(record.key, function () { return null; }); continue; }
      if (record.owner === currentUser && ['uploading', 'waiting'].includes(record.state)) await kick(record.key);
    }
    await renderTray();
  }
  addEventListener('online', function () { restore().catch(function () {}); });
  addEventListener('pageshow', function () { restore().catch(function () {}); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) restore().catch(function () {}); });
  setInterval(function () { if (!document.hidden) restore().catch(function () {}); }, 10000);
  restore().catch(function () {});
})(globalThis);
