from huggingface_hub import snapshot_download

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
    # that cost.
    import mlx_lm

    global _model, _tokenizer
    if _model is None or _tokenizer is None:
        _model, _tokenizer = mlx_lm.load(local_ad_model_snapshot())
    messages = [{"role": "user", "content": prompt}]
    templated = _tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    return mlx_lm.generate(
        _model,
        _tokenizer,
        prompt=templated,
        max_tokens=2048,
        verbose=False,
    )
