// Runs the router off the main thread so the map stays responsive while routes are planned.
import { loadGraph, Router } from "./router.js";

const ready = loadGraph(new URL("data/", import.meta.url).href).then((g) => new Router(g));

onmessage = async ({ data }) => {
  try {
    const router = await ready;
    postMessage({ id: data.id, ok: true, body: router.route(data.params, data.stations) });
  } catch (e) {
    postMessage({ id: data.id, ok: false, status: e.status || 500, detail: e.message || "Routing failed." });
  }
};
