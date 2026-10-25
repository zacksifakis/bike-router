const PRESETS = {
  fastest:   { bike: 0,   green: 0, lively: 0,  dock: 0 },
  commute:   { bike: 2,   green: 0, lively: -0.5, dock: 0 },
  scenic:    { bike: 1,   green: 3, lively: -1, dock: 0 },
  bikeshare: { bike: 1,   green: 0, lively: 0,  dock: 3 },
  buzz:      { bike: 0.5, green: 0, lively: 2.5, dock: 0 },
};
const SLIDERS = ["bike", "green", "lively", "dock"];

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

function weights() {
  return Object.fromEntries(SLIDERS.map((id) => [id, document.getElementById(id).value]));
}

function showValues() {
  for (const id of SLIDERS) {
    const v = Number(document.getElementById(id).value);
    let text = v.toFixed(2).replace(/\.?0+$/, "");
    if (id === "lively" && v !== 0) text = v < 0 ? `avoid ${-v}` : `seek ${v}`;
    if (v === 0) text = "off";
    document.querySelector(`output[data-for="${id}"]`).textContent = text;
  }
}

showValues();

let timer;
for (const id of SLIDERS) {
  document.getElementById(id).addEventListener("input", () => {
    showValues();
    clearTimeout(timer);
    timer = setTimeout(route, 200);
  });
}
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

let seq = 0;
let fitted = false; // zoom to the first route only, then leave the view to the user
async function route() {
  if (!start || !end || !map.getSource("preferred")) return; // the load handler routes once sources exist
  const id = ++seq;
  const params = new URLSearchParams({
    from_lat: start[1], from_lon: start[0], to_lat: end[1], to_lon: end[0], ...weights(),
  });
  const res = await fetch(`/api/route?${params}`);
  if (id !== seq) return; // a newer request superseded this one
  const body = await res.json();
  const hint = document.getElementById("hint");
  if (!res.ok) {
    hint.textContent = body.detail || "Routing failed.";
    hint.classList.add("error");
    for (const name of ["shortest", "preferred"]) map.getSource(name).setData({ type: "FeatureCollection", features: [] });
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

  document.getElementById("stats").innerHTML = ROWS.map(([label, f]) =>
    `<tr><td>${label}</td><td>${f(body.preferred)}</td><td>${f(body.shortest)}</td></tr>`).join("");
  const extra = body.preferred.distance_m - body.shortest.distance_m;
  document.getElementById("streets").textContent =
    (extra > 20 ? `+${fmtKm(extra)} detour. ` : "") + "Via " + (body.preferred.streets.join(" → ") || "unnamed paths");
  document.getElementById("results").hidden = false;
}

async function loadStations() {
  const stations = await (await fetch("/api/stations")).json();
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
  for (const name of ["shortest", "preferred", "stations"]) map.addSource(name, { type: "geojson", data: empty });

  map.addLayer({
    id: "shortest", type: "line", source: "shortest",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#8a94a3", "line-width": 4, "line-dasharray": [1, 2] },
  });
  map.addLayer({
    id: "preferred", type: "line", source: "preferred",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#0f7b5f", "line-width": 6, "line-opacity": 0.9 },
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
  loadStations();
  setInterval(loadStations, 60_000);
  route();
});
