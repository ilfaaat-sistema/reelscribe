/*
 * Общая панель переключения между приложениями владельца.
 * Подключается одной строкой: <script src="https://reelscribe-ai.vercel.app/apps-bar.js" defer></script>
 * Самодостаточный скрипт без зависимостей. Повторное подключение (или повторный запуск
 * того же тега) ничего не делает — см. проверку #apps-bar в начале.
 */
(function () {
  'use strict';

  if (document.getElementById('apps-bar')) return;

  // currentScript доступен только синхронно на этом шаге — сохраняем сразу,
  // до любых асинхронных веток (ожидание DOMContentLoaded и т.п.).
  var scriptEl = document.currentScript;
  var forcedApp = (scriptEl && scriptEl.dataset && scriptEl.dataset.app) || null;

  // Единый список приложений — единственное место правки ссылок.
  var APPS = [
    { id: 'reels-parser', title: 'Рилс Парсер', href: 'https://reelscribe-ai.vercel.app/' },
    { id: 'reels-radar', title: 'Рилс Радар', href: 'https://reelscribe-ai.vercel.app/radar' },
    // YouTube — ngrok с ноутбука владельца, работает только пока он включён.
    // Появится постоянный домен — поменять адреса здесь.
    { id: 'yt-radar', title: 'YouTube Радар', href: 'https://limelight-robin-unwilling.ngrok-free.dev/' },
    { id: 'yt-parser', title: 'YouTube Парсер', href: 'https://limelight-robin-unwilling.ngrok-free.dev/parser' },
    { id: 'chertogi', title: 'Чертоги', href: 'https://chertogi.vercel.app/' },
    { id: 'tg-bot', title: 'Telegram-бот', href: 'https://t.me/moy_parser_razvitie_bot' }
  ];

  var CSS =
    ':host{all:initial;display:block;}' +
    '.bar{box-sizing:border-box;height:30px;display:flex;align-items:center;gap:10px;' +
    'padding:0 10px;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;' +
    'font-size:12.5px;line-height:1;background:var(--ab-bg,#eaeaea);' +
    'border-bottom:1px solid var(--ab-border,rgba(0,0,0,.08));color:var(--ab-text,rgba(0,0,0,.6));' +
    'transition:background-color .15s ease,border-color .15s ease,color .15s ease;overflow:hidden;}' +
    '.label{flex:0 0 auto;opacity:.65;white-space:nowrap;}' +
    '.items{display:flex;align-items:center;gap:4px;flex:1;min-width:0;height:100%;' +
    'overflow-x:auto;overflow-y:hidden;white-space:nowrap;scrollbar-width:none;-ms-overflow-style:none;}' +
    '.items::-webkit-scrollbar{display:none;}' +
    '.item{flex:0 0 auto;display:inline-flex;align-items:center;padding:4px 9px;border-radius:6px;' +
    'text-decoration:none;color:inherit;transition:color .15s ease,background-color .15s ease;cursor:pointer;}' +
    'a.item:hover{color:var(--ab-text-hover);background:var(--ab-item-hover-bg);}' +
    '.item.current{font-weight:600;color:var(--ab-current-color);background:var(--ab-current-bg);cursor:default;}' +
    '@media(max-width:640px){.label{display:none;}}';

  function localPortStartsWith(prefix) {
    var host = location.hostname;
    if (host !== 'localhost' && host !== '127.0.0.1') return false;
    return String(location.port || '').indexOf(prefix) === 0;
  }

  function detectAppId() {
    var host = location.hostname;
    var path = location.pathname;
    if (host.indexOf('reelscribe') !== -1 || localPortStartsWith('524')) {
      return path.indexOf('/radar') === 0 ? 'reels-radar' : 'reels-parser';
    }
    if (host.indexOf('chertogi') !== -1 || localPortStartsWith('525')) {
      return 'chertogi';
    }
    if (host.indexOf('ngrok') !== -1 || localPortStartsWith('526')) {
      return path.indexOf('/parser') === 0 ? 'yt-parser' : 'yt-radar';
    }
    return null;
  }

  function currentAppId() {
    return forcedApp || detectAppId();
  }

  // ---------- цвет страницы ----------

  function parseRgba(str) {
    if (!str) return null;
    if (str === 'transparent') return { r: 0, g: 0, b: 0, a: 0 };
    var m = str.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    var parts = m[1].split(',').map(function (s) { return parseFloat(s); });
    return { r: parts[0] || 0, g: parts[1] || 0, b: parts[2] || 0, a: parts.length > 3 ? parts[3] : 1 };
  }

  function opaqueBg(el) {
    if (!el) return null;
    var cs;
    try { cs = getComputedStyle(el); } catch (e) { return null; }
    if (!cs) return null;
    var c = parseRgba(cs.backgroundColor);
    if (c && c.a > 0.5) return c;
    // Фон градиентом (например, Чертоги: background: var(--bg-grad)) — цвета нет в
    // background-color, берём средний из непрозрачных цветов градиента
    var img = cs.backgroundImage || '';
    if (img.indexOf('gradient') !== -1) {
      var found = (img.match(/rgba?\([^)]+\)/g) || []).map(parseRgba).filter(function (x) { return x && x.a > 0.5; });
      if (found.length) {
        var sum = found.reduce(function (acc, x) { return { r: acc.r + x.r, g: acc.g + x.g, b: acc.b + x.b }; }, { r: 0, g: 0, b: 0 });
        return { r: sum.r / found.length, g: sum.g / found.length, b: sum.b / found.length, a: 1 };
      }
    }
    return null;
  }

  function findPageBg(hostEl) {
    var chain = [];
    try {
      var els = document.elementsFromPoint(window.innerWidth / 2, window.innerHeight / 2) || [];
      for (var i = 0; i < els.length; i++) {
        if (els[i] !== hostEl) chain.push(els[i]);
      }
    } catch (e) { /* окружение без elementsFromPoint — уходим на фолбэк ниже */ }
    var el = chain[0] || null;
    while (el) {
      var c = opaqueBg(el);
      if (c) return c;
      el = el.parentElement;
    }
    var b = opaqueBg(document.body);
    if (b) return b;
    var h = opaqueBg(document.documentElement);
    if (h) return h;
    return { r: 255, g: 255, b: 255, a: 1 };
  }

  function luminance(c) {
    function chan(v) { v = v / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    return 0.2126 * chan(c.r) + 0.7152 * chan(c.g) + 0.0722 * chan(c.b);
  }

  function mixBlack(c, pct) {
    var f = 1 - pct;
    return { r: Math.round(c.r * f), g: Math.round(c.g * f), b: Math.round(c.b * f) };
  }

  // ---------- сборка ----------

  function build() {
    var host = document.createElement('div');
    host.id = 'apps-bar';
    var shadow = host.attachShadow({ mode: 'open' });

    var style = document.createElement('style');
    style.textContent = CSS;
    shadow.appendChild(style);

    var bar = document.createElement('div');
    bar.className = 'bar';

    var label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'Мои сервисы';

    var items = document.createElement('div');
    items.className = 'items';

    bar.appendChild(label);
    bar.appendChild(items);
    shadow.appendChild(bar);

    document.body.insertBefore(host, document.body.firstChild);
    document.documentElement.style.setProperty('--apps-bar-h', '30px');

    function renderItems() {
      var curId = currentAppId();
      items.innerHTML = '';
      APPS.forEach(function (app) {
        var isCurrent = app.id === curId;
        var el;
        if (isCurrent) {
          el = document.createElement('span');
          el.className = 'item current';
          el.setAttribute('aria-current', 'page');
        } else {
          el = document.createElement('a');
          el.className = 'item';
          el.href = app.href;
          if (app.id === 'tg-bot') {
            el.target = '_blank';
            el.rel = 'noopener';
          }
        }
        el.textContent = app.title;
        items.appendChild(el);
      });
    }

    function applyColors() {
      var bg = findPageBg(host);
      var isLight = luminance(bg) > 0.5;
      var barBg = mixBlack(bg, isLight ? 0.04 : 0.25);
      var vars = {
        '--ab-bg': 'rgb(' + barBg.r + ',' + barBg.g + ',' + barBg.b + ')',
        '--ab-border': isLight ? 'rgba(0,0,0,.08)' : 'rgba(255,255,255,.06)',
        '--ab-text': isLight ? 'rgba(0,0,0,.6)' : 'rgba(255,255,255,.62)',
        '--ab-text-hover': isLight ? 'rgba(0,0,0,.9)' : '#fff',
        '--ab-item-hover-bg': isLight ? 'rgba(0,0,0,.05)' : 'rgba(255,255,255,.06)',
        '--ab-current-color': isLight ? '#1a1a22' : '#fff',
        '--ab-current-bg': isLight ? 'rgba(0,0,0,.07)' : 'rgba(255,255,255,.12)'
      };
      Object.keys(vars).forEach(function (k) { host.style.setProperty(k, vars[k]); });
    }

    // Пересчёт с повтором: новая страница/тема дорисовывается не мгновенно.
    function recalcSoon() {
      renderItems();
      applyColors();
      setTimeout(applyColors, 150);
      setTimeout(applyColors, 800);
    }

    recalcSoon();
    window.addEventListener('load', recalcSoon);

    // SPA-навигация: перехват history.pushState/replaceState + popstate.
    ['pushState', 'replaceState'].forEach(function (method) {
      var orig = history[method];
      history[method] = function () {
        var ret = orig.apply(this, arguments);
        recalcSoon();
        return ret;
      };
    });
    window.addEventListener('popstate', recalcSoon);

    // Смена темы/классов страницы (class, data-theme, inline style на html/body).
    // recalcSoon, а не сразу: новые значения темы (переменные, переходы) дорисовываются не мгновенно
    var mo = new MutationObserver(function () { recalcSoon(); });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme', 'style'] });
    if (document.body) {
      mo.observe(document.body, { attributes: true, attributeFilter: ['class', 'data-theme', 'style'] });
    }

    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      if (mq.addEventListener) mq.addEventListener('change', applyColors);
      else if (mq.addListener) mq.addListener(applyColors);
    }
  }

  function init() {
    if (document.getElementById('apps-bar')) return;
    if (!document.body) {
      document.addEventListener('DOMContentLoaded', init);
      return;
    }
    build();
  }

  init();
})();
