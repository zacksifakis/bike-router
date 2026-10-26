// In-browser port of backend/graph.py, backend/battery.py and the /api/route handler in
// backend/app.py, so the demo runs as static files. Same model, constants and output shape;
// the Python modules explain the reasoning and carry the tests.

const EARTH_R = 6_371_000;
const COS_LAT0 = Math.cos((43.07 * Math.PI) / 180);
const DOCK_FAR_M = 800;
const SNAP_MAX_M = 500;
const RIDE_SPEED_MS = 4.2;
const DOCK_SNAP_M = 150;
const SWAP_CHARGE = 0.9;
const SWAP_PENALTY = 400;
const MAX_EXPANSIONS = 40;
const TOL = 0.5;

const toX = (lon) => ((lon * Math.PI) / 180) * EARTH_R * COS_LAT0;
const toY = (lat) => ((lat * Math.PI) / 180) * EARTH_R;

export class RouteError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

const fmtDist = (m) => (!Number.isFinite(m) ? "unreachable" : m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(1)} km`);

class Heap {
  constructor(capacity = 1024) {
    this.keys = new Float64Array(capacity);
    this.vals = new Int32Array(capacity);
    this.size = 0;
    this.topKey = 0;
  }

  push(key, val) {
    if (this.size === this.keys.length) {
      const keys = new Float64Array(this.size * 2), vals = new Int32Array(this.size * 2);
      keys.set(this.keys);
      vals.set(this.vals);
      this.keys = keys;
      this.vals = vals;
    }
    let i = this.size++;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (this.keys[p] <= key) break;
      this.keys[i] = this.keys[p];
      this.vals[i] = this.vals[p];
      i = p;
    }
    this.keys[i] = key;
    this.vals[i] = val;
  }

  pop() {
    const top = this.vals[0];
    this.topKey = this.keys[0];
    const key = this.keys[--this.size], val = this.vals[this.size];
    let i = 0;
    for (;;) {
      let c = 2 * i + 1;
      if (c >= this.size) break;
      if (c + 1 < this.size && this.keys[c + 1] < this.keys[c]) c++;
      if (this.keys[c] >= key) break;
      this.keys[i] = this.keys[c];
      this.vals[i] = this.vals[c];
      i = c;
    }
    this.keys[i] = key;
    this.vals[i] = val;
    return top;
  }
}

// Edge ids grouped by one endpoint, CSR style: edges[start[v]..start[v+1]] touch v.
function adjacency(keys, n) {
  const start = new Int32Array(n + 1);
  for (let e = 0; e < keys.length; e++) start[keys[e] + 1]++;
  for (let v = 0; v < n; v++) start[v + 1] += start[v];
  const fill = start.slice(0, n), edges = new Int32Array(keys.length);
  for (let e = 0; e < keys.length; e++) edges[fill[keys[e]]++] = e;
  return { start, edges };
}

export async function loadGraph(base) {
  const [meta, buf] = await Promise.all([
    fetch(`${base}graph.json`).then((r) => r.json()),
    fetch(`${base}graph.bin`).then((r) => r.arrayBuffer()),
  ]);
  const TYPES = { "<f4": Float32Array, "<i4": Int32Array, "<u2": Uint16Array };
  const arrays = {};
  for (const [key, { offset, length, dtype }] of Object.entries(meta.arrays)) {
    arrays[key] = new TYPES[dtype](buf, offset, length);
  }
  return new Graph(arrays, meta.names);
}

class Graph {
  constructor(a, names) {
    Object.assign(this, a);
    this.names = names;
    this.n = a.node_lat.length;
    this.m = a.edge_u.length;
    this.length = Float64Array.from(a.length);
    this.nodeX = Float64Array.from(a.node_lon, toX);
    this.nodeY = Float64Array.from(a.node_lat, toY);
    this.midX = Float64Array.from(a.mid_lon, toX);
    this.midY = Float64Array.from(a.mid_lat, toY);
    this.fwd = adjacency(a.edge_u, this.n);
    this.rev = adjacency(a.edge_v, this.n);
  }

  nearest(lat, lon) {
    const x = toX(lon), y = toY(lat);
    let best = -1, bestD = Infinity;
    for (let v = 0; v < this.n; v++) {
      const dx = this.nodeX[v] - x, dy = this.nodeY[v] - y, d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = v; }
    }
    return [best, Math.sqrt(bestD)];
  }

  // Dijkstra on per-edge weights w from sources. reverse=true follows edges backwards,
  // giving distances *to* the sources. With budget, a node is never ridden through when the
  // distance ridden to it exceeds budget[node] (the battery rule, see safeTree). side
  // accumulates a second per-edge quantity along the chosen paths.
  dijkstra(w, sources, { reverse = false, limit = Infinity, budget = null, side = null } = {}) {
    const n = this.n;
    const dist = new Float64Array(n).fill(Infinity);
    const pred = new Int32Array(n).fill(-1);
    const done = new Uint8Array(n);
    const along = budget ? new Float64Array(n).fill(Infinity) : null;
    const sideSum = side ? new Float64Array(n).fill(Infinity) : null;
    const adj = reverse ? this.rev : this.fwd, other = reverse ? this.edge_u : this.edge_v;
    const heap = new Heap();
    for (const s of sources) {
      dist[s] = 0;
      if (along) along[s] = 0;
      if (sideSum) sideSum[s] = 0;
      heap.push(0, s);
    }
    while (heap.size) {
      const v = heap.pop(), d = heap.topKey;
      if (done[v] || d > dist[v]) continue;
      if (d > limit) break;
      done[v] = 1;
      if (budget && along[v] > budget[v]) {
        dist[v] = Infinity; // would strand the rider: don't ride through here
        continue;
      }
      for (let k = adj.start[v]; k < adj.start[v + 1]; k++) {
        const e = adj.edges[k], t = other[e];
        if (done[t]) continue;
        const nd = d + w[e];
        if (nd < dist[t]) {
          dist[t] = nd;
          pred[t] = e;
          if (along) along[t] = along[v] + this.length[e];
          if (sideSum) sideSum[t] = sideSum[v] + side[e];
          heap.push(nd, t);
        }
      }
    }
    for (let v = 0; v < n; v++) if (!done[v]) dist[v] = Infinity;
    return { dist, pred, along, side: sideSum };
  }

  path(pred, src, dst) {
    const edges = [];
    for (let node = dst; node !== src; node = this.edge_u[pred[node]]) {
      if (pred[node] < 0) return null;
      edges.push(pred[node]);
    }
    return edges.reverse();
  }
}

export class Router {
  constructor(graph) {
    this.g = graph;
    this.dockCache = { stations: null };
  }

  snap([lat, lon]) {
    const [v, d] = this.g.nearest(lat, lon);
    if (d > SNAP_MAX_M) throw new RouteError(400, `Point is ${Math.round(d)} m from the nearest bikeable street; pick somewhere in Madison.`);
    return v;
  }

  // Straight-line metres from each edge midpoint to the nearest station with a bike or open dock.
  dockDistance(stations) {
    if (this.dockCache.stations === stations) return this.dockCache.dist;
    const g = this.g, usable = stations.filter((s) => s.active && (s.bikes > 0 || s.docks > 0));
    const dist = new Float64Array(g.m).fill(usable.length ? Infinity : DOCK_FAR_M);
    const sx = usable.map((s) => toX(s.lon)), sy = usable.map((s) => toY(s.lat));
    for (let e = 0; e < g.m; e++) {
      for (let i = 0; i < sx.length; i++) {
        const dx = g.midX[e] - sx[i], dy = g.midY[e] - sy[i], d = Math.sqrt(dx * dx + dy * dy);
        if (d < dist[e]) dist[e] = d;
      }
    }
    this.dockCache = { stations, dist };
    return dist;
  }

  // Stations where you could trade your bike for a charged one, snapped to the graph.
  swapDocks(stations) {
    const nodes = [], docks = [], seen = new Set();
    for (const s of stations) {
      if (!(s.active && s.docks > 0 && s.ebikes > 0)) continue;
      const [v, d] = this.g.nearest(s.lat, s.lon);
      if (d <= DOCK_SNAP_M && !seen.has(v)) {
        seen.add(v);
        nodes.push(v);
        docks.push(s);
      }
    }
    return [nodes, docks];
  }

  route(p, stations) {
    const g = this.g;
    const src = this.snap(p.from), dst = this.snap(p.to);
    const dockDist = this.dockDistance(stations);
    const cost = new Float64Array(g.m);
    for (let e = 0; e < g.m; e++) {
      const livelyPen = p.lively >= 0 ? 1 - g.lively[e] : g.lively[e];
      const dockPen = Math.min(1, dockDist[e] / DOCK_FAR_M);
      cost[e] = g.length[e] * (1 + p.bike * (1 - g.bike[e]) + p.green * (1 - g.green[e])
        + Math.abs(p.lively) * livelyPen + p.dock * dockPen);
    }
    const preferred = g.path(g.dijkstra(cost, [src]).pred, src, dst);
    if (!preferred) throw new RouteError(404, "No bikeable route between those points.");
    const shortest = g.path(g.dijkstra(g.length, [src]).pred, src, dst);

    const chosen = p.battery == null
      ? this.summarize(preferred, dockDist)
      : this.batteryRoute(cost, src, dst, preferred, p.battery, p.rangeKm, dockDist, stations);
    return { preferred: chosen, shortest: this.summarize(shortest, dockDist) };
  }

  summarize(path, dockDist) {
    const g = this.g;
    let total = 0, bike = 0, green = 0, lively = 0, gap = 0, nPoints = 0;
    for (const e of path) {
      const l = g.length[e];
      total += l;
      if (g.bike[e] >= 0.6) bike += l;
      if (g.green[e] >= 0.5) green += l;
      if (g.lively[e] >= 0.5) lively += l;
      gap = Math.max(gap, dockDist[e]);
      nPoints += g.offsets[e + 1] - g.offsets[e];
    }
    const coordinates = new Array(nPoints);
    let k = 0;
    const streets = [];
    for (const e of path) {
      for (let i = g.offsets[e]; i < g.offsets[e + 1]; i++) {
        coordinates[k++] = [+g.coords[2 * i].toFixed(6), +g.coords[2 * i + 1].toFixed(6)];
      }
      const name = g.names[g.name_idx[e]];
      if (name && streets.at(-1) !== name) streets.push(name);
    }
    const pct = (x) => (total ? Math.round((100 * x) / total) : 0);
    return {
      geometry: { type: "LineString", coordinates },
      distance_m: Math.round(total),
      duration_min: Math.round((total / RIDE_SPEED_MS / 60) * 10) / 10,
      bike_infra_pct: pct(bike),
      green_pct: pct(green),
      lively_pct: pct(lively),
      max_dock_gap_m: Math.round(gap),
      streets: streets.slice(0, 12),
    };
  }

  batteryRoute(cost, src, dst, unconstrained, battery, rangeKm, dockDist, stations) {
    const g = this.g;
    const [nodes, docks] = this.swapDocks(stations);
    const full = rangeKm * 1000;
    const startRange = (full * battery) / 100, swapRange = full * SWAP_CHARGE;
    const reach = nodes.length ? g.dijkstra(g.length, nodes, { reverse: true }).dist : new Float64Array(g.n).fill(Infinity);
    const legs = this.plan(cost, src, dst, nodes, startRange, swapRange, reach);

    const edges = legs.flatMap((l) => l.edges);
    const { points, margin } = this.legProfile(legs, reach);
    const swaps = [];
    let done = 0;
    for (const leg of legs) {
      for (const e of leg.edges) done += g.length[e];
      if (leg.swapAt != null) {
        const d = docks[leg.swapAt];
        swaps.push({ name: d.name, lat: d.lat, lon: d.lon, at_m: Math.round(done), ebikes: d.ebikes });
      }
    }
    const [unsafeAt, emptyAt] = this.firstUnsafe(unconstrained, startRange, reach);

    const out = this.summarize(edges, dockDist);
    out.battery = {
      profile: points.map(([f, r]) => [Math.round(f * 1e5) / 1e5, Math.round((1000 * r) / full) / 10]),
      arrive_pct: Math.round((100 * points.at(-1)[1]) / full),
      swaps,
      min_margin_m: Math.round(margin),
      without: {
        unsafe_at_m: unsafeAt == null ? null : Math.round(unsafeAt),
        empty_at_m: emptyAt == null ? null : Math.round(emptyAt),
      },
    };
    return out;
  }

  // Shortest-path tree that never strands the rider: see safe_tree in backend/battery.py.
  safeTree(w, src, startRange, reach, side = null) {
    const g = this.g;
    const budget = new Float64Array(g.n);
    for (let v = 0; v < g.n; v++) budget[v] = startRange - reach[v] + TOL;
    if (budget[src] < 0) return null;
    let ratio = 1;
    if (w !== g.length) for (let e = 0; e < g.m; e++) ratio = Math.max(ratio, w[e] / Math.max(g.length[e], 1e-6));
    return g.dijkstra(w, [src], { limit: startRange * ratio + 1, budget, side });
  }

  // A* over {start, docks, destination}; see plan in backend/battery.py.
  plan(cost, src, dst, docks, startRange, swapRange, reach) {
    const g = this.g;
    if (startRange + TOL < reach[src]) {
      throw new RouteError(422, Number.isFinite(reach[src])
        ? `Not enough charge: the nearest dock is ${fmtDist(reach[src])} away but you have ${fmtDist(startRange)} of range.`
        : "No dock with a charged bike can be reached from here.");
    }
    const stops = [src, ...docks, dst], DST = stops.length - 1;
    const h = g.dijkstra(cost, [dst], { reverse: true }).dist;
    const best = new Float64Array(stops.length).fill(Infinity), parent = new Array(stops.length);
    const expanded = new Uint8Array(stops.length);
    const heap = new Heap(64);
    best[0] = 0;
    heap.push(h[src], 0);
    let count = 0;

    while (heap.size) {
      const i = heap.pop();
      if (expanded[i]) continue;
      expanded[i] = 1;
      if (i === DST) return this.legs(stops, parent, startRange, swapRange);
      if (++count > MAX_EXPANSIONS) break;

      const r = i === 0 ? startRange : swapRange;
      const exact = this.safeTree(g.length, stops[i], r, reach, cost);
      const nice = this.safeTree(cost, stops[i], r, reach);
      if (!exact) continue;
      for (let j = 1; j < stops.length; j++) {
        if (expanded[j] || j === i) continue;
        const node = stops[j];
        if (!Number.isFinite(exact.dist[node])) continue; // no safe way there on this charge
        const useNice = nice && Number.isFinite(nice.dist[node]);
        const legCost = useNice ? nice.dist[node] : exact.side[node];
        const g2 = best[i] + legCost + (j === DST ? 0 : SWAP_PENALTY);
        if (g2 < best[j]) {
          best[j] = g2;
          parent[j] = [i, useNice ? nice : exact];
          heap.push(g2 + h[node], j);
        }
      }
    }
    throw new RouteError(422, "No safe route: the destination is too far from docks with bikes on this charge.");
  }

  legs(stops, parent, startRange, swapRange) {
    const chain = [];
    for (let j = stops.length - 1; j !== 0; j = parent[j][0]) chain.push([parent[j][0], j, parent[j][1]]);
    return chain.reverse().map(([i, j, tree]) => ({
      edges: this.g.path(tree.pred, stops[i], stops[j]),
      startRange: i === 0 ? startRange : swapRange,
      swapAt: j === stops.length - 1 ? null : j - 1,
    }));
  }

  legProfile(legs, reach) {
    const g = this.g;
    let total = 0;
    for (const leg of legs) for (const e of leg.edges) total += g.length[e];
    total = total || 1;
    const points = [];
    let done = 0, margin = Infinity;
    for (const leg of legs) {
      let along = 0;
      if (leg.edges.length) margin = Math.min(margin, leg.startRange - reach[g.edge_u[leg.edges[0]]]);
      for (const e of leg.edges) {
        along += g.length[e];
        margin = Math.min(margin, leg.startRange - along - reach[g.edge_v[e]]);
      }
      points.push([done / total, leg.startRange]);
      done += along;
      points.push([done / total, leg.startRange - along]);
    }
    return { points, margin };
  }

  // Where an ordinary route breaks the rule: [metres to the point of no return, metres until empty].
  firstUnsafe(edges, startRange, reach) {
    const g = this.g;
    if (!edges.length) return [null, null];
    let along = 0, unsafe = null;
    if (reach[g.edge_u[edges[0]]] > startRange + TOL) unsafe = 0;
    for (const e of edges) {
      if (unsafe != null) break;
      along += g.length[e];
      if (along + reach[g.edge_v[e]] > startRange + TOL) unsafe = along;
    }
    let total = 0;
    for (const e of edges) total += g.length[e];
    return [unsafe, total > startRange ? startRange : null];
  }
}
