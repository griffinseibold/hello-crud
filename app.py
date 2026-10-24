import json
import os
import sqlite3
import urllib.error
import urllib.request

from flask import Flask, g, jsonify, request


app = Flask(__name__)
app.config["DATABASE_PATH"] = os.environ.get(
    "DATABASE_PATH",
    "/tmp/hello-crud.db",
)
# OpenAI-compatible base URL of the homelab's language model, such as
# http://llama-server.llm.svc.cluster.local/v1. Empty turns the feature off.
app.config["LLM_URL"] = os.environ.get("LLM_URL", "")

LLM_HEALTH_TIMEOUT_SECONDS = 2
LLM_REQUEST_TIMEOUT_SECONDS = 30
# The model can take a couple of minutes to load after it is requested.
LLM_RETRY_AFTER_SECONDS = 30


def get_database():
    if "database" not in g:
        g.database = sqlite3.connect(app.config["DATABASE_PATH"])
        g.database.row_factory = sqlite3.Row

    return g.database


@app.teardown_appcontext
def close_database(_exception=None):
    database = g.pop("database", None)
    if database is not None:
        database.close()


def initialize_database():
    with sqlite3.connect(app.config["DATABASE_PATH"]) as database:
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            )
            """
        )


initialize_database()


def read_name():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    return name.strip()


class LanguageModelStarting(Exception):
    """The model is not accepting requests yet; retrying later should work."""


class LanguageModelError(Exception):
    """The model answered, but not with something usable."""


def llm_health_url(llm_url):
    base_url = llm_url.rstrip("/").removesuffix("/v1")
    return base_url + "/health"


def language_model_ready():
    try:
        with urllib.request.urlopen(
            llm_health_url(app.config["LLM_URL"]),
            timeout=LLM_HEALTH_TIMEOUT_SECONDS,
        ) as response:
            return response.status == 200
    except OSError:
        # Connection refused, timeouts, and HTTP errors such as the 503 the
        # server returns while loading all mean "not ready".
        return False


def ask_language_model(prompt):
    body = {
        "messages": [
            {
                "role": "system",
                "content": "Reply with exactly one short, friendly sentence.",
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 64,
        # Qwen3 otherwise reasons at length before answering.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    llm_request = urllib.request.Request(
        app.config["LLM_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            llm_request,
            timeout=LLM_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 503:
            raise LanguageModelStarting() from error
        raise LanguageModelError(f"model returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, (ConnectionError, TimeoutError)):
            raise LanguageModelStarting() from error
        raise LanguageModelError(f"model unreachable: {error.reason}") from error
    except (ConnectionError, TimeoutError) as error:
        raise LanguageModelStarting() from error
    except ValueError as error:
        raise LanguageModelError("model returned invalid JSON") from error

    try:
        content = payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise LanguageModelError("model returned an unexpected response") from error
    if not content:
        raise LanguageModelError("model returned an empty response")

    return content


@app.get("/")
def hello():
    return jsonify(message="Hello, world!", resource="/items")


@app.get("/healthz")
def health():
    return jsonify(status="ok")


@app.get("/items")
def list_items():
    rows = get_database().execute(
        "SELECT id, name FROM items ORDER BY id"
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/items")
def create_item():
    name = read_name()
    if name is None:
        return jsonify(error="name must be a non-empty string"), 400

    database = get_database()
    cursor = database.execute("INSERT INTO items (name) VALUES (?)", (name,))
    database.commit()
    item = {"id": cursor.lastrowid, "name": name}
    return jsonify(item), 201


@app.get("/items/<int:item_id>")
def get_item(item_id):
    row = get_database().execute(
        "SELECT id, name FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        return jsonify(error="item not found"), 404
    return jsonify(dict(row))


@app.put("/items/<int:item_id>")
def update_item(item_id):
    name = read_name()
    if name is None:
        return jsonify(error="name must be a non-empty string"), 400

    database = get_database()
    cursor = database.execute(
        "UPDATE items SET name = ? WHERE id = ?",
        (name, item_id),
    )
    database.commit()
    if cursor.rowcount == 0:
        return jsonify(error="item not found"), 404

    return jsonify(id=item_id, name=name)


@app.delete("/items/<int:item_id>")
def delete_item(item_id):
    database = get_database()
    cursor = database.execute("DELETE FROM items WHERE id = ?", (item_id,))
    database.commit()
    if cursor.rowcount == 0:
        return jsonify(error="item not found"), 404
    return "", 204


@app.get("/llm")
def llm_status():
    if not app.config["LLM_URL"]:
        return jsonify(enabled=False, ready=False)
    return jsonify(enabled=True, ready=language_model_ready())


@app.post("/items/<int:item_id>/describe")
def describe_item(item_id):
    if not app.config["LLM_URL"]:
        return jsonify(error="the language model feature is not enabled"), 501

    row = get_database().execute(
        "SELECT id, name FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        return jsonify(error="item not found"), 404

    try:
        description = ask_language_model(
            f"Describe this item in one friendly sentence: {row['name']}"
        )
    except LanguageModelStarting:
        response = jsonify(
            error="the language model is starting; try again shortly"
        )
        response.headers["Retry-After"] = str(LLM_RETRY_AFTER_SECONDS)
        return response, 503
    except LanguageModelError as error:
        return jsonify(error=str(error)), 502

    return jsonify(id=row["id"], name=row["name"], description=description)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
