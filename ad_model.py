from huggingface_hub import snapshot_download

from ad_detect import ModelReply, _CLASSIFICATION_OPEN, paid_ad_confidence_from_chunks

AD_MODEL = "mlx-community/Qwen2.5-7B-Instruct-4bit"

# Hub checkout metadata. The ad model runs from the weights and tokenizer, so a
# snapshot that is only missing these files is still usable offline.
OPTIONAL_SNAPSHOT_FILES = [".gitattributes", "README.md", "LICENSE", "LICENSE.txt"]

_model = None
_tokenizer = None


def local_ad_model_snapshot():
    # The nightly worker must not lose ad removal because a model already
    # cached on disk attempts an unnecessary Hub request (which can fail
    # behind an authenticated proxy). Resolve the exact local snapshot
    # first, and fail clearly when the weights have not been preloaded.
    return snapshot_download(
        AD_MODEL,
        local_files_only=True,
        ignore_patterns=OPTIONAL_SNAPSHOT_FILES,
    )


def complete_ad_prompt(prompt):
    # MLX takes several seconds to import and the weights are only needed
    # while the nightly job is classifying ads. Headphone sync must not pay
    # that cost. stream_generate is the same call generate() makes, and it
    # keeps the label-token logprobs that generate() throws away.
    import mlx_lm
    from mlx_lm.generate import stream_generate

    global _model, _tokenizer
    if _model is None or _tokenizer is None:
        _model, _tokenizer = mlx_lm.load(local_ad_model_snapshot())
    messages = [{"role": "user", "content": prompt}]
    templated = _tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    # The label can start at the quote (`paid`) or after a space (` paid`).
    # ` editorial` is one token, while `editorial` starts with `editor`.
    label_token_ids = {}
    for label in ("paid_ad", "house_promo", "editorial", "credits_or_outro"):
        label_token_ids[label] = {
            _tokenizer.encode(form, add_special_tokens=False)[0]
            for form in (label, " " + label)
        }
    parts = []
    chunks = []
    text = ""
    waiting_for_label = False
    captured = False
    for response in stream_generate(
        _model,
        _tokenizer,
        prompt=templated,
        max_tokens=2048,
    ):
        piece = response.text or ""
        parts.append(piece)
        row = None
        # Only the first label token carries the probability we keep. Reading
        # the full-vocab vector on every other token just syncs the device.
        if not captured and response.logprobs is not None and waiting_for_label and piece.strip():
            row = {
                label: max(float(response.logprobs[token_id]) for token_id in token_ids)
                for label, token_ids in label_token_ids.items()
            }
            captured = True
        upcoming = text + piece
        if not captured and _CLASSIFICATION_OPEN.search(upcoming):
            waiting_for_label = True
        text = upcoming
        chunks.append((piece, row))
    return ModelReply("".join(parts), paid_ad_confidence_from_chunks(chunks))
