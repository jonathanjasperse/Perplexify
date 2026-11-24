"""Dictation: the recording the page sends, the endpoints, and the engine on real speech when the model is on this computer."""
import struct
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from helpers import TempHome

from perplexify import dictation


def pcm(seconds=1.0, value=0.1):
    return (np.ones(int(16000 * seconds), dtype="<f4") * value).tobytes()


class Recording(unittest.TestCase):
    def test_float32_samples_are_read_and_limited(self):
        x = dictation.samples_from_bytes(pcm(1.0, 0.5))
        self.assertEqual(len(x), 16000)
        self.assertAlmostEqual(float(x[0]), 0.5)
        big = dictation.samples_from_bytes(np.array([5.0, -5.0], dtype="<f4").tobytes())
        self.assertEqual(list(big), [1.0, -1.0])                       # clipped, never louder than full scale

    def test_a_damaged_or_endless_recording_is_refused(self):
        for bad in (b"", b"abc", b"\x00" * 7):
            with self.assertRaises(dictation.DictationError):
                dictation.samples_from_bytes(bad)
        with self.assertRaises(dictation.DictationError):
            dictation.samples_from_bytes(np.array([float("nan")] * 4, dtype="<f4").tobytes())
        with self.assertRaises(dictation.DictationError):
            dictation.samples_from_bytes(b"\x00" * 4 * 16000 * (dictation.MAX_SECONDS + 1))

    def test_a_blink_of_sound_is_not_sent_to_the_model(self):
        with mock.patch.object(dictation, "available", return_value=True):
            self.assertEqual(dictation.transcribe(np.zeros(100, dtype=np.float32)), "")


class Endpoints(TempHome):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from perplexify.context import AppContext
        from perplexify.server import create_app

        cls.ctx = AppContext()
        cls.client = TestClient(create_app(cls.ctx), base_url="http://127.0.0.1")

    def test_status_says_what_is_installed(self):
        st = self.client.get("/api/dictation").json()
        self.assertEqual(set(st), {"available", "model_ready", "model_mb", "max_seconds", "sample_rate"})
        self.assertEqual(st["sample_rate"], 16000)

    def test_words_come_back_and_the_language_and_hint_are_passed_on(self):
        with mock.patch.object(dictation, "model_ready", return_value=True), \
                mock.patch.object(dictation, "transcribe", return_value="hello there") as t:
            r = self.client.post("/api/dictation/transcribe?language=en&hint=JUCE", content=pcm(1.0))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"text": "hello there"})
        samples, language, hint = t.call_args.args
        self.assertEqual((len(samples), language, hint), (16000, "en", "JUCE"))

    def test_without_the_model_the_answer_says_so_and_a_bad_recording_is_a_400(self):
        with mock.patch.object(dictation, "model_ready", return_value=False):
            self.assertEqual(self.client.post("/api/dictation/transcribe", content=pcm()).status_code, 409)
        with mock.patch.object(dictation, "model_ready", return_value=True):
            r = self.client.post("/api/dictation/transcribe", content=b"abc")
        self.assertEqual(r.status_code, 400)

    def test_cross_site_pages_cannot_use_the_microphone_endpoint(self):
        r = self.client.post("/api/dictation/transcribe", content=pcm(), headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_preparing_downloads_the_model_as_a_job_only_when_it_is_missing(self):
        with mock.patch.object(dictation, "available", return_value=True), mock.patch.object(dictation, "model_ready", return_value=True):
            self.assertEqual(self.client.post("/api/dictation/prepare").json(), {"ready": True})
        calls = []
        with mock.patch.object(dictation, "available", return_value=True), mock.patch.object(dictation, "model_ready", return_value=False), \
                mock.patch.object(dictation, "download_model", lambda progress=None, **k: calls.append(1)):
            r = self.client.post("/api/dictation/prepare").json()
            self.assertFalse(r["ready"])
            self.assertEqual(r["job"]["kind"], "speech-model")
            import time
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.1)
        self.assertEqual(calls, [1])


class RealSpeech(TempHome):
    """The engine on a spoken sentence. Needs the model on this computer (the installer ships it; run
    `python -m perplexify download-model --whisper` once from the source), so it is skipped without it."""

    SAMPLE = Path(__file__).resolve().parent / "speech_sample.wav"

    def test_a_spoken_question_is_written_down(self):
        import os

        if not dictation.available():
            self.skipTest("faster-whisper is not installed")
        # the model of this user's data folder, or the one the build staged
        folder = None
        for root in (Path(os.environ.get("PERPLEXIFY_TEST_MODELS", "")), Path(os.environ.get("LOCALAPPDATA", "")) / "Perplexify" / "models", Path(__file__).resolve().parent.parent / "installer" / "build" / "stage" / "models"):
            if (root / dictation.MODEL_NAME / "model.bin").is_file():
                folder = root / dictation.MODEL_NAME
                break
        if folder is None:
            self.skipTest("the speech model is not on this computer")
        patch = mock.patch.object(dictation, "model_dir", lambda: folder)
        patch.start()
        self.addCleanup(patch.stop)
        if not self.SAMPLE.is_file():
            self.skipTest("no speech sample")
        w = wave.open(str(self.SAMPLE))
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / w.getframerate())), np.arange(len(x)), x).astype(np.float32)
        self.assertTrue(dictation.model_ready())
        text = dictation.transcribe(x, "en").lower()
        for word in ("connect", "function", "mixer", "repository"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
