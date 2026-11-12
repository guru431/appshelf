(function () {
  'use strict';
  var key = 'appshelf-device-profile', profile = null, invite = 'appshelf-device-invite-closed';
  var allowed = ['', 'arm64-v8a', 'arm64-v8a,armeabi-v7a,armeabi', 'armeabi-v7a,armeabi', 'x86_64', 'x86_64,x86', 'x86'];
  function valid(p) {
    return p && (p.sdk === 0 || Number.isInteger(p.sdk) && p.sdk >= 23 && p.sdk <= 36) && allowed.includes(p.abi);
  }
  try { var saved = JSON.parse(localStorage.getItem(key)); if (valid(saved)) profile = saved; } catch (_) {}
  function assess(v, p) {
    var reasons = [], unknown = [], sdk = Number(v.min), target = Number(v.target), rotation = Number(v.rotation);
    if (!p.sdk) unknown.push('Версия Android не указана');
    else {
      if (!sdk) unknown.push('Минимальная Android неизвестна');
      else if (sdk > p.sdk) reasons.push('Нужна Android с API ' + sdk + '; у устройства API ' + p.sdk);
      // Android installation policy, not the separate Google Play publication requirement:
      // https://developer.android.com/about/versions/15/behavior-changes-all#minimum-target-api-level
      if (!target) unknown.push('targetSdk неизвестен');
      else if (p.sdk >= 35 && target < 24 || p.sdk === 34 && target < 23)
        reasons.push('Слишком старый targetSdk ' + target + ' для обычной установки на выбранную Android');
      if (rotation && p.sdk < rotation) reasons.push('Обновление с сохранением данных при ротации ключа подтверждено только с API ' + rotation);
    }
    if (!p.abi) unknown.push('Архитектура устройства не указана');
    else if (v.abis && !v.abis.split(',').some(function (abi) { return p.abi.split(',').includes(abi); }))
      reasons.push('APK: ' + v.abis + '; устройство: ' + p.abi);
    return {label: reasons.length ? 'Есть ограничения' : unknown.length ? 'Неизвестно' : 'Совместима по известным требованиям',
            reasons: reasons.concat(unknown), limited: reasons.length > 0};
  }
  function update() {
    document.querySelectorAll('[data-device-form]').forEach(function (form) {
      form.elements.sdk.value = profile && profile.sdk || '';
      form.elements.abi.value = profile && profile.abi || '';
    });
    // строка «Устройство: …» Android-страниц; без профиля — приглашение, которое вошедший может скрыть
    document.querySelectorAll('[data-device-line]').forEach(function (line) {
      var form = line.querySelector('[data-device-form]'), text = line.querySelector('[data-device-summary]');
      var dismiss = line.querySelector('[data-device-dismiss]'), closed = false;
      function label(select, none) { return select.value ? select.selectedOptions[0].textContent : none; }
      text.textContent = profile ? 'Устройство: Android ' + label(form.elements.sdk, 'не указан') + ' · ' +
        label(form.elements.abi, 'архитектура не указана') : 'Для какого устройства?';
      text.nextElementSibling.textContent = profile ? 'изменить' : 'указать';
      try { closed = !!localStorage.getItem(invite); } catch (_) {}
      if (dismiss) dismiss.hidden = !!profile;
      line.hidden = !profile && closed && !!dismiss;
    });
    document.querySelectorAll('[data-compatibility]').forEach(function (box) {
      var card = box.closest('.app'), select = card.querySelector('[data-apk-select]');
      var data = select ? select.selectedOptions[0].dataset : box.dataset;
      box.hidden = !profile;
      if (!profile) return;
      var result = assess(data, profile);
      box.querySelector('summary').textContent = result.label;
      box.querySelector('[data-compatibility-reasons]').textContent = result.reasons.join('. ') ||
        'Минимальная Android и архитектура подходят. Дополнительные требования и установленную подпись проверит Android.';
      box.classList.toggle('err', result.limited);
    });
    document.querySelectorAll('[data-fdroid-builds]').forEach(function (select) {
      var hint = select.parentElement.querySelector('[data-build-hint]');
      if (!hint) return;
      if (!profile) { hint.textContent = ''; return; }
      var fits = Array.from(select.options).filter(function (o) {
        return o.dataset.abis !== undefined && !assess(o.dataset, profile).limited;
      });
      var choice = fits[0];
      if (!select.dataset.userSelected && choice) select.value = choice.value;
      hint.textContent = choice ? 'Для профиля подходит: ' + choice.textContent.trim() + '. Выбор можно изменить.' :
        'Подходящая сборка по известным требованиям не найдена. Проверьте архитектуру и Android.';
      if (select.dataset.userSelected) hint.textContent += ' Сохранён ваш выбор.';
      var selected = select.selectedOptions[0];
      if (selected && selected.dataset.abis !== undefined) {
        var result = assess(selected.dataset, profile);
        if (result.limited || result.label === 'Неизвестно') hint.textContent += ' Выбранная сборка: ' + result.label + '. ' + result.reasons.join('. ');
        hint.textContent += ' Конкретная сборка не обновляется автоматически; для автообновлений выберите «Рекомендуемая».';
      }
    });
  }
  document.querySelectorAll('[data-device-form]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var next = {sdk: Number(form.elements.sdk.value), abi: form.elements.abi.value};
      if (!valid(next)) return;
      try { localStorage.setItem(key, JSON.stringify(next)); localStorage.removeItem(invite); profile = next; update();
        form.querySelector('[data-device-saved]').textContent = 'Профиль сохранён в этом браузере';
      } catch (_) { form.querySelector('[data-device-saved]').textContent = 'Браузер не разрешил сохранить профиль'; }
    });
    form.querySelector('[data-device-reset]').addEventListener('click', function () {
      try { localStorage.removeItem(key); profile = null; update();
        form.querySelector('[data-device-saved]').textContent = 'Профиль сброшен';
      } catch (_) { form.querySelector('[data-device-saved]').textContent = 'Браузер не разрешил сбросить профиль'; }
    });
  });
  document.querySelectorAll('[data-device-dismiss]').forEach(function (button) {
    button.addEventListener('click', function () {
      button.closest('[data-device-line]').hidden = true;
      try { localStorage.setItem(invite, '1'); } catch (_) {}
    });
  });
  addEventListener('change', function (event) {
    if (event.target.matches('[data-fdroid-builds]')) event.target.dataset.userSelected = '1';
    if (event.target.matches('[data-apk-select], [data-fdroid-builds]')) update();
  });
  addEventListener('storage', function (event) {
    if (event.key !== key) return;
    try { var p = JSON.parse(event.newValue); profile = valid(p) ? p : null; } catch (_) { profile = null; }
    update();
  });
  var results = document.getElementById('search-results');
  // Поиск вставляет новые строки внутрь списка; подсказки update() меняют только текст и сюда не попадают.
  if (results) new MutationObserver(function (changes) {
    if (changes.some(function (change) {
      return Array.prototype.some.call(change.addedNodes, function (node) {
        return node.nodeType === 1 && (node.matches('[data-fdroid-builds]') || node.querySelector('[data-fdroid-builds]'));
      });
    })) update();
  }).observe(results, {childList: true, subtree: true});
  var effective = document.querySelector('[data-effective-theme]');
  if (effective) {
    var media = matchMedia('(prefers-color-scheme: dark)');
    function theme() { effective.textContent = 'Сейчас в системе: ' + (media.matches ? 'тёмная' : 'светлая'); }
    media.addEventListener('change', theme); theme();
  }
  update();
})();
