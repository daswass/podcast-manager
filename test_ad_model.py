import inspect
import unittest
from unittest.mock import patch

from mlx_lm.generate import generate_step, stream_generate as real_stream_generate

import ad_model
from ad_detect import ModelReply
from ad_model import OPTIONAL_SNAPSHOT_FILES, complete_ad_prompt, local_ad_model_snapshot


class LocalAdModelSnapshotTest(unittest.TestCase):
    def test_offline_snapshot_ignores_hub_metadata(self):
        with patch("ad_model.snapshot_download", return_value="/models/qwen") as download:
            path = local_ad_model_snapshot()

        self.assertEqual(path, "/models/qwen")
        self.assertEqual(download.call_args.args, ("mlx-community/Qwen2.5-7B-Instruct-4bit",))
        self.assertTrue(download.call_args.kwargs["local_files_only"])
        ignored = download.call_args.kwargs["ignore_patterns"]
        self.assertIn("README.md", ignored)
        self.assertIn(".gitattributes", ignored)
        self.assertEqual(ignored, OPTIONAL_SNAPSHOT_FILES)


class _TokenIds:
    def __init__(self, values):
        self.values = values

    def __getitem__(self, token_id):
        return self.values[token_id]


class _Tokenizer:
    _IDS = {
        "paid_ad": [10],
        " paid_ad": [20],
        "house_promo": [11],
        " house_promo": [21],
        "editorial": [12],
        " editorial": [22],
        "credits_or_outro": [13],
        " credits_or_outro": [23],
    }

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        return messages[0]["content"]

    def encode(self, text, add_special_tokens=False):
        return self._IDS[text]


class _Chunk:
    def __init__(self, text, values):
        self.text = text
        self.logprobs = _TokenIds(values)


class CompletionConfidenceTest(unittest.TestCase):
    def tearDown(self):
        ad_model._model = None
        ad_model._tokenizer = None

    def test_completion_reads_the_label_token_probability(self):
        flat = {token_id: -5.0 for token_id in (10, 11, 12, 13, 20, 21, 22, 23)}
        sure = dict(flat)
        sure[10] = -0.1
        chunks = [
            _Chunk('{"classification": ', flat),
            _Chunk('"', flat),
            _Chunk("paid", sure),
            _Chunk('_ad"}', flat),
        ]

        def stream_generate(*args, **kwargs):
            # stream_generate accepts **kwargs and forwards them to generate_step,
            # which rejects verbose. Bind the forwarded keywords here.
            own = inspect.signature(real_stream_generate).parameters
            unknown = set(kwargs) - set(own)
            self.assertEqual(unknown, set(), unknown)
            inspect.signature(generate_step).bind(
                object(), object(), max_tokens=kwargs["max_tokens"]
            )
            return iter(chunks)

        with patch("mlx_lm.load", return_value=(object(), _Tokenizer())), patch(
            "mlx_lm.generate.stream_generate",
            side_effect=stream_generate,
        ):
            reply = complete_ad_prompt("classify this")

        self.assertIsInstance(reply, ModelReply)
        self.assertIn("paid_ad", reply.text)
        self.assertGreater(reply.confidence, 0.8)
