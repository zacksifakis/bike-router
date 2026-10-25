# Serves the API and map. Expects data/ to be built already (python scripts/build_graph.py).
FROM python:3.11-slim

RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY --chown=user backend backend
COPY --chown=user frontend frontend
COPY --chown=user data/graph.npz data/edge_names.json data/

USER user
ENV PORT=7860
EXPOSE 7860
CMD uvicorn backend.app:app --host 0.0.0.0 --port $PORT
