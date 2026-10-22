WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"


def transcribe_episode(path):
    # MLX takes about 20 seconds to import. Headphone sync must not pay that
    # cost, and it must still copy files when the transcriber is not installed.
    import mlx_whisper

    result = mlx_whisper.transcribe(
        str(path),
        path_or_hf_repo=WHISPER_MODEL,
        verbose=None,
        language="en",
        condition_on_previous_text=False,
    )
    return result.get("segments") or []
