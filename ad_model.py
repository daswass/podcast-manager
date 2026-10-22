AD_MODEL = "mlx-community/Qwen2.5-7B-Instruct-4bit"

_model = None
_tokenizer = None


def complete_ad_prompt(prompt):
    # MLX takes several seconds to import and the weights are only needed
    # while the nightly job is classifying ads. Headphone sync must not pay
    # that cost.
    import mlx_lm

    global _model, _tokenizer
    if _model is None or _tokenizer is None:
        _model, _tokenizer = mlx_lm.load(AD_MODEL)
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
