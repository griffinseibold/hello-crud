# hello-crud

A minimal Flask CRUD API backed by SQLite on a persistent volume. This is the
sample application for the [homelab](https://github.com/griffinseibold/Homelab)
Kubernetes platform, and it is fully self-contained: source, tests, container
build, Helm chart, and CI all live in this repository.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | Greeting and a pointer to `/items` |
| GET | `/healthz` | Health check |
| GET | `/items` | List items |
| POST | `/items` | Create an item from `{"name": "..."}` |
| GET | `/items/<id>` | Fetch one item |
| PUT | `/items/<id>` | Rename an item |
| DELETE | `/items/<id>` | Delete an item |
| GET | `/llm` | Whether the language model is enabled and ready |
| POST | `/items/<id>/describe` | One-sentence description of an item from the language model |

Items are stored in SQLite at `DATABASE_PATH` (default `/tmp/hello-crud.db`;
the chart sets `/data/hello-crud.db` on a persistent volume).

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --requirement requirements.txt
python -m unittest discover -s tests -t .
flask run --port 8080
```

## Releases

Pushing a tag like `v0.3.0` runs the tests, lints the chart, and publishes
`ghcr.io/griffinseibold/hello-crud:0.3.0` (and `:latest`) through GitHub
Actions. No local docker build is required.

```bash
git tag v0.3.0
git push origin v0.3.0
```

## Deployment

`chart/hello-crud` is a standard Helm chart deployable to any Kubernetes
cluster. On the homelab platform it is registered through Argo CD, which
watches this repository and syncs the chart; `replicaCount` can be overridden
from the Argo CD UI to scale the application between 0 and 1.

Because SQLite allows a single writer on a ReadWriteOnce volume, running more
than one replica is not supported.

Notable values (see `chart/hello-crud/values.yaml` for all of them):

| Value | Purpose |
| --- | --- |
| `replicaCount` | 0 stops the app, 1 runs it |
| `image.tag` | Released application version |
| `httpRoute.domain` | Route hostname becomes `hello-crud.<domain>`; empty means catch-all |
| `httpRoute.hostnames` | Explicit hostnames, overriding the derived one |
| `namespace.labels` | Labels the platform Gateway requires for route attachment |
| `storage.size` / `storage.storageClassName` | Persistent volume settings |
| `llm.enabled` | Request the homelab's language model and enable the `/llm` endpoints (default `false`) |
| `llm.url` | OpenAI-compatible base URL of the language model |

## Optional: the homelab's language model

The homelab runs a language model (Qwen3-8B behind an OpenAI-compatible API)
only while some application asks for it. An application asks by carrying the
label `homelab.local/llm: requested` on its Deployment. The request counts
while the Deployment has at least one replica, and the homelab stops the model
a few minutes after nothing running requests it.

hello-crud shows both sides of this. By default it does not carry the label,
so the sample app never keeps the GPU busy. Setting `llm.enabled=true` (as a
Helm value, or as a parameter on the Argo CD application) adds the label and
passes `llm.url` to the app as `LLM_URL`:

```bash
helm upgrade --install hello-crud chart/hello-crud --set llm.enabled=true
```

With it on, two endpoints use the model:

```bash
curl http://hello-crud.localhost/llm
# {"enabled": true, "ready": true}

curl -X POST http://hello-crud.localhost/items/1/describe
# {"id": 1, "name": "teapot", "description": "A cheerful little teapot ..."}
```

`/llm` reports `ready: false` until the model's health check answers. The
description is generated fresh each time and is not saved.

The model takes a couple of minutes to load after it is first requested, so
right after the app starts `/items/<id>/describe` answers `503` with a
`Retry-After` header and `{"error": "the language model is starting; try
again shortly"}`. Retry after that many seconds. Other responses:

| Status | Meaning |
| --- | --- |
| `200` | Description generated |
| `404` | No item with that id |
| `501` | The feature is off (`LLM_URL` is not set) |
| `502` | The model answered with an error or something unusable |
| `503` | The model is still starting; see `Retry-After` |

To run it locally, point `LLM_URL` at any OpenAI-compatible server:

```bash
LLM_URL=http://localhost:8000/v1 flask run --port 8080
```
