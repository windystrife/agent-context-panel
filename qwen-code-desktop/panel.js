/* Qwen Code Desktop - in-app conversation context panel.
   Injected into resources/app/dist/renderer/index.html by inject.py.

   The app records token usage but never displays it; qwen-monitor serves those
   records on 127.0.0.1:8098 and this draws them inside the app window.

   Runs in a Shadow DOM so no styles leak either way. Read-only, no app APIs. */
(function () {
  if (window.__qwenCtxPanel) return;
  window.__qwenCtxPanel = true;

  var API = "http://127.0.0.1:8098/api/stats";
  var host = document.createElement("div");
  host.id = "__qwen_ctx_panel";
  host.style.cssText = "position:fixed;right:14px;bottom:14px;z-index:2147483000";
  var sh = host.attachShadow({ mode: "open" });
  sh.innerHTML =
    '<style>' +
    ':host{display:block;line-height:normal;color:#e6e8ec}' +
    '*{box-sizing:border-box;font:12px/1.45 ui-sans-serif,system-ui,"Segoe UI",sans-serif}' +
    '.pill{display:flex;align-items:center;gap:8px;padding:7px 12px;border-radius:999px;' +
    ' background:#1e2128f2;color:#e6e8ec;border:1px solid #333945;cursor:pointer;' +
    ' box-shadow:0 4px 16px #0006;backdrop-filter:blur(6px);user-select:none;white-space:nowrap}' +
    '.pill:hover{border-color:#4b5464}' +
    '.dot{width:9px;height:9px;border-radius:50%;flex:none}' +
    '.mono{font-variant-numeric:tabular-nums}' +
    '.dim{color:#8b93a1}' +
    '.card{display:none;width:310px;padding:14px 15px;border-radius:14px;margin-bottom:9px;' +
    ' background:#1e2128f7;color:#e6e8ec;border:1px solid #333945;' +
    ' box-shadow:0 10px 34px #0008;backdrop-filter:blur(8px)}' +
    '.card.open{display:block}' +
    '.hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:11px}' +
    '.big{font-size:21px;font-weight:650;letter-spacing:-.3px}' +
    '.bar{height:6px;background:#0f1116;border-radius:4px;overflow:hidden;margin:7px 0 3px}' +
    '.bar>i{display:block;height:100%;border-radius:4px;transition:width .35s}' +
    '.g{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:11px}' +
    '.b{background:#191c22;border:1px solid #2c313b;border-radius:9px;padding:8px 10px}' +
    '.b .v{font-size:15px;font-weight:600}' +
    '.x{cursor:pointer;color:#8b93a1;padding:0 3px}.x:hover{color:#e6e8ec}' +
    '</style>' +
    '<div class="card" id="card"></div>' +
    '<div class="pill" id="pill"><span class="dot" id="dot"></span>' +
    '<span class="mono" id="txt">ctx …</span></div>';

  var pill = sh.getElementById("pill"),
      card = sh.getElementById("card"),
      dot = sh.getElementById("dot"),
      txt = sh.getElementById("txt");
  var open = false, cur = null;

  pill.addEventListener("click", function () {
    open = !open;
    card.classList.toggle("open", open);
    draw();
  });

  function n(v, d) {
    if (v == null) return "-";
    return Number(v).toLocaleString(undefined, { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  }
  function k(v) {
    if (v == null) return "-";
    return v >= 1e6 ? (v / 1e6).toFixed(1) + "m" : v >= 1e3 ? Math.round(v / 1e3) + "k" : String(v);
  }
  function col(p) { return p >= 90 ? "#f87171" : p >= 70 ? "#fbbf24" : "#818cf8"; }

  // A coding session often costs a fraction of a cent. Two decimals renders
  // every one of them as "$0.00", which reads as "no estimate" rather than
  // "cheap", so scale the precision to the amount.
  function money(v, priced) {
    if (!priced) return "no price";
    if (v == null) return "-";
    if (v >= 1) return "$" + v.toFixed(2);
    if (v >= 0.01) return "$" + v.toFixed(3);
    if (v > 0) return "$" + v.toFixed(4);
    return "$0";
  }

  function draw() {
    if (!cur) {
      dot.style.background = "#6b7280";
      txt.textContent = "ctx: monitor off";
      card.innerHTML = '<div class="dim">qwen-monitor is not running.<br>' +
        'Start <b>start-qwen-monitor.cmd</b> (port 8098).</div>';
      return;
    }
    var p = cur.context_pct || 0;
    dot.style.background = col(p);
    txt.innerHTML = '<b>' + n(p, 1) + '%</b> <span class="dim">' +
      k(cur.context_used) + "/" + k(cur.context_window) + "</span>";
    if (!open) return;
    card.innerHTML =
      '<div class="hdr"><span class="dim">Conversation context</span>' +
      '<span class="x" id="cl">✕</span></div>' +
      '<div class="big mono">' + k(cur.context_used) +
      ' <span class="dim" style="font-size:13px;font-weight:400">/ ' + k(cur.context_window) + '</span></div>' +
      '<div class="bar"><i style="width:' + Math.min(100, p) + '%;background:' + col(p) + '"></i></div>' +
      '<div class="dim mono">' + n(p, 1) + '% used · ' + n(cur.context_available) + ' available</div>' +
      '<div class="g">' +
      '<div class="b"><div class="dim">Total tokens</div><div class="v mono">' + k(cur.input + cur.output) + '</div></div>' +
      '<div class="b"><div class="dim">Cost (est.)</div><div class="v mono">' + money(cur.cost, cur.priced) + '</div></div>' +
      '<div class="b"><div class="dim">Input</div><div class="v mono">' + k(cur.input) + '</div></div>' +
      '<div class="b"><div class="dim">Output</div><div class="v mono">' + k(cur.output) + '</div></div>' +
      '</div>' +
      '<div class="bar" style="margin-top:11px"><i style="width:' + Math.min(100, cur.cache_pct) + '%;background:#4ade80"></i></div>' +
      '<div class="dim mono">Cache ' + n(cur.cache_pct, 0) + '% · read ' + k(cur.cached) +
      ' · ' + cur.requests + ' requests</div>' +
      '<div class="dim" style="margin-top:8px;overflow:hidden;text-overflow:ellipsis">' +
      (cur.model || "") + '</div>' +
      '<div class="dim" style="margin-top:6px;font-size:11px">most recently active session</div>';
    var cl = sh.getElementById("cl");
    if (cl) cl.addEventListener("click", function (e) {
      e.stopPropagation(); open = false; card.classList.remove("open");
    });
  }

  function attach() {
    // The app mounts React after this script parses, and a framework that
    // rewrites document.body would take the panel with it. Re-attaching on
    // every tick is cheaper than watching for it and never gets orphaned.
    if (document.body && !host.isConnected) document.body.appendChild(host);
  }

  function tick() {
    attach();
    fetch(API, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (s) { cur = (s.sessions || [])[0] || null; draw(); })
      .catch(function () { cur = null; draw(); });
  }

  function mount() {
    if (!document.body) return setTimeout(mount, 200);
    attach();
    draw();                       // show the pill immediately, before any fetch
    tick();
    setInterval(tick, 2000);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
