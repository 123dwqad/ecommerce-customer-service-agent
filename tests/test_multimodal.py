"""Provider protocol tests using MockTransport; no external calls or API keys."""
import asyncio
import base64
import io
import json
import unittest
import wave
from unittest.mock import patch

import httpx
from PIL import Image

import test_commerce as base
from app.commerce.multimodal import MAX_IMAGE_REQUEST_BYTES, MediaError, read_bounded


def image_bytes(format="PNG"):
    output = io.BytesIO()
    image = Image.new("RGB", (16, 16), "green")
    exif = Image.Exif()
    exif[270] = "private metadata"
    image.save(output, format=format, exif=exif)
    return output.getvalue()


def audio_bytes(seconds=1, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x00\x00" * int(rate*seconds)*channels)
    return output.getvalue()


class MultimodalTests(unittest.TestCase):
    def setUp(self):
        base.CommerceTests.setUp(self)
        self.settings.speech_enabled = False
        self.settings.vision_enabled = False
        self.settings.speech_protocol = "multipart"
    tearDown = base.CommerceTests.tearDown
    account = base.CommerceTests.account

    def configure(self, handler):
        self.settings.speech_protocol = "multipart"
        for kind in ["speech", "vision"]:
            setattr(self.settings, f"{kind}_enabled", True)
            setattr(self.settings, f"{kind}_url", f"https://mock-provider.test/v1/{kind}")
            setattr(self.settings, f"{kind}_model", "mock-model")
            setattr(self.settings, f"{kind}_api_key", "secret-test-key")
        self.app.state.media.transport = httpx.MockTransport(handler)

    def vision(self, data=None, question="图片里有什么？", **kwargs):
        return self.client.post("/media/vision", headers=self.headers, json={
            "image_base64": base64.b64encode(data if data is not None else image_bytes()).decode(),
            "question": question}, **kwargs)

    def audio(self, data=None):
        return self.client.post("/media/transcribe", headers={**self.headers, "Content-Type": "audio/wav"},
            content=data if data is not None else audio_bytes())

    def test_disabled_modes_clear_error_and_no_key_leak(self):
        result = self.client.get("/media/options").json()
        self.assertFalse(result["speech"]["ready"])
        self.assertFalse(result["vision"]["ready"])
        self.assertEqual(self.vision().json()["code"], "media_not_configured")
        self.assertEqual(self.audio().status_code, 503)
        self.configure(lambda request: httpx.Response(200, json={"text": "test"}))
        options = self.client.get("/media/options").text
        self.assertNotIn("secret-test-key", options)
        self.assertNotIn("mock-provider", options)

    def test_media_routes_require_login(self):
        self.assertEqual(self.client.post("/media/transcribe", content=b"test").status_code, 401)
        self.assertEqual(self.client.post("/media/vision", json={}).status_code, 401)

    def test_dashscope_speech_protocol(self):
        def provider(request):
            payload = json.loads(request.content)
            audio = payload["input"]["messages"][0]["content"][0]
            self.assertEqual(audio["type"], "input_audio")
            self.assertEqual(base64.b64decode(audio["input_audio"]["data"].split(",", 1)[1]), audio_bytes())
            self.assertEqual(payload["parameters"], {"format": "wav", "sample_rate": "16000"})
            return httpx.Response(200, json={"output": {"text": "查询物流"}})
        self.configure(provider)
        self.settings.speech_protocol = "dashscope_asr"
        with patch.object(self.app.state.agent, "chat", side_effect=AssertionError("No auto execution")):
            result = self.audio()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["text"], "查询物流")
        self.assertTrue(result.json()["review_required"])

    def test_dashscope_invalid_response(self):
        self.configure(lambda request: httpx.Response(200, json={"output": []}))
        self.settings.speech_protocol = "dashscope_asr"
        self.assertEqual(self.audio().json()["code"], "provider_invalid_response")

    def test_speech_protocol_redaction_and_no_business_execution(self):
        requests = []
        def provider(request):
            requests.append(request)
            self.assertEqual(request.headers["Authorization"], "Bearer secret-test-key")
            self.assertIn("multipart/form-data", request.headers["Content-Type"])
            payload = request.read()
            self.assertIn(b'name="model"', payload)
            self.assertIn(b'filename="recording.wav"', payload)
            self.assertIn(b"RIFF", payload)
            return httpx.Response(200, json={"text": "取消订单，电话13800138000"})
        self.configure(provider)
        with patch.object(self.app.state.agent, "chat", side_effect=AssertionError("No auto execution")):
            result = self.audio()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["review_required"])
        self.assertNotIn("13800138000", result.text)
        self.assertEqual(len(requests), 1)

    def test_vision_protocol_normalizes_image_and_strips_metadata(self):
        def provider(request):
            payload = json.loads(request.read())
            self.assertEqual(payload["model"], "mock-model")
            self.assertEqual(len(payload["messages"]), 2)
            content = payload["messages"][1]["content"]
            self.assertNotIn("13800138000", content[0]["text"])
            self.assertEqual(content[1]["type"], "image_url")
            url = content[1]["image_url"]["url"]
            self.assertTrue(url.startswith("data:image/jpeg;base64,"))
            image = Image.open(io.BytesIO(base64.b64decode(url.split(",")[1])))
            self.assertFalse(image.getexif())
            self.assertNotIn("tools", payload)
            return httpx.Response(200, json={"choices": [{"message": {"content": "包装边角有折痕，无法确认内部是否损坏。"}}]})
        self.configure(provider)
        with patch.object(self.app.state.agent, "chat", side_effect=AssertionError("No auto execution")):
            response = self.vision(question="请看图片，电话13800138000")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["kind"], "image_analysis")
        self.assertIn("未查询订单", response.json()["notice"])

    def test_bad_image_format_and_invalid_base64(self):
        self.configure(lambda request: self.fail("Should not call provider"))
        self.assertEqual(self.vision(b"not-an-image").json()["code"], "invalid_image")
        self.assertEqual(self.vision(image_bytes("BMP")).status_code, 415)
        result = self.client.post("/media/vision", headers=self.headers, json={"image_base64": "!!!!"})
        self.assertEqual(result.json()["code"], "invalid_image")

    def test_image_body_oversize_and_remote_url_rejected(self):
        self.configure(lambda request: self.fail("Should not call provider"))
        response = self.client.post("/media/vision", headers={**self.headers, "Content-Type": "application/json",
            "Content-Length": str(MAX_IMAGE_REQUEST_BYTES+1)}, content=b"{}")
        self.assertEqual(response.status_code, 413)
        response = self.client.post("/media/vision", headers=self.headers,
            json={"image_base64": "AAAA", "image_url": "https://private.test/image"})
        self.assertEqual(response.status_code, 422)

    def test_large_dimensions_rejected_before_provider(self):
        self.configure(lambda request: self.fail("Should not call provider"))
        output = io.BytesIO()
        Image.new("RGB", (5000, 4100)).save(output, format="PNG")
        self.assertEqual(self.vision(output.getvalue()).status_code, 413)

    def test_audio_invalid_short_long_or_wrong_codec(self):
        self.configure(lambda request: self.fail("Should not call provider"))
        for raw in [b"not-wav", audio_bytes(.05), audio_bytes(61), audio_bytes(rate=8000)]:
            with self.subTest(size=len(raw)):
                self.assertIn(self.audio(raw).status_code, {400, 415})
        self.assertEqual(self.audio(audio_bytes()[:-100]).status_code, 400)

    def test_empty_or_malformed_provider_response(self):
        for response in [{}, {"text": ""}, {"text": ["invalid"]}]:
            self.configure(lambda request: httpx.Response(200, json=response))
            self.assertIn(self.audio().status_code, {422, 502})
        for response in [{}, {"choices": []}, {"choices": [{"message": {"content": ""}}]}]:
            self.configure(lambda request: httpx.Response(200, json=response))
            self.assertEqual(self.vision().status_code, 502)

    def test_provider_http_failures_are_sanitized_and_not_retried(self):
        for status, expected in [(401,503),(429,503),(500,502),(307,502)]:
            requests = []
            def provider(request):
                requests.append(request)
                return httpx.Response(status, text="secret-test-key and provider internals")
            self.configure(provider)
            result = self.audio()
            self.assertEqual(result.status_code, expected)
            self.assertNotIn("secret-test-key", result.text)
            self.assertNotIn("provider internals", result.text)
            self.assertEqual(len(requests), 1)

    def test_timeout_has_actionable_error(self):
        def provider(request):
            raise httpx.ReadTimeout("private-provider-url", request=request)
        self.configure(provider)
        self.assertEqual(self.audio().json()["code"], "provider_timeout")

    def test_concurrency_guard(self):
        self.configure(lambda request: self.fail("Should not call provider"))
        self.app.state.media.active = 4
        self.assertEqual(self.audio().status_code, 429)

    def test_non_https_endpoint_rejected(self):
        self.configure(lambda request: self.fail("Should not call provider"))
        self.settings.speech_url = "http://external-provider.test/audio"
        self.assertEqual(self.audio().json()["code"], "media_config_invalid")

    def test_stream_limit_without_content_length(self):
        class StreamingRequest:
            headers = {}
            async def stream(self):
                yield b"a"*6
                yield b"b"*6
        with self.assertRaises(MediaError) as caught:
            asyncio.run(read_bounded(StreamingRequest(), 10))
        self.assertEqual(caught.exception.status, 413)


if __name__ == "__main__":
    unittest.main()
