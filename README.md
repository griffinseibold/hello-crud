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
| `httpRoute.hostnames` | Gateway hostnames; empty list is the catch-all |
| `namespace.labels` | Labels the platform Gateway requires for route attachment |
| `storage.size` / `storage.storageClassName` | Persistent volume settings |
