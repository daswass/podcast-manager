import json
import math
import re


# A chapter title marks an ad. Word boundaries keep "adaptation" and
# "additional" from matching.
_AD_CHAPTER = re.compile(
    r"\b(ad|ads|sponsor|sponsored|sponsorship|promo|promotion|advertisement|advertising|commercial|commercials)\b",
    re.IGNORECASE,
)

MAX_MODEL_SPAN_SECONDS = 240
MAX_AD_FRACTION = 1 / 3
WINDOW_SECONDS = 600
OVERLAP_SECONDS = 180
_MIN_QUOTE_CHARS = 8


def chapter_spans(chapters, duration):
    ordered = []
    for chapter in chapters or []:
        start = _time(chapter, "startTime", "start")
        if start is None:
            continue
        ordered.append((start, chapter))
    ordered.sort(key=lambda item: item[0])

    spans = []
    for index, (start, chapter) in enumerate(ordered):
        if not _AD_CHAPTER.search(str(chapter.get("title") or "")):
            continue
        end = _time(chapter, "endTime", "end")
        if end is None and index + 1 < len(ordered):
            end = ordered[index + 1][0]
        if end is None:
            end = duration
        if end is not None and end > start:
            spans.append((start, end))
    return merge_spans(spans)


def transcript_windows(segments, window_seconds=WINDOW_SECONDS, overlap_seconds=OVERLAP_SECONDS):
    if not segments:
        return []
    episode_end = max(float(segment["end"]) for segment in segments)
    step = window_seconds - overlap_seconds
    if step <= 0:
        step = window_seconds
    windows = []
    cursor = 0.0
    while cursor < episode_end:
        window_end = cursor + window_seconds
        chunk = [
            segment
            for segment in segments
            if float(segment["end"]) > cursor and float(segment["start"]) < window_end
        ]
        if chunk:
            windows.append(chunk)
        if window_end >= episode_end:
            break
        cursor += step
    return windows


def build_ad_prompt(window):
    return (
        "You mark advertisements in one window of a podcast transcript.\n"
        'Return JSON only: {"ads": [{"start": <seconds>, "end": <seconds>, '
        '"quote": "<exact words copied from the transcript>"}]}\n'
        "An ad is a sponsor read, a promo code, or a produced commercial. "
        "A product mentioned as part of the story is not an ad.\n"
        "start and end are seconds from the start of the episode and must "
        "match the segment timestamps. If there is no ad, return {\"ads\": []}.\n\n"
        f"Transcript:\n{_format_transcript(window)}\n"
    )


def build_narrow_start_prompt(window):
    return (
        "The last lines of this short section name a sponsor.\n"
        "Return the first line of that sponsor's pitch. "
        "A problem stated immediately before the product is part of the pitch. "
        "The interview and a previous sponsor's website are not.\n"
        'Return JSON only: {"start_line": <number>}\n\n'
        f"Transcript:\n{_format_plain(window)}\n"
    )


def build_wide_start_prompt(window, anchor_line):
    return (
        f"Line {anchor_line} is inside one sponsor read.\n"
        "Return the line number where THAT read starts.\n"
        "The start can be a quick break, a brought-to-us-by line, or the problem stated "
        "just before the product is named. It is not the interview before that, "
        "and it is not an earlier sponsor.\n"
        'Return JSON only: {"start_line": <number>}\n\n'
        f"Transcript:\n{_format_plain(window)}\n"
    )


def build_end_prompt(window):
    return (
        "Line 1 is the first line of a sponsor offer.\n"
        "Return the last line of this same offer. Include a repeated website, a shipping note, "
        "a code, or a free gift.\n"
        "Stop before a different sponsor and before the interview.\n"
        'Return JSON only: {"end_line": <number>}\n\n'
        f"Transcript:\n{_format_plain(window)}\n"
    )


def parse_model_spans(response, window, first_sentence_only=False):
    payload = _load_json_object(response)
    ads = payload.get("ads") if isinstance(payload, dict) else None
    if not isinstance(ads, list):
        ads = _ads_from_truncated_json(response)
    if not isinstance(ads, list) or not window:
        return []
    window_start = min(float(segment["start"]) for segment in window)
    window_end = max(float(segment["end"]) for segment in window)
    spans = []
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        start = _number(ad.get("start"))
        end = _number(ad.get("end"))
        quote = _norm(ad.get("quote"))
        if start is None or end is None or end <= start:
            continue
        if end - start > MAX_MODEL_SPAN_SECONDS:
            continue
        if start < window_start - 1 or end > window_end + 1:
            continue
        text = _span_text(window, start, end)
        full_quote = quote
        if first_sentence_only:
            quote = _first_sentence(quote)
        if len(quote) < _MIN_QUOTE_CHARS or not _quote_in_order(quote, text):
            continue
        if first_sentence_only:
            start, end = _cover_quote(window, start, end, full_quote)
        if end - start > MAX_MODEL_SPAN_SECONDS:
            continue
        spans.append((start, end))
    return merge_spans(spans)


def classify_transcript(segments, duration, complete):
    found = []
    for window in transcript_windows(segments):
        found.extend(parse_model_spans(complete(build_ad_prompt(window)), window))
    seeds = merge_spans(found)
    bounded = []
    for seed in seeds:
        bounded.extend(_bound_seed(segments, seed, seeds, complete))
    merged = merge_spans(bounded)
    covered = sum(end - start for start, end in merged)
    if duration > 0 and covered > duration * MAX_AD_FRACTION:
        print(
            f"\tAd model marked {covered:.0f}s of {duration:.0f}s; ignoring that pass"
        )
        return None
    return merged


def choose_cut_spans(phrase_spans, model_spans, chapter_spans):
    chapters = merge_spans(chapter_spans)
    if model_spans is None:
        return merge_spans(list(chapters) + list(phrase_spans or []))
    return merge_spans(list(chapters) + list(model_spans))


def tighten_spans(audio, spans, search_seconds=3.0):
    search_ms = int(search_seconds * 1000)
    tightened = []
    for start, end in spans:
        start_ms = int(round(float(start) * 1000))
        end_ms = int(round(float(end) * 1000))
        new_start = _snap_edge(audio, start_ms, search_ms)
        new_end = _snap_edge(audio, end_ms, search_ms)
        if new_end - new_start < 300:
            tightened.append((float(start), float(end)))
        else:
            tightened.append((new_start / 1000, new_end / 1000))
    return merge_spans(tightened)


def merge_spans(spans, max_gap=0):
    ordered = sorted((float(start), float(end)) for start, end in spans if end > start)
    if not ordered:
        return []
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if _spans_join(start, merged[-1][1], max_gap):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _format_transcript(window):
    lines = []
    for segment in window:
        text = (segment.get("text") or "").strip()
        lines.append(f"[{float(segment['start']):.1f}-{float(segment['end']):.1f}] {text}")
    return "\n".join(lines)


_CLOSING_WORDS = ("again", "slash", ".com", "dot com", "code", "credit", "ship", "free", "gift", "subscription")
_NEW_SPONSOR = re.compile(
    r"\b(brought to (?:us|you) by|acknowledge our sponsor|this (?:episode|podcast) is sponsored)\b",
    re.IGNORECASE,
)
_END_WINDOW_SECONDS = 100
_FOLLOW_UPS = 3


def _format_plain(window):
    lines = []
    for index, segment in enumerate(window, start=1):
        lines.append(f"{index}. {(segment.get('text') or '').strip()}")
    return "\n".join(lines)


def _bound_seed(segments, seed, seeds, complete):
    start = _locate_start(segments, seed, seeds, complete)
    if start is None:
        return [seed]
    end = _locate_end(segments, start, complete)
    if end is None:
        return [seed]
    span = (float(start["start"]), float(end["end"]))
    if span[1] <= span[0] or not _overlaps(span, seed):
        return [seed]
    pieces = [
        piece for piece in _split_new_sponsors(segments, span)
        if 0 < piece[1] - piece[0] <= MAX_MODEL_SPAN_SECONDS
    ]
    return pieces or [seed]


def _locate_start(segments, seed, seeds, complete):
    anchor = _anchor_segment(segments, seed)
    if anchor is None:
        return None
    narrow = _segments_between(segments, float(anchor["start"]) - 40, float(anchor["end"]) + 5)
    wide = _segments_between(segments, float(anchor["start"]) - 110, float(anchor["end"]) + 12)
    narrow_seg = _chosen_segment(narrow, complete(build_narrow_start_prompt(narrow)), "start_line") if narrow else None
    anchor_line = _line_number_of(wide, anchor) if wide else None
    wide_seg = None
    if wide and anchor_line is not None:
        wide_seg = _chosen_segment(
            wide,
            complete(build_wide_start_prompt(wide, anchor_line)),
            "start_line",
        )
    return _pick_start(anchor, narrow_seg, wide_seg, seed, seeds)


def _locate_end(segments, start, complete):
    window = _segments_between(segments, float(start["start"]) - 0.05, float(start["start"]) + _END_WINDOW_SECONDS)
    if not window:
        return None
    chosen = _chosen_segment(window, complete(build_end_prompt(window)), "end_line")
    if chosen is None:
        return None
    return _extend_closing(segments, _retreat_to_closing(window, chosen), complete)


def _extend_closing(segments, end, complete):
    for _ in range(_FOLLOW_UPS):
        later = [segment for segment in segments if float(segment["start"]) >= float(end["start"]) - 0.05]
        window = later[:3]
        if len(window) < 2:
            return end
        chosen = _chosen_segment(window, complete(build_end_prompt(window)), "end_line")
        if chosen is None or chosen not in window:
            return end
        limit = window.index(chosen)
        picked = None
        for index in range(1, limit + 1):
            if _is_closing_line(window[index].get("text")):
                picked = window[index]
        if picked is None:
            return end
        end = picked
    return end


def _retreat_to_closing(window, chosen):
    if _is_closing_line(chosen.get("text")):
        return chosen
    index = window.index(chosen)
    for earlier in range(index - 1, -1, -1):
        if _is_closing_line(window[earlier].get("text")):
            return window[earlier]
    return chosen


def _split_new_sponsors(segments, span):
    covered = [
        segment for segment in segments
        if float(segment["end"]) > span[0] + 0.5 and float(segment["start"]) < span[1]
    ]
    pieces = []
    current = span[0]
    for segment in covered[1:]:
        if not _NEW_SPONSOR.search(segment.get("text") or ""):
            continue
        boundary = float(segment["start"])
        if boundary > current:
            pieces.append((current, boundary))
        current = boundary
    if span[1] > current:
        pieces.append((current, span[1]))
    return pieces or [span]


def _pick_start(anchor, narrow, wide, seed, seeds):
    chosen = anchor
    for segment in (wide, narrow):
        if segment is None:
            continue
        if float(segment["start"]) > float(anchor["start"]) + 1:
            continue
        if float(anchor["start"]) - float(segment["start"]) > 110:
            continue
        if _overlaps_other(segment, seed, seeds):
            continue
        if float(segment["start"]) < float(chosen["start"]):
            chosen = segment
    return chosen


def _overlaps_other(segment, seed, seeds):
    span = (float(segment["start"]), float(segment["end"]))
    for other in seeds:
        if _overlaps(other, seed):
            continue
        if _overlaps(span, other):
            return True
    return False


def _anchor_segment(segments, seed):
    for segment in segments:
        if float(segment["end"]) > seed[0] and float(segment["start"]) < seed[1]:
            return segment
    return None


def _segments_between(segments, start, end):
    return [
        segment
        for segment in segments
        if start <= float(segment["start"]) < end
    ]


def _chosen_segment(window, response, key):
    index = _line_index(_load_json_object(response).get(key), len(window))
    if index is None:
        return None
    return window[index]


def _line_number_of(window, segment):
    for index, candidate in enumerate(window, start=1):
        if candidate is segment:
            return index
    return None


def _is_closing_line(text):
    norm = _norm(text)
    return any(word in norm for word in _CLOSING_WORDS)


def _line_index(value, count):
    number = _number(value)
    if number is None:
        return None
    line = int(number)
    if line != number or line < 1 or line > count:
        return None
    return line - 1


def _spans_join(start, previous_end, max_gap):
    if max_gap > 0:
        return start <= previous_end + max_gap
    return start <= previous_end - 1


def _overlaps(left, right):
    return left[0] < right[1] and right[0] < left[1]


def _quote_in_order(quote, text):
    parts = [part.strip() for part in re.split(r"[.!?]+", quote) if len(part.strip()) >= _MIN_QUOTE_CHARS]
    if not parts:
        return quote in text
    cursor = 0
    for part in parts:
        index = text.find(part, cursor)
        if index == -1:
            return False
        cursor = index + len(part)
    return True


def _span_text(window, start, end):
    parts = []
    for segment in window:
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        if seg_end >= start - 2 and seg_start <= end + 2:
            parts.append(segment.get("text") or "")
    return _norm(" ".join(parts))


def _load_json_object(response):
    text = response or ""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def _ads_from_truncated_json(response):
    ads = []
    pattern = re.compile(
        r'"start"\s*:\s*(?P<start>[0-9.]+)\s*,\s*"end"\s*:\s*(?P<end>[0-9.]+)\s*,\s*"quote"\s*:\s*"(?P<quote>.*)',
        re.DOTALL,
    )
    for match in pattern.finditer(response or ""):
        quote = match.group("quote")
        closing = quote.find('"')
        if closing != -1:
            quote = quote[:closing]
        ads.append({
            "start": match.group("start"),
            "end": match.group("end"),
            "quote": quote,
        })
    return ads


def _first_sentence(quote):
    sentence = re.split(r"[.!?]", quote, maxsplit=1)[0].strip()
    if len(sentence) < _MIN_QUOTE_CHARS:
        return quote
    return sentence


def _cover_quote(window, start, end, quote):
    parts = [part.strip() for part in re.split(r"[.!?]+", quote) if len(part.strip()) >= _MIN_QUOTE_CHARS]
    if len(parts) < 2:
        return start, end
    pieces = []
    cursor = 0
    blob_parts = []
    for segment in window:
        text = _norm(segment.get("text"))
        if not text:
            continue
        begin = cursor
        blob_parts.append(text)
        cursor += len(text) + 1
        pieces.append((begin, cursor, float(segment["start"]), float(segment["end"])))
    blob = " ".join(blob_parts)
    position = 0
    located = []
    for part in parts:
        index = blob.find(part, position)
        if index == -1:
            break
        located.append((index, index + len(part)))
        position = index + len(part)
    if len(located) < 2:
        return start, end
    covered_start = None
    covered_end = None
    first_index = located[0][0]
    last_index = located[-1][1] - 1
    for begin, finish, seg_start, seg_end in pieces:
        if covered_start is None and begin <= first_index < finish:
            covered_start = seg_start
        if begin <= last_index < finish:
            covered_end = seg_end
    if covered_start is None or covered_end is None or covered_end <= covered_start:
        return start, end
    if covered_end - covered_start > MAX_MODEL_SPAN_SECONDS:
        return start, end
    if covered_end <= start or covered_start >= end:
        return start, end
    return covered_start, covered_end


def _time(chapter, *keys):
    for key in keys:
        if key in chapter:
            return _number(chapter.get(key))
    return None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _snap_edge(audio, edge_ms, search_ms, frame_ms=100):
    candidates = _edge_candidates(audio, edge_ms, search_ms, frame_ms)
    if not candidates:
        return edge_ms
    return min(candidates, key=lambda mark: (abs(mark - edge_ms), mark))


def _edge_candidates(audio, edge_ms, search_ms, frame_ms):
    duration = len(audio)
    lo = max(0, edge_ms - search_ms)
    hi = min(duration, edge_ms + search_ms)
    frames = []
    cursor = lo
    while cursor + frame_ms <= hi:
        frames.append((cursor, _dbfs(audio[cursor:cursor + frame_ms])))
        cursor += frame_ms
    candidates = []
    for index, (mark, level) in enumerate(frames):
        if not math.isfinite(level) or level < -40:
            candidates.append(mark)
            candidates.append(min(duration, mark + frame_ms))
        if index + 1 < len(frames):
            if _loudness_jump(level, frames[index + 1][1]) >= 8:
                candidates.append(frames[index + 1][0])
    return [
        mark for mark in candidates
        if 0 <= mark <= duration and abs(mark - edge_ms) <= search_ms
    ]


def _dbfs(piece):
    if len(piece) == 0:
        return 0.0
    level = piece.dBFS
    if level is None or level == float("-inf"):
        return float("-inf")
    return level


def _loudness_jump(left, right):
    left_finite = math.isfinite(left)
    right_finite = math.isfinite(right)
    if left_finite and right_finite:
        return abs(left - right)
    if left_finite != right_finite:
        return 90.0
    return 0.0
