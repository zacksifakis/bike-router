// Runs frontend/router.js on cases sent as JSON on stdin and prints the results as JSON.
// Driven by tests/test_js_port.py, which runs the same cases through the Python backend.
import { readFileSync } from "node:fs";
import { Graph, Router } from "../../frontend/router.js";

const input = JSON.parse(readFileSync(0, "utf8"));
const results = input.cases.map((c) => {
  const a = c.graph;
  const graph = new Graph({
    node_lat: Float64Array.from(a.node_lat), node_lon: Float64Array.from(a.node_lon),
    edge_u: Int32Array.from(a.edge_u), edge_v: Int32Array.from(a.edge_v),
    length: Float64Array.from(a.length), bike: Float64Array.from(a.bike),
    green: Float64Array.from(a.green), lively: Float64Array.from(a.lively),
    mid_lat: Float64Array.from(a.mid_lat), mid_lon: Float64Array.from(a.mid_lon),
    coords: Float32Array.from(a.coords), offsets: Int32Array.from(a.offsets),
    name_idx: new Uint16Array(a.edge_u.length),
  }, [""]);
  const router = new Router(graph);
  const cost = Float64Array.from(c.cost);

  const preferred = graph.path(graph.dijkstra(cost, [c.src]).pred, c.src, c.dst);
  const reach = graph.dijkstra(graph.length, c.docks, { reverse: true }).dist;
  let legs = null, error = null;
  try {
    legs = router.plan(cost, c.src, c.dst, c.docks, c.start_range, c.swap_range, reach)
      .map((l) => ({ edges: l.edges, swap_at: l.swapAt }));
  } catch (e) {
    error = e.message;
  }
  return { preferred, legs, error };
});
console.log(JSON.stringify(results));
