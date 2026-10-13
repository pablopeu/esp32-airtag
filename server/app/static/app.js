/* Find You web UI: shared helpers, dashboard and tag detail pages. */

const $ = (sel, root = document) => root.querySelector(sel);

function toast(message, isError = false) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 4000);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const isJson = (res.headers.get("content-type") || "").includes("application/json");
  const body = isJson ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = (body && body.error) || res.statusText || "Request failed";
    throw new Error(msg);
  }
  return body;
}

function fmtTime(iso) {
  if (!iso) return "never";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function relative(iso) {
  if (!iso) return "";
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

/* Wire the header Refresh button (present on every page). */
function wireRefresh() {
  const btn = $("#btn-refresh");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await api("/api/refresh", { method: "POST" });
      toast("Refresh requested — locations update within a minute");
    } catch (e) {
      toast(e.message, true);
    } finally {
      setTimeout(() => { btn.disabled = false; }, 1500);
    }
  });
}

/* ---------------------------------------------------------------- dashboard */

async function initIndex() {
  wireRefresh();
  await refreshStatus();
  await loadTags();
  $("#import-form").addEventListener("submit", onImport);
  $("#apple-login-form").addEventListener("submit", onAppleLogin);
  $("#apple-2fa-form").addEventListener("submit", onApple2fa);

  // Keep the dashboard fresh.
  setInterval(async () => { await refreshStatus(); await loadTags(); }, 30000);
}

async function refreshStatus() {
  let status;
  try {
    status = await api("/api/status");
  } catch (e) {
    $("#apple-status").textContent = "Status unavailable: " + e.message;
    return;
  }
  const el = $("#apple-status");
  const loginForm = $("#apple-login-form");
  const tfaForm = $("#apple-2fa-form");
  const state = status.apple.state;

  loginForm.hidden = state !== "logged_out";
  tfaForm.hidden = state !== "2fa_required";

  if (state === "logged_in") {
    el.innerHTML = `<span class="ok">Connected</span> as ${status.apple.account_name || "Apple ID"}
      <button class="btn ghost" id="apple-logout" type="button">Disconnect</button>`;
    const out = $("#apple-logout");
    if (out) out.addEventListener("click", async () => {
      await api("/api/apple/logout", { method: "POST" });
      toast("Apple account disconnected");
      refreshStatus();
    });
  } else if (state === "2fa_required") {
    el.textContent = "Two-factor authentication required";
    await load2faMethods();
  } else {
    el.textContent = "Not connected — add an Apple ID to fetch locations.";
  }

  const info = $("#refresh-info");
  if (info) {
    const when = status.last_refresh ? relative(new Date(status.last_refresh * 1000).toISOString()) : "never";
    info.textContent = `Last refresh: ${when}`;
  }
}

async function load2faMethods() {
  // Methods are returned by the login call and cached on the element.
  const box = $("#apple-2fa-methods");
  if (!box || box.dataset.loaded) return;
  box.dataset.loaded = "1";
}

async function onAppleLogin(ev) {
  ev.preventDefault();
  const form = ev.target;
  const btn = form.querySelector("button");
  btn.disabled = true;
  try {
    const data = await api("/api/apple/login", { method: "POST", body: new FormData(form) });
    if (data.state === "2fa_required") {
      render2fa(data.methods || []);
      toast("Enter your two-factor code");
    } else if (data.state === "logged_in") {
      toast("Apple account connected");
    } else {
      toast("Unexpected state: " + data.state, true);
    }
    await refreshStatus();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

function render2fa(methods) {
  const box = $("#apple-2fa-methods");
  const tfaForm = $("#apple-2fa-form");
  tfaForm.hidden = false;
  box.innerHTML = "";
  methods.forEach((m, i) => {
    if (m.kind === "trusted_device") {
      const chip = document.createElement("span");
      chip.className = "btn ghost";
      chip.textContent = `Trusted device #${i + 1}`;
      chip.onclick = () => {
        tfaForm.method.value = "trusted_device";
        tfaForm.phone_id.value = "";
      };
      box.appendChild(chip);
    } else {
      const chip = document.createElement("span");
      chip.className = "btn ghost";
      chip.textContent = `SMS ••••${m.last_digits}`;
      chip.onclick = async () => {
        tfaForm.method.value = "sms";
        tfaForm.phone_id.value = m.phone_id;
        try {
          await api("/api/apple/sms", { method: "POST", body: new URLSearchParams({ phone_id: m.phone_id }) });
          toast("SMS code sent");
        } catch (e) {
          toast(e.message, true);
        }
      };
      box.appendChild(chip);
    }
  });
}

async function onApple2fa(ev) {
  ev.preventDefault();
  const form = ev.target;
  try {
    const data = await api("/api/apple/2fa", { method: "POST", body: new FormData(form) });
    if (data.state === "logged_in") {
      toast("Apple account connected");
      $("#apple-2fa-form").hidden = true;
    } else {
      toast(data.error || "2FA failed", true);
    }
    await refreshStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

async function onImport(ev) {
  ev.preventDefault();
  const form = ev.target;
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const data = await api("/api/import", { method: "POST", body: new FormData(form) });
    toast(`Imported ${data.tag_ids.length} tag(s)`);
    form.reset();
    await loadTags();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function loadTags() {
  const list = $("#tag-list");
  if (!list) return;
  let data;
  try {
    data = await api("/api/tags");
  } catch (e) {
    list.innerHTML = `<p class="error">${e.message}</p>`;
    return;
  }
  if (!data.tags.length) {
    list.innerHTML = `<p class="muted">No tags yet. Import an accessory_list.plist above.</p>`;
    return;
  }
  list.innerHTML = "";
  data.tags.forEach((t) => {
    const item = document.createElement("div");
    item.className = "tag-item";
    const pos = (t.last_lat != null)
      ? `${t.last_lat.toFixed(5)}, ${t.last_lon.toFixed(5)}`
      : "no location yet";
    item.innerHTML = `
      <div class="tag-main">
        <span class="tag-name"><a href="/tag/${t.id}">${escapeHtml(t.name)}</a></span>
        <span class="muted small">${t.key_count} keys · ${pos} · ${t.last_ts ? relative(t.last_ts) : "—"}</span>
      </div>
      <div class="row">
        <a class="btn" href="/tag/${t.id}">Map</a>
        <button class="btn danger" type="button">Delete</button>
      </div>`;
    item.querySelector("button.danger").addEventListener("click", async () => {
      if (!confirm(`Delete "${t.name}" and its location history?`)) return;
      try {
        await api(`/api/tags/${t.id}/delete`, { method: "POST" });
        toast("Tag deleted");
        loadTags();
      } catch (e) {
        toast(e.message, true);
      }
    });
    list.appendChild(item);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* --------------------------------------------------------------- tag detail */

let map, marker, trail, polyline;

async function initTag(tagId) {
  wireRefresh();
  map = L.map("map", { worldCopyJump: true }).setView([0, 0], 2);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  polyline = L.polyline([], { color: "#4f8cff", weight: 3, opacity: 0.85 }).addTo(map);

  $("#history-hours").addEventListener("change", () => loadReports(tagId));
  await loadReports(tagId);
  setInterval(() => loadReports(tagId), 60000);
}

async function loadReports(tagId) {
  const hours = $("#history-hours").value;
  let data;
  try {
    data = await api(`/api/tags/${tagId}/reports?hours=${hours}`);
  } catch (e) {
    toast(e.message, true);
    return;
  }
  const reports = data.reports;
  const meta = $("#tag-meta");
  const tbody = $("#reports-table tbody");

  if (!reports.length) {
    meta.textContent = "No reports in this window (the device only appears when an iPhone passes nearby).";
    tbody.innerHTML = `<tr><td colspan="4" class="muted">No reports.</td></tr>`;
    polyline.setLatLngs([]);
    if (marker) { map.removeLayer(marker); marker = null; }
    return;
  }

  const latest = reports[reports.length - 1];
  meta.innerHTML = `Last seen <strong>${fmtTime(latest.ts)}</strong> (${relative(latest.ts)}) ·
    ${reports.length} report(s) in window`;

  const pts = reports.map((r) => [r.lat, r.lon]);
  polyline.setLatLngs(pts);

  if (!marker) {
    marker = L.marker([latest.lat, latest.lon]).addTo(map);
  } else {
    marker.setLatLng([latest.lat, latest.lon]);
  }
  marker.bindPopup(`<strong>${fmtTime(latest.ts)}</strong><br>
    ±${Math.round(latest.accuracy || 0)} m · confidence ${latest.confidence ?? "?"}`);

  map.fitBounds(L.latLngBounds(pts).pad(0.25), { maxZoom: 16 });

  tbody.innerHTML = "";
  [...reports].reverse().forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${new Date(r.ts).toISOString().replace("T", " ").slice(0, 19)}</td>
      <td>${r.lat.toFixed(6)}</td><td>${r.lon.toFixed(6)}</td>
      <td>${r.accuracy != null ? "±" + Math.round(r.accuracy) + " m" : "—"}</td>`;
    tr.addEventListener("click", () => {
      map.setView([r.lat, r.lon], 16);
      marker.openPopup();
    });
    tbody.appendChild(tr);
  });
}
