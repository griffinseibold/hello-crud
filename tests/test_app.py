import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import app as hello_crud


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(
            self.temporary_directory.name,
            "hello-crud.db",
        )
        hello_crud.app.config.update(
            TESTING=True,
            DATABASE_PATH=self.database_path,
        )
        hello_crud.initialize_database()
        self.client = hello_crud.app.test_client()

    def tearDown(self):
        self.temporary_directory.cleanup()


class HelloCrudTestCase(DatabaseTestCase):
    def test_hello_and_health(self):
        self.assertEqual(
            self.client.get("/").get_json(),
            {"message": "Hello, world!", "resource": "/items"},
        )
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_crud_lifecycle(self):
        created = self.client.post("/items", json={"name": "first"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json(), {"id": 1, "name": "first"})

        self.assertEqual(
            self.client.get("/items").get_json(),
            [{"id": 1, "name": "first"}],
        )
        self.assertEqual(self.client.get("/items/1").status_code, 200)

        updated = self.client.put("/items/1", json={"name": "updated"})
        self.assertEqual(updated.get_json(), {"id": 1, "name": "updated"})

        self.assertEqual(self.client.delete("/items/1").status_code, 204)
        self.assertEqual(self.client.get("/items/1").status_code, 404)

    def test_rejects_invalid_input(self):
        response = self.client.post("/items", json={"name": "  "})
        self.assertEqual(response.status_code, 400)

    def test_items_survive_database_reinitialization(self):
        self.client.post("/items", json={"name": "persistent"})

        hello_crud.initialize_database()
        replacement_client = hello_crud.app.test_client()

        self.assertEqual(
            replacement_client.get("/items").get_json(),
            [{"id": 1, "name": "persistent"}],
        )


LLM_URL = "http://llama-server.llm.svc.cluster.local/v1"


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status


def http_error(code):
    return urllib.error.HTTPError(LLM_URL, code, "error", {}, None)


def chat_completion(content):
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return FakeResponse(json.dumps(body).encode())


class LanguageModelTestCase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        hello_crud.app.config["LLM_URL"] = LLM_URL
        patcher = mock.patch.object(hello_crud.urllib.request, "urlopen")
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        self.client.post("/items", json={"name": "teapot"})

    def tearDown(self):
        hello_crud.app.config["LLM_URL"] = ""
        super().tearDown()

    def test_health_url_strips_the_api_path(self):
        self.assertEqual(
            hello_crud.llm_health_url(LLM_URL),
            "http://llama-server.llm.svc.cluster.local/health",
        )
        self.assertEqual(
            hello_crud.llm_health_url("http://llm.example/v1/"),
            "http://llm.example/health",
        )
        self.assertEqual(
            hello_crud.llm_health_url("http://llm.example"),
            "http://llm.example/health",
        )

    def test_disabled_without_llm_url(self):
        hello_crud.app.config["LLM_URL"] = ""

        self.assertEqual(
            self.client.get("/llm").get_json(),
            {"enabled": False, "ready": False},
        )
        response = self.client.post("/items/1/describe")
        self.assertEqual(response.status_code, 501)
        self.assertIn("error", response.get_json())
        self.urlopen.assert_not_called()

    def test_status_ready(self):
        self.urlopen.return_value = FakeResponse(b'{"status": "ok"}')

        self.assertEqual(
            self.client.get("/llm").get_json(),
            {"enabled": True, "ready": True},
        )
        self.assertEqual(
            self.urlopen.call_args.args[0],
            "http://llama-server.llm.svc.cluster.local/health",
        )

    def test_status_not_ready_while_loading_or_stopped(self):
        for error in (http_error(503), urllib.error.URLError(ConnectionRefusedError())):
            with self.subTest(error=error):
                self.urlopen.side_effect = error
                self.assertEqual(
                    self.client.get("/llm").get_json(),
                    {"enabled": True, "ready": False},
                )

    def test_describe_returns_the_model_answer(self):
        self.urlopen.return_value = chat_completion(" A cheerful teapot. ")

        response = self.client.post("/items/1/describe")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"id": 1, "name": "teapot", "description": "A cheerful teapot."},
        )
        llm_request = self.urlopen.call_args.args[0]
        self.assertEqual(llm_request.full_url, LLM_URL + "/chat/completions")
        sent = json.loads(llm_request.data)
        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        self.assertLessEqual(sent["max_tokens"], 128)
        self.assertIn("teapot", sent["messages"][-1]["content"])
        self.assertIsNotNone(self.urlopen.call_args.kwargs.get("timeout"))

        # The description is not stored on the item.
        self.assertEqual(
            self.client.get("/items/1").get_json(),
            {"id": 1, "name": "teapot"},
        )

    def test_describe_unknown_item(self):
        response = self.client.post("/items/99/describe")

        self.assertEqual(response.status_code, 404)
        self.urlopen.assert_not_called()

    def test_describe_while_the_model_is_starting(self):
        errors = (
            http_error(503),
            urllib.error.URLError(ConnectionRefusedError()),
            urllib.error.URLError(TimeoutError()),
            TimeoutError(),
        )
        for error in errors:
            with self.subTest(error=error):
                self.urlopen.side_effect = error

                response = self.client.post("/items/1/describe")

                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.headers["Retry-After"],
                    str(hello_crud.LLM_RETRY_AFTER_SECONDS),
                )
                self.assertIn("starting", response.get_json()["error"])

    def test_describe_other_upstream_failures(self):
        failures = (
            {"side_effect": http_error(500)},
            {"return_value": FakeResponse(b"not json")},
            {"return_value": FakeResponse(b'{"choices": []}')},
            {"return_value": chat_completion("   ")},
        )
        for failure in failures:
            with self.subTest(failure=failure):
                self.urlopen.reset_mock(return_value=True, side_effect=True)
                self.urlopen.configure_mock(**failure)

                response = self.client.post("/items/1/describe")

                self.assertEqual(response.status_code, 502)
                self.assertNotIn("Retry-After", response.headers)


if __name__ == "__main__":
    unittest.main()
