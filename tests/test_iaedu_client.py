"""
Offline unit tests for iaedu_client.

HTTP is mocked and real socket connections are blocked for the whole run,
so a test that accidentally reached the network fails instead of passing.

Run from the repository root:

    python -m unittest discover -v
"""

from __future__ import annotations

import io
import socket
import unittest
from unittest import mock

import requests.adapters as requests_adapters
import requests.models as requests_models
import requests.structures as requests_structures

import iaedu_client
from iaedu_client import IaeduClient, IaeduError

TEST_KEY = "test-key-not-real"


def _no_network(*args, **kwargs):
    raise AssertionError("Network access attempted during offline tests")


def setUpModule():
    global _socket_patch
    _socket_patch = mock.patch.object(socket.socket, "connect", _no_network)
    _socket_patch.start()


def tearDownModule():
    _socket_patch.stop()


def _fake_response(lines, status=200, headers=None):
    """Mock of requests.Response with a controlled iter_lines()."""
    resp = mock.MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.iter_lines.return_value = iter(lines)
    return resp


class NetworkGuardTests(unittest.TestCase):
    def test_guard_blocks_real_connections(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(AssertionError):
                sock.connect(("192.0.2.1", 443))  # TEST-NET-1, never routed
        finally:
            sock.close()


class BuildRequestTests(unittest.TestCase):
    def test_header_url_and_multipart_fields(self):
        client = IaeduClient(api_key=TEST_KEY, base_url="https://example.test/api/v1/")
        fake = _fake_response(['{"type": "token", "content": "OK"}'])

        with mock.patch.object(iaedu_client.requests, "post", return_value=fake) as post:
            result = client.chat(
                agent_id="agent-123",
                channel_id="chan-456",
                message="Olá",
                thread_id="thr-fixed",
            )

        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://example.test/api/v1/agent/agent-123/stream")
        self.assertEqual(kwargs["headers"]["x-api-key"], TEST_KEY)
        self.assertTrue(kwargs["stream"])
        self.assertIs(kwargs["allow_redirects"], False)

        fields = dict(kwargs["files"])
        self.assertEqual(fields["channel_id"], (None, "chan-456"))
        self.assertEqual(fields["thread_id"], (None, "thr-fixed"))
        self.assertEqual(fields["message"], (None, "Olá"))
        self.assertEqual(fields["user_info"], (None, "{}"))
        self.assertNotIn("user_id", fields)
        self.assertNotIn("image", fields)
        self.assertEqual(result["thread_id"], "thr-fixed")
        fake.close.assert_called_once()

    def test_optional_fields_included_when_given(self):
        client = IaeduClient(api_key=TEST_KEY)
        fake = _fake_response(['{"type": "token", "content": "OK"}'])

        with mock.patch.object(iaedu_client.requests, "post", return_value=fake) as post:
            client.chat(
                agent_id="a",
                channel_id="c",
                message="m",
                user_id="user-9",
                user_context="lang=pt",
                user_info={"nome": "Ana"},
                image=mock.sentinel.image_file,
            )

        fields = dict(post.call_args.kwargs["files"])
        self.assertEqual(fields["user_id"][1], "user-9")
        self.assertEqual(fields["user_context"][1], "lang=pt")
        self.assertEqual(fields["user_info"][1], '{"nome": "Ana"}')
        self.assertEqual(fields["image"], ("image", mock.sentinel.image_file))

    def test_thread_id_generated_when_absent(self):
        client = IaeduClient(api_key=TEST_KEY)
        fake = _fake_response(['{"type": "token", "content": "OK"}'])

        with mock.patch.object(iaedu_client.requests, "post", return_value=fake) as post:
            result = client.chat(agent_id="a", channel_id="c", message="m")

        sent = dict(post.call_args.kwargs["files"])["thread_id"][1]
        self.assertTrue(sent.startswith("thr-"))
        self.assertEqual(result["thread_id"], sent)

    def test_missing_ids_raise(self):
        client = IaeduClient(api_key=TEST_KEY)
        with self.assertRaises(IaeduError):
            client.chat(agent_id="", channel_id="c", message="m")
        with self.assertRaises(IaeduError):
            client.chat(agent_id="a", channel_id="", message="m")

    def test_network_error_wrapped(self):
        client = IaeduClient(api_key=TEST_KEY)
        boom = iaedu_client.requests.ConnectionError("unreachable")
        with mock.patch.object(iaedu_client.requests, "post", side_effect=boom):
            with self.assertRaises(IaeduError):
                client.chat(agent_id="a", channel_id="c", message="m")


class StreamParsingTests(unittest.TestCase):
    def setUp(self):
        self.client = IaeduClient(api_key=TEST_KEY)

    def _chat(self, lines, status=200, headers=None):
        fake = _fake_response(lines, status=status, headers=headers)
        with mock.patch.object(iaedu_client.requests, "post", return_value=fake):
            return self.client.chat(agent_id="a", channel_id="c", message="m")

    def test_envelope_format(self):
        lines = [
            '{"run_id": "r1", "type": "start", "content": "Processing"}',
            '{"run_id": "r1", "type": "token", "content": "O"}',
            '{"run_id": "r1", "type": "token", "content": "K"}',
            '{"run_id": "r1", "type": "message", "content": {"type": "ai", '
            '"content": "OK", "response_metadata": {"model_name": "example-model", '
            '"model_provider": "example-provider", "stop_reason": "end_turn"}}}',
            '{"run_id": "r1", "type": "done", "content": "r1", "messageId": "msg-1"}',
        ]
        result = self._chat(lines)
        self.assertEqual(result["text"], "OK")
        self.assertEqual(result["model"], "example-model")
        self.assertEqual(result["provider"], "example-provider")
        self.assertEqual(result["stop_reason"], "end_turn")
        self.assertEqual(result["message_id"], "msg-1")
        self.assertIsNone(result["error"])
        self.assertEqual(result["raw_chunks"], lines)

    def test_token_spaces_preserved(self):
        lines = [
            '{"type": "token", "content": "Bom"}',
            '{"type": "token", "content": " dia"}',
        ]
        self.assertEqual(self._chat(lines)["text"], "Bom dia")

    def test_falls_back_to_message_without_tokens(self):
        lines = [
            '{"type": "start", "content": "Processing"}',
            '{"type": "message", "content": {"content": "Resposta final"}}',
            '{"type": "done", "content": "x"}',
        ]
        self.assertEqual(self._chat(lines)["text"], "Resposta final")

    def test_error_event_reported(self):
        lines = ['{"type": "error", "content": "agent not found"}']
        result = self._chat(lines, status=404)
        self.assertEqual(result["error"], "agent not found")
        self.assertEqual(result["status"], 404)
        self.assertEqual(result["text"], "")

    def test_blank_lines_dropped(self):
        lines = ["", "   ", '{"type": "token", "content": "x"}']
        result = self._chat(lines)
        self.assertEqual(result["raw_chunks"], ['{"type": "token", "content": "x"}'])

    def test_sse_plain_text(self):
        result = self._chat(["data: Olá", "data:  mundo", "data: [DONE]"])
        self.assertEqual(result["text"], "Olá mundo")

    def test_sse_openai_style_delta(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"Bom "}}]}',
            'data: {"choices":[{"delta":{"content":"dia"}}]}',
            "data: [DONE]",
        ]
        self.assertEqual(self._chat(lines)["text"], "Bom dia")

    def test_sse_control_lines_ignored(self):
        lines = ["event: message", "id: 1", "data: conteúdo", ": comment", "data: [DONE]"]
        self.assertEqual(self._chat(lines)["text"], "conteúdo")

    def test_plain_json_lines(self):
        self.assertEqual(self._chat(['{"text": "um "}', '{"text": "dois"}'])["text"], "um dois")

    def test_raw_text_fallback(self):
        self.assertEqual(self._chat(["linha um ", "linha dois"])["text"], "linha um linha dois")

    def test_stream_cut_midway(self):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {}

        def lines():
            yield '{"type": "token", "content": "par"}'
            raise iaedu_client.requests.ConnectionError("reset")

        resp.iter_lines.return_value = lines()
        with mock.patch.object(iaedu_client.requests, "post", return_value=resp):
            result = self.client.chat(agent_id="a", channel_id="c", message="m")
        self.assertEqual(result["text"], "par")
        self.assertTrue(result["raw_chunks"][-1].startswith("[stream-error]"))
        self.assertIn("stream-error", result["error"])
        resp.close.assert_called_once()


class _RecordingTransport:
    """
    Replaces HTTPAdapter.send: records every request that would go on the wire
    and answers the first one with a redirect to another host over plain HTTP.
    Runs the real requests Session logic, so redirect handling is exercised
    for real, without any socket.
    """

    LOCATION = "http://attacker.example.test/collect"

    def __init__(self):
        self.sent = []

    # Installed as a plain (non-descriptor) class attribute, so it is called
    # without the adapter instance.
    def __call__(self, request, **kwargs):
        self.sent.append((request.url, dict(request.headers)))
        resp = requests_models.Response()
        resp.request = request
        resp.url = request.url
        resp.raw = io.BytesIO(b"")
        if len(self.sent) == 1:
            resp.status_code = 302
            resp.headers = requests_structures.CaseInsensitiveDict({"Location": self.LOCATION})
        else:
            resp.status_code = 200
            resp.headers = requests_structures.CaseInsensitiveDict()
        return resp


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.transport = _RecordingTransport()
        patcher = mock.patch.object(requests_adapters.HTTPAdapter, "send", self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_redirect_to_other_host_does_not_resend_key(self):
        client = IaeduClient(api_key=TEST_KEY, base_url="https://api.example.test/v1")
        with self.assertRaises(IaeduError) as ctx:
            client.chat(agent_id="a", channel_id="c", message="m")

        # Exactly one request went out, to the configured host only.
        self.assertEqual(len(self.transport.sent), 1)
        url, headers = self.transport.sent[0]
        self.assertTrue(url.startswith("https://api.example.test/"))
        # Nothing reached the redirect target, with or without the key.
        self.assertFalse(any("attacker" in u for u, _ in self.transport.sent))
        # The error names the target host but never contains the key.
        self.assertIn("attacker.example.test", str(ctx.exception))
        self.assertNotIn(TEST_KEY, str(ctx.exception))

    def test_harness_detects_key_forwarding_when_redirects_are_followed(self):
        # Control: the same harness with requests' default behaviour shows the
        # leak this client prevents. If this ever stops forwarding the header,
        # the test above no longer proves anything and should be revisited.
        iaedu_client.requests.post(
            "https://api.example.test/v1/agent/a/stream",
            headers={"x-api-key": TEST_KEY},
            allow_redirects=True,
        )
        self.assertEqual(len(self.transport.sent), 2)
        url, headers = self.transport.sent[1]
        self.assertEqual(url, _RecordingTransport.LOCATION)
        self.assertEqual(headers.get("x-api-key"), TEST_KEY)

    def test_any_3xx_is_an_error(self):
        client = IaeduClient(api_key=TEST_KEY)
        for status in (301, 302, 303, 307, 308):
            fake = _fake_response([], status=status, headers={"Location": "https://other.example.test/"})
            with mock.patch.object(iaedu_client.requests, "post", return_value=fake):
                with self.assertRaises(IaeduError):
                    client.chat(agent_id="a", channel_id="c", message="m")
            fake.close.assert_called_once()


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.client = IaeduClient(api_key=TEST_KEY)

    def _chat(self, status=200, headers=None):
        fake = _fake_response(['{"type": "token", "content": "x"}'], status=status, headers=headers)
        with mock.patch.object(iaedu_client.requests, "post", return_value=fake):
            return self.client.chat(agent_id="a", channel_id="c", message="m")

    def test_http_429(self):
        result = self._chat(status=429)
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["status"], 429)

    def test_remaining_zero(self):
        self.assertTrue(self._chat(headers={"X-RateLimit-Remaining": "0"})["rate_limited"])

    def test_retry_after(self):
        self.assertTrue(self._chat(headers={"Retry-After": "5"})["rate_limited"])

    def test_non_numeric_header_ignored(self):
        self.assertFalse(self._chat(headers={"RateLimit-Remaining": "n/a"})["rate_limited"])

    def test_normal(self):
        self.assertFalse(self._chat(headers={"X-RateLimit-Remaining": "99"})["rate_limited"])


class ConfigTests(unittest.TestCase):
    def test_missing_key_raises(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(IaeduError):
                IaeduClient()

    def test_key_and_url_from_env(self):
        env = {"IAEDU_API_KEY": "env-key", "IAEDU_BASE_URL": "https://alt.example.test/v1"}
        with mock.patch.dict("os.environ", env, clear=True):
            client = IaeduClient()
        self.assertEqual(client._api_key, "env-key")
        self.assertEqual(client.base_url, "https://alt.example.test/v1")

    def test_default_base_url(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            client = IaeduClient(api_key=TEST_KEY)
        self.assertEqual(client.base_url, "https://api.iaedu.pt/agent-chat/api/v1")

    def test_plain_http_refused(self):
        with self.assertRaises(IaeduError):
            IaeduClient(api_key=TEST_KEY, base_url="http://api.example.test")

    def test_repr_hides_key(self):
        self.assertNotIn(TEST_KEY, repr(IaeduClient(api_key=TEST_KEY)))

    def test_key_not_a_public_attribute(self):
        self.assertFalse(hasattr(IaeduClient(api_key=TEST_KEY), "api_key"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
