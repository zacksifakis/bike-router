const PRESETS = {
  fastest:   { bike: 0,   green: 0, lively: 0,  dock: 0 },
  commute:   { bike: 2,   green: 0, lively: -0.5, dock: 0 },
  scenic:    { bike: 1,   green: 3, lively: -1, dock: 0 },
  bikeshare: { bike: 1,   green: 0, lively: 0,  dock: 3 },
  buzz:      { bike: 0.5, green: 0, lively: 2.5, dock: 0 },
};
const SLIDERS = ["bike", "green", "lively", "dock"];

// Any GBFS feed works here, including Lyft's (Citi Bike, Divvy, Bay Wheels, ...).
const GBFS_URL = "https://gbfs.bcycle.com/1/bcycle_madison/gbfs.json";

// Routing runs in a worker (router.js) so the page stays responsive.
const worker = new Worker("worker.js", { type: "module" });
const pending = new Map();
let nextId = 0;
worker.onmessage = ({ data }) => {
  pending.get(data.id)(data);
  pending.delete(data.id);
};
function callRouter(params) {
  const id = nextId++;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    worker.postMessage({ id, params, stations });
  });
}

let stations = [];

// Demo trip: Memorial Union to Olbrich Botanical Gardens, across the isthmus.
let start = [-89.3999, 43.0766];
let end = [-89.3369, 43.0928];

const map = new maplibregl.Map({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/liberty",
  center: [-89.38, 43.075],
  zoom: 12.5,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl(), "top-right");

const startMarker = new maplibregl.Marker({ color: "#0f7b5f", draggable: true });
const endMarker = new maplibregl.Marker({ color: "#c2410c", draggable: true });
startMarker.on("dragend", () => { start = startMarker.getLngLat().toArray(); route(); });
endMarker.on("dragend", () => { end = endMarker.getLngLat().toArray(); route(); });

let settingStart = true;
map.on("click", (e) => {
  if (map.queryRenderedFeatures(e.point, { layers: ["stations"] }).length) return;
  if (settingStart) {
    start = e.lngLat.toArray();
    startMarker.setLngLat(start).addTo(map);
    document.getElementById("hint").textContent = "Now click your destination.";
  } else {
    end = e.lngLat.toArray();
    endMarker.setLngLat(end).addTo(map);
    document.getElementById("hint").textContent = "Click again to pick a new start. Drag pins to adjust.";
    route();
  }
  settingStart = !settingStart;
});

const batteryOn = () => document.getElementById("battery-on").checked;

function weights() {
  const w = Object.fromEntries(SLIDERS.map((id) => [id, Number(document.getElementById(id).value)]));
  if (batteryOn()) {
    w.battery = Number(document.getElementById("battery").value);
    w.rangeKm = Number(document.getElementById("range_km").value);
  }
  return w;
}

function showValues() {
  for (const id of SLIDERS) {
    const v = Number(document.getElementById(id).value);
    let text = v.toFixed(2).replace(/\.?0+$/, "");
    if (id === "lively" && v !== 0) text = v < 0 ? `avoid ${-v}` : `seek ${v}`;
    if (v === 0) text = "off";
    document.querySelector(`output[data-for="${id}"]`).textContent = text;
  }
  document.querySelector('output[data-for="battery"]').textContent = `${document.getElementById("battery").value}%`;
}

showValues();

let timer;
for (const id of [...SLIDERS, "battery"]) {
  document.getElementById(id).addEventListener("input", () => {
    showValues();
    clearTimeout(timer);
    timer = setTimeout(route, 200);
  });
}
document.getElementById("battery-on").addEventListener("change", () => {
  document.getElementById("battery-controls").hidden = !batteryOn();
  route();
});
document.getElementById("range_km").addEventListener("change", route);

for (const btn of document.querySelectorAll("[data-preset]")) {
  btn.addEventListener("click", () => {
    const p = PRESETS[btn.dataset.preset];
    for (const id of SLIDERS) document.getElementById(id).value = p[id];
    showValues();
    route();
  });
}

const fmtKm = (m) => `${(m / 1000).toFixed(2)} km`;
const ROWS = [
  ["Distance", (r) => fmtKm(r.distance_m)],
  ["Time (15 km/h)", (r) => `${Math.round(r.duration_min)} min`],
  ["On bike paths/lanes", (r) => `${r.bike_infra_pct}%`],
  ["Near nature/water", (r) => `${r.green_pct}%`],
  ["Busy streets", (r) => `${r.lively_pct}%`],
  ["Farthest from a dock", (r) => `${r.max_dock_gap_m} m`],
];

const BATTERY_ROWS = [
  ["Battery on arrival", (b) => `${b.arrive_pct}%`],
  ["Bike swaps", (b) => b.swaps.length || "none"],
  ["Closest call", (b) => `${fmtKm(b.min_margin_m)} spare`],
];

const escape = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// Route colour by charge left: red when nearly empty, amber when low, green otherwise.
const CHARGE_COLORS = [[0, [220, 38, 38]], [20, [245, 158, 11]], [50, [15, 123, 95]]];
function chargeColor(pct) {
  for (let i = 1; i < CHARGE_COLORS.length; i++) {
    const [p0, c0] = CHARGE_COLORS[i - 1], [p1, c1] = CHARGE_COLORS[i];
    if (pct <= p1) {
      const t = Math.max(0, (pct - p0) / (p1 - p0));
      return `rgb(${c0.map((c, k) => Math.round(c + (c1[k] - c) * t)).join(",")})`;
    }
  }
  return `rgb(${CHARGE_COLORS.at(-1)[1].join(",")})`;
}

const PLAIN_LINE = ["interpolate", ["linear"], ["line-progress"], 0, "#0f7b5f", 1, "#0f7b5f"];

function chargeGradient(profile) {
  // profile: [fraction along route, charge %] breakpoints; a swap shows up as two points at the same fraction.
  const expr = ["interpolate", ["linear"], ["line-progress"]];
  let last = -1;
  for (let k = 0; k + 1 < profile.length; k++) {
    const [f0, p0] = profile[k], [f1, p1] = profile[k + 1];
    if (f1 <= f0) continue;
    for (let s = 0; s <= 6; s++) {
      const f = Math.min(1, Math.max(f0 + (f1 - f0) * s / 6, last + 1e-6));
      if (f <= last) continue;
      expr.push(f, chargeColor(p0 + (p1 - p0) * s / 6));
      last = f;
    }
  }
  return expr.length >= 7 ? expr : PLAIN_LINE;
}

let swapMarkers = [];
function showBattery(b) {
  for (const m of swapMarkers) m.remove();
  swapMarkers = [];
  const note = document.getElementById("battery-note");
  map.setPaintProperty("preferred", "line-gradient", b ? chargeGradient(b.profile) : PLAIN_LINE);
  if (!b) {
    note.hidden = true;
    return;
  }
  for (const s of b.swaps) {
    const el = document.createElement("div");
    el.className = "swap-marker";
    el.textContent = "Swap";
    swapMarkers.push(new maplibregl.Marker({ element: el }).setLngLat([s.lon, s.lat]).addTo(map));
  }
  const parts = b.swaps.map((s) =>
    `Swap at <strong>${escape(s.name)}</strong> after ${fmtKm(s.at_m)} (${s.ebikes} e-bikes there now).`);
  const w = b.without;
  if (w.unsafe_at_m != null) {
    parts.push(`Without battery mode you'd be out of reach of any dock after ${fmtKm(w.unsafe_at_m)}` +
      (w.empty_at_m != null ? ` and run flat at ${fmtKm(w.empty_at_m)}.` : "."));
  } else {
    parts.push("Your normal route is already safe on this charge.");
  }
  note.innerHTML = parts.join(" ");
  note.hidden = false;
}

let seq = 0;
let fitted = false; // zoom to the first route only, then leave the view to the user
async function route() {
  if (!start || !end || !map.getSource("preferred")) return; // the load handler routes once sources exist
  const id = ++seq;
  const res = await callRouter({ from: [start[1], start[0]], to: [end[1], end[0]], ...weights() });
  if (id !== seq) return; // a newer request superseded this one
  const body = res.body;
  const hint = document.getElementById("hint");
  if (!res.ok) {
    hint.textContent = res.detail;
    hint.classList.add("error");
    for (const name of ["shortest", "preferred"]) map.getSource(name).setData({ type: "FeatureCollection", features: [] });
    showBattery(null);
    document.getElementById("results").hidden = true;
    return;
  }
  if (hint.classList.contains("error")) {
    hint.textContent = "Click to pick a new start. Drag the pins to adjust.";
    hint.classList.remove("error");
  }
  map.getSource("shortest").setData(body.shortest.geometry);
  map.getSource("preferred").setData(body.preferred.geometry);
  if (!fitted) {
    const bounds = new maplibregl.LngLatBounds();
    for (const c of [...body.preferred.geometry.coordinates, ...body.shortest.geometry.coordinates]) bounds.extend(c);
    const mobile = window.innerWidth <= 600;
    map.fitBounds(bounds, { padding: mobile ? { top: 40, bottom: window.innerHeight * 0.55 + 20, left: 30, right: 30 }
                                             : { top: 60, bottom: 60, left: 380, right: 60 } });
    fitted = true;
  }

  const b = body.preferred.battery;
  document.getElementById("stats").innerHTML = ROWS.map(([label, f]) =>
    `<tr><td>${label}</td><td>${f(body.preferred)}</td><td>${f(body.shortest)}</td></tr>`).join("") +
    (b ? BATTERY_ROWS.map(([label, f]) => `<tr><td>${label}</td><td>${f(b)}</td><td>–</td></tr>`).join("") : "");
  showBattery(b);
  const extra = body.preferred.distance_m - body.shortest.distance_m;
  document.getElementById("streets").textContent =
    (extra > 20 ? `+${fmtKm(extra)} detour. ` : "") + "Via " + (body.preferred.streets.join(" → ") || "unnamed paths");
  document.getElementById("results").hidden = false;
}

async function fetchStations() {
  const root = await (await fetch(GBFS_URL)).json();
  const urls = Object.fromEntries(Object.values(root.data)[0].feeds.map((f) => [f.name, f.url]));
  const [info, status] = await Promise.all(
    [urls.station_information, urls.station_status].map((u) => fetch(u).then((r) => r.json())));
  const byId = new Map(status.data.stations.map((s) => [s.station_id, s]));
  return info.data.stations.map((s) => {
    const st = byId.get(s.station_id) || {};
    return {
      id: s.station_id, name: s.name, lat: s.lat, lon: s.lon,
      bikes: st.num_bikes_available ?? 0, docks: st.num_docks_available ?? 0,
      // BCycle nests e-bike counts by type; Lyft feeds use a flat field.
      ebikes: st.num_bikes_available_types?.electric ?? st.num_ebikes_available ?? 0,
      active: Boolean(st.is_renting),
    };
  });
}

async function loadStations() {
  try {
    stations = await fetchStations();
  } catch {
    return; // keep showing the last good snapshot
  }
  map.getSource("stations").setData({
    type: "FeatureCollection",
    features: stations.map((s) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [s.lon, s.lat] },
      properties: s,
    })),
  });
}

map.on("load", () => {
  const empty = { type: "FeatureCollection", features: [] };
  for (const name of ["shortest", "preferred", "stations"]) {
    // lineMetrics lets the preferred route be coloured by charge along its length.
    map.addSource(name, { type: "geojson", data: empty, lineMetrics: name === "preferred" });
  }

  map.addLayer({
    id: "shortest", type: "line", source: "shortest",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#8a94a3", "line-width": 4, "line-dasharray": [1, 2] },
  });
  map.addLayer({
    id: "preferred", type: "line", source: "preferred",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-gradient": PLAIN_LINE, "line-width": 6, "line-opacity": 0.9 },
  });
  map.addLayer({
    id: "stations", type: "circle", source: "stations",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 3, 15, 7],
      "circle-color": ["case", ["==", ["get", "active"], false], "#9ca3af",
        ["==", ["get", "bikes"], 0], "#f59e0b", "#2563eb"],
      "circle-stroke-color": "#fff",
      "circle-stroke-width": 1.5,
    },
  });

  map.on("click", "stations", (e) => {
    const s = e.features[0].properties;
    new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(`<strong>${s.name}</strong><br>${s.bikes} bikes (${s.ebikes} e-bikes) · ${s.docks} open docks`)
      .addTo(map);
  });
  map.on("mouseenter", "stations", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "stations", () => { map.getCanvas().style.cursor = ""; });

  startMarker.setLngLat(start).addTo(map);
  endMarker.setLngLat(end).addTo(map);
  setInterval(loadStations, 60_000);
  loadStations().then(route);
});
