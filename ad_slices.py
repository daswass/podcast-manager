# Host-read ads usually announce themselves. Matching these phrases, then
# stopping at a return-to-show line, avoids cutting an entire episode when a
# read never gets an explicit ending.
_AD_STARTS = (
    "this episode is sponsored",
    "this podcast is sponsored",
    "brought to you by",
    "sponsored by",
    "a quick word from",
    "we'll be right back",
    "we will be right back",
    "thanks to our sponsor",
    "promo code",
    "use code",
)

_AD_ENDS = (
    "welcome back",
    "and we're back",
    "and we are back",
    "now back to",
    "back to the episode",
    "back to the show",
)


def find_ad_spans(segments, max_ad_seconds=180):
    spans = []
    ad_start = None
    for segment in segments:
        text = (segment.get("text") or "").lower()
        start = float(segment["start"])
        end = float(segment["end"])
        if ad_start is None:
            if any(phrase in text for phrase in _AD_STARTS):
                ad_start = start
            continue
        if any(phrase in text for phrase in _AD_ENDS) and start > ad_start:
            spans.append((ad_start, start))
            ad_start = None
            continue
        if end - ad_start >= max_ad_seconds:
            spans.append((ad_start, ad_start + max_ad_seconds))
            ad_start = None
    if ad_start is not None and segments:
        last_end = float(segments[-1]["end"])
        spans.append((ad_start, min(last_end, ad_start + max_ad_seconds)))
    return spans


def cut_ads(audio, spans):
    duration = len(audio)
    clamped = []
    for start, end in spans:
        start_ms = max(0, int(start * 1000))
        end_ms = min(duration, int(end * 1000))
        if end_ms > start_ms:
            clamped.append((start_ms, end_ms))
    clamped.sort()

    pieces = []
    cursor = 0
    for start_ms, end_ms in clamped:
        if start_ms > cursor:
            pieces.append(audio[cursor:start_ms])
        cursor = max(cursor, end_ms)
    if cursor < duration:
        pieces.append(audio[cursor:])
    if not pieces:
        return audio[:0]

    combined = pieces[0]
    for piece in pieces[1:]:
        combined += piece
    return combined
