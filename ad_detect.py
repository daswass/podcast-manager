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
EDGE_WINDOW_SECONDS = 45
EDGE_OVERLAP_SECONDS = 15
_MIN_QUOTE_CHARS = 8
_VERIFIER_LABELS = {"paid_ad", "house_promo", "editorial", "credits_or_outro"}
_STRONG_SPONSOR_CUE = re.compile(
    r"\b(?:acknowledge (?:one of )?our sponsors?|brought to (?:us|you)(?: in part)? by|"
    r"this (?:episode|podcast) is sponsored by)\b",
    re.IGNORECASE,
)
_COMMERCIAL_CTA = re.compile(
    r"\b(?:[a-z0-9-]+\.(?:com|io|org)|dot com|slash [a-z0-9]|use code|promo code|"
    r"sign up|get started|special offer|\d+% off|save up to|free (?:trial|gift|bottle|sample)|"
    r"terms apply|member fdic|paid membership)\b",
    re.IGNORECASE,
)
_COMMERCIAL_PITCH = re.compile(
    r"\b(?:checking accounts?|subscription|app|product|formula|mattress|sleep system|"
    r"software|platform|businesses|customers|purchase|shipping|ships to|off your first)\b",
    re.IGNORECASE,
)
_FALLBACK_EXCLUSION = re.compile(
    r"\b(?:acast|another podcast|show we recommend|wherever you (?:get|listen)|"
    r"subscribe to (?:this|our) (?:show|podcast)|follow (?:this|our|us)|newsletter|"
    r"executive producer|production credits|thank you for listening|that's all for this episode)\b",
    re.IGNORECASE,
)
_OPENING_COMMERCIAL_CUE = re.compile(
    r"\b(?:terms apply|member fdic|promo code|use code|free trial|"
    r"what(?:'|’)s in your wallet|sponsors? this episode)\b",
    re.IGNORECASE,
)
_OBVIOUS_HOUSE_PROMO = re.compile(
    r"\b(?:ask me anything|send (?:in |us )?your questions|"
    r"would like to advertise on|subject line advertising|send your question as a voice note)\b",
    re.IGNORECASE,
)
_SPONSOR_AFTER_CUE = re.compile(
    r"(?:acknowledge (?:one of )?our sponsors?|brought to (?:us|you)(?: in part)? by|this (?:episode|podcast) is sponsored by)"
    r"[, :]*([A-Za-z0-9][A-Za-z0-9 .&'-]{0,50})",
    re.IGNORECASE,
)
_DOMAIN_SPONSOR = re.compile(
    r"\b([a-z][a-z0-9-]{2,})\s*(?:\.| dot )\s*(?:com|io|org)\b", re.IGNORECASE
)


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


def candidate_windows(segments):
    """Return ordinary scan windows plus smaller overlapping edge windows."""
    windows = list(transcript_windows(segments))
    if not segments:
        return windows
    episode_end = max(float(segment["end"]) for segment in segments)
    step = EDGE_WINDOW_SECONDS - EDGE_OVERLAP_SECONDS
    last_start = max(0.0, episode_end - EDGE_WINDOW_SECONDS)
    edge_starts = [0.0, step, max(0.0, last_start - step), last_start]
    seen = {_window_key(window) for window in windows}
    for start in edge_starts:
        end = start + EDGE_WINDOW_SECONDS
        window = [
            segment for segment in segments
            if float(segment["end"]) > start and float(segment["start"]) < end
        ]
        key = _window_key(window)
        if window and key not in seen:
            windows.append(window)
            seen.add(key)
    return windows


def build_ad_prompt(window):
    return (
        "You mark advertisements in one window of a podcast transcript.\n"
        'Return JSON only: {"ads": [{"start": <seconds>, "end": <seconds>, '
        '"quote": "<exact words copied from the transcript>"}]}\n'
        "This is candidate discovery: enumerate every possible paid sponsor read or produced commercial; "
        "a later verifier will reject false positives. Keep adjacent advertisers as separate entries.\n"
        "Do not combine a paid ad with a different sponsor, network/show cross-promo, editorial/news discussion, "
        "host credits, guest credits, subscription/newsletter/social prompts, or a show outro. "
        "A sponsor name in credits or an editorial discussion of advertising is not itself a paid ad.\n"
        "start and end are seconds from the start of the episode and must "
        "match the segment timestamps. If there is no ad, return {\"ads\": []}.\n\n"
        f"Transcript:\n{_format_transcript(window)}\n"
    )


def build_verifier_prompt(window, candidate):
    return (
        "Classify one candidate from a podcast transcript. Precision is more important than recall.\n"
        "Use exactly one classification: paid_ad, house_promo, editorial, credits_or_outro.\n"
        "paid_ad requires direct evidence of a paid third-party sponsor pitch or produced commercial. "
        "Host-read acknowledgements such as 'acknowledge our sponsor, BRAND' are paid_ad even when the "
        "host's delivery matches the interview. A produced commercial may begin with a consumer problem, "
        "including at timestamp zero, and identify the advertiser only later through its product, URL, offer, "
        "or legal disclaimer. Classify that complete candidate as paid_ad. "
        "house_promo includes another show/network/Acast cross-promo, subscriptions, newsletters, events, "
        "or the podcast's own products. editorial includes news/reporting/interview discussion, even when it "
        "mentions brands or advertising. credits_or_outro includes host/guest/production credits, recaps, "
        "thanks, and closing music or sign-off language.\n"
        "Concrete negatives: an Acast voice recommending another podcast or saying to follow/listen wherever "
        "you get podcasts is house_promo; a show's closing credits, producer names, guest thanks, recap, or "
        "sign-off is credits_or_outro; a news clip, rally audio, or reporting about brands/advertising is "
        "editorial. A host joking that a guest, caffeine, or nicotine is a product recommendation is editorial, "
        "not a paid ad, unless the candidate itself has sponsor, offer, URL, code, or disclaimer evidence. "
        "These remain negative even beside a real paid ad. Classify only the candidate span, not a "
        "nearby paid ad in the context. Adjacent advertisers are separate paid ads; do not let evidence for "
        "one advertiser verify the other.\n"
        "Return JSON only: {\"classification\": <label>, \"sponsor\": <paid advertiser name or empty>, "
        "\"evidence_quote\": <exact consecutive words copied from the transcript>}.\n"
        "For paid_ad, sponsor and evidence_quote are required and the quote must identify that sponsor or "
        "its offer/product. For every other label sponsor must be empty. Never infer evidence not present.\n\n"
        f"Candidate: {candidate['start']:.1f}-{candidate['end']:.1f}; "
        f"discovery quote: {candidate['quote']}\n"
        f"Transcript:\n{_format_transcript(window)}\n"
    )


def _sponsor_instruction(sponsor, evidence):
    return (
        f"The paid advertiser is {sponsor}. Verified evidence: {evidence}\n"
        "Choose only a line belonging to this same advertiser. Do not cross into another advertiser, "
        "editorial/news content, a house or network cross-promo, credits, or an outro.\n"
    )


def build_narrow_start_prompt(window, sponsor="the verified sponsor", evidence="verified in this read"):
    return (
        _sponsor_instruction(sponsor, evidence)
        +
        "Return the first line of that sponsor's pitch. "
        "A problem stated immediately before the product is part of the pitch. "
        "The interview and a previous sponsor's website are not.\n"
        'Return JSON only: {"sponsor": "<same paid advertiser>", "start_line": <number>, "evidence_quote": '
        '"<exact words from that numbered line proving it belongs to this sponsor>"}\n\n'
        f"Transcript:\n{_format_plain(window)}\n"
    )


def build_wide_start_prompt(window, anchor_line, sponsor="the verified sponsor", evidence="verified in this read"):
    return (
        _sponsor_instruction(sponsor, evidence)
        +
        f"Line {anchor_line} is inside one sponsor read.\n"
        "Return the line number where THAT read starts.\n"
        "The start can be a quick break, a brought-to-us-by line, or the problem stated "
        "just before the product is named. It is not the interview before that, "
        "and it is not an earlier sponsor.\n"
        'Return JSON only: {"sponsor": "<same paid advertiser>", "start_line": <number>, "evidence_quote": '
        '"<exact words from that numbered line proving it belongs to this sponsor>"}\n\n'
        f"Transcript:\n{_format_plain(window)}\n"
    )


def build_end_prompt(window, sponsor="the verified sponsor", evidence="verified in this read"):
    return (
        _sponsor_instruction(sponsor, evidence)
        +
        "Line 1 is the first line of a sponsor offer.\n"
        "Return the last line of this same offer. Do not stop after the initial product description "
        "when later lines continue the benefits or host testimonial. Include the final website, shipping note, "
        "code, legal disclaimer, discount, or free gift.\n"
        "Stop before a different sponsor, editorial/interview material, credits/outro, or a cross-promo.\n"
        'Return JSON only: {"sponsor": "<same paid advertiser>", "end_line": <number>, "evidence_quote": '
        '"<exact words from that numbered line proving it belongs to this sponsor>"}\n\n'
        f"Transcript:\n{_format_plain(window)}\n"
    )


def parse_ad_candidates(response, window):
    payload = _load_json_object(response)
    ads = payload.get("ads") if isinstance(payload, dict) else None
    if not isinstance(ads, list):
        ads = _ads_from_truncated_json(response)
    if not isinstance(ads, list) or not window:
        return []
    window_start = min(float(segment["start"]) for segment in window)
    window_end = max(float(segment["end"]) for segment in window)
    candidates = []
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        start = _number(ad.get("start"))
        end = _number(ad.get("end"))
        quote = _norm(ad.get("quote"))
        if start is None or end is None or end <= start or end - start > MAX_MODEL_SPAN_SECONDS:
            continue
        if start < window_start - 1 or end > window_end + 1:
            continue
        if len(quote) < _MIN_QUOTE_CHARS or not _quote_in_order(quote, _span_text(window, start, end)):
            continue
        candidates.append({"start": start, "end": end, "quote": quote})
    return candidates


def parse_verified_candidate(response, window, candidate):
    payload = _load_json_object(response)
    if not isinstance(payload, dict):
        return None
    label = str(payload.get("classification") or "").strip().lower()
    sponsor = str(payload.get("sponsor") or "").strip()
    evidence = _norm(payload.get("evidence_quote"))
    if label not in _VERIFIER_LABELS or label != "paid_ad":
        return None
    if not sponsor or len(evidence) < _MIN_QUOTE_CHARS:
        return None
    candidate_text = _candidate_span_text(window, candidate["start"], candidate["end"])
    if evidence not in candidate_text:
        return None
    return {
        "start": candidate["start"],
        "end": candidate["end"],
        "quote": candidate["quote"],
        "sponsor": sponsor,
        "sponsor_key": _sponsor_key(sponsor),
        "evidence": evidence,
    }


def parse_model_spans(response, window, first_sentence_only=False):
    if not first_sentence_only:
        return merge_spans(
            (item["start"], item["end"])
            for item in parse_ad_candidates(response, window)
        )
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
    # Explicit host-read introductions are deterministic discovery seeds. The
    # verifier still decides whether they are paid ads; this only prevents a
    # long discovery window from silently omitting an obvious sponsor read.
    discovered = _strong_sponsor_candidates(segments) + _opening_block_candidates(segments)
    for window in candidate_windows(segments):
        discovered.extend(parse_ad_candidates(complete(build_ad_prompt(window)), window))
    candidates = _merge_discovery_candidates(discovered)
    verified = []
    for candidate in candidates:
        context = _candidate_context(segments, candidate)
        candidate_text = _candidate_span_text(context, candidate["start"], candidate["end"])
        if _OBVIOUS_HOUSE_PROMO.search(candidate_text):
            continue
        verifier_response = complete(build_verifier_prompt(context, candidate))
        result = parse_verified_candidate(
            verifier_response,
            context,
            candidate,
        )
        if result is not None and not _verified_paid_has_commercial_grounding(context, candidate):
            result = None
        if result is None:
            result = _deterministic_paid_fallback(context, candidate)
        if result is not None:
            verified.append(result)
    verified = _merge_verified_candidates(verified)
    seeds = [(candidate["start"], candidate["end"]) for candidate in verified]
    bounded = []
    for candidate in verified:
        bounded.extend(
            _trim_after_commercial_close(
                segments, _expand_explicit_sponsor_read(segments, candidate, span)
            )
            for span in _bound_candidate(segments, candidate, verified, complete)
        )
    merged = merge_spans(bounded)
    covered = sum(end - start for start, end in merged)
    if duration > 0 and covered > duration * MAX_AD_FRACTION:
        print(
            f"\tAd model marked {covered:.0f}s of {duration:.0f}s; ignoring that pass"
        )
        return None
    return merged


def _strong_sponsor_candidates(segments):
    candidates = []
    for index, segment in enumerate(segments):
        text = _norm(segment.get("text"))
        if not _STRONG_SPONSOR_CUE.search(text):
            continue
        end = float(segment["end"])
        limit = float(segment["start"]) + 180
        for later in segments[index + 1:]:
            if float(later["start"]) >= limit:
                break
            later_text = _norm(later.get("text"))
            if "again" in later_text and _is_closing_line(later_text):
                end = float(later["end"])
                break
        candidates.append({
            "start": float(segment["start"]),
            "end": end,
            "quote": text,
        })
    return candidates


def _opening_block_candidates(segments, min_gap=5, max_seconds=MAX_MODEL_SPAN_SECONDS):
    """Seed a self-contained preroll block before a transcript gap."""
    if not segments or float(segments[0]["start"]) > 1:
        return []
    block = [segments[0]]
    for segment in segments[1:]:
        gap = float(segment["start"]) - float(block[-1]["end"])
        if gap >= min_gap:
            break
        block.append(segment)
    start = float(block[0]["start"])
    end = float(block[-1]["end"])
    if len(block) == len(segments) or end <= start or end - start > max_seconds:
        return []
    quote = _norm(block[0].get("text"))
    block_text = " ".join(_norm(item.get("text")) for item in block)
    if len(quote) < _MIN_QUOTE_CHARS or not _OPENING_COMMERCIAL_CUE.search(block_text):
        return []
    return [{"start": start, "end": end, "quote": quote}]


def _candidate_context(segments, candidate, padding=15):
    return _segments_between(
        segments,
        float(candidate["start"]) - padding,
        float(candidate["end"]) + padding,
    )


def _deterministic_paid_fallback(context, candidate):
    """Keep transcript-grounded commercial seeds when model verification fails."""
    candidate_text = _candidate_span_text(context, candidate["start"], candidate["end"])
    explicit = _STRONG_SPONSOR_CUE.search(candidate_text)
    commercial = _COMMERCIAL_CTA.search(candidate_text) and (
        _COMMERCIAL_PITCH.search(candidate_text) or _repeated_brand_token(candidate_text)
    )
    if _FALLBACK_EXCLUSION.search(candidate_text) or not (explicit or commercial):
        return None
    sponsor = _fallback_sponsor(candidate_text, candidate_text)
    if not sponsor:
        return None
    evidence = _norm(explicit.group(0) if explicit else candidate["quote"])
    return {
        "start": candidate["start"],
        "end": candidate["end"],
        "quote": candidate["quote"],
        "sponsor": sponsor,
        "sponsor_key": _sponsor_key(sponsor),
        "evidence": evidence,
        "deterministic_fallback": True,
    }


def _verified_paid_has_commercial_grounding(context, candidate):
    candidate_text = _candidate_span_text(context, candidate["start"], candidate["end"])
    return bool(
        re.search(r"\b(?:paid )?sponsors?(?:ed|hip)?\b|\bbrought to (?:us|you)(?: in part)? by\b", candidate_text)
        or _COMMERCIAL_CTA.search(candidate_text)
    )


def _trim_after_commercial_close(segments, span):
    covered = [
        segment for segment in segments
        if float(segment["end"]) > span[0] and float(segment["start"]) < span[1]
    ]
    closing = [segment for segment in covered if _is_closing_line(segment.get("text"))]
    if not closing:
        return span
    end = min(float(span[1]), float(closing[-1]["end"]))
    return (float(span[0]), end) if end > span[0] else span


def _expand_explicit_sponsor_read(segments, candidate, span):
    seed_text = _span_text(segments, candidate["start"], candidate["end"])
    if not _STRONG_SPONSOR_CUE.search(seed_text):
        return span
    anchor = _anchor_segment(segments, (candidate["start"], candidate["end"]))
    if anchor is None:
        return span
    index = segments.index(anchor)
    end = float(span[1])
    limit = float(anchor["start"]) + 120
    transition = re.compile(r"\b(?:back to|welcome back|let's get back|interview (?:continues|begins))\b", re.IGNORECASE)
    for later in segments[index + 1:]:
        start = float(later["start"])
        text = later.get("text") or ""
        if start >= limit or _FALLBACK_EXCLUSION.search(text) or transition.search(text) or _NEW_SPONSOR.search(text):
            break
        end = max(end, float(later["end"]))
        if _is_closing_line(text):
            break
    return (min(float(span[0]), float(anchor["start"])), end)


def _fallback_sponsor(candidate_text, context_text):
    match = _SPONSOR_AFTER_CUE.search(candidate_text)
    if match:
        sponsor = re.split(r"[.!?]", match.group(1), maxsplit=1)[0].strip(" ,:-")
        if sponsor:
            return sponsor
    for text in (candidate_text, context_text):
        match = _DOMAIN_SPONSOR.search(text)
        if match:
            sponsor = match.group(1)
            if sponsor.lower().startswith("drink") and len(sponsor) > 7:
                sponsor = sponsor[5:]
            return sponsor
    return None


def _repeated_brand_token(text):
    words = re.findall(r"\b[a-z][a-z0-9]{2,}\b", _norm(text))
    ignored = {"the", "and", "with", "that", "this", "your", "for", "from", "are", "you", "our"}
    counts = {}
    for word in words:
        if word not in ignored:
            counts[word] = counts.get(word, 0) + 1
    return any(count >= 2 for count in counts.values())


def _merge_discovery_candidates(candidates):
    merged = []
    for candidate in sorted(candidates, key=lambda item: (item["start"], item["end"])):
        duplicate = None
        for existing in merged:
            if candidate["quote"] == existing["quote"] and _overlaps(
                (candidate["start"], candidate["end"]),
                (existing["start"], existing["end"]),
            ):
                duplicate = existing
                break
        if duplicate is None:
            merged.append(dict(candidate))
        else:
            duplicate["start"] = min(duplicate["start"], candidate["start"])
            duplicate["end"] = max(duplicate["end"], candidate["end"])
            if len(candidate["quote"]) > len(duplicate["quote"]):
                duplicate["quote"] = candidate["quote"]
    return merged


def _merge_verified_candidates(candidates):
    merged = []
    for candidate in sorted(candidates, key=lambda item: (item["start"], item["end"])):
        if merged and _same_sponsor(candidate["sponsor_key"], merged[-1]["sponsor_key"]) and (
            candidate["start"] <= merged[-1]["end"] + 1
        ):
            merged[-1]["start"] = min(merged[-1]["start"], candidate["start"])
            merged[-1]["end"] = max(merged[-1]["end"], candidate["end"])
            continue
        merged.append(dict(candidate))
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


def _bound_candidate(segments, candidate, candidates, complete):
    seed = (candidate["start"], candidate["end"])
    seeds = [(item["start"], item["end"]) for item in candidates]
    start = _locate_start(segments, candidate, seeds, complete)
    if start is None:
        return [seed]
    end = _locate_end(segments, start, candidate, complete)
    if end is None:
        return [seed]
    span = (float(start["start"]), float(end["end"]))
    span = _clip_at_sponsor_changes(span, candidate, candidates)
    if span[1] <= span[0] or not _overlaps(span, seed):
        return [seed]
    if span[1] - span[0] > MAX_MODEL_SPAN_SECONDS:
        return [seed]
    return [span]


def _bound_seed(segments, seed, seeds, complete):
    """Compatibility wrapper for older callers; production uses verified candidates."""
    candidate = {
        "start": seed[0], "end": seed[1], "sponsor": "verified sponsor",
        "sponsor_key": "verified sponsor", "evidence": "verified sponsor evidence",
    }
    candidates = [
        dict(candidate, start=other[0], end=other[1]) for other in seeds
    ]
    return _bound_candidate(segments, candidate, candidates, complete)


def _locate_start(segments, candidate, seeds, complete):
    seed = (candidate["start"], candidate["end"])
    anchor = _anchor_segment(segments, seed)
    if anchor is None:
        return None
    narrow = _segments_between(segments, float(anchor["start"]) - 40, float(anchor["end"]) + 5)
    wide = _segments_between(segments, float(anchor["start"]) - 110, float(anchor["end"]) + 12)
    narrow_seg = _chosen_grounded_segment(
        narrow,
        complete(build_narrow_start_prompt(narrow, candidate["sponsor"], candidate["evidence"])),
        "start_line",
        candidate["sponsor"],
    ) if narrow else None
    anchor_line = _line_number_of(wide, anchor) if wide else None
    wide_seg = None
    if wide and anchor_line is not None:
        wide_seg = _chosen_grounded_segment(
            wide,
            complete(build_wide_start_prompt(
                wide, anchor_line, candidate["sponsor"], candidate["evidence"]
            )),
            "start_line",
            candidate["sponsor"],
        )
    chosen = _pick_start(anchor, narrow_seg, wide_seg, seed, seeds)
    return _recover_structural_edge_gap(segments, chosen, candidate, seeds)


def _recover_structural_edge_gap(segments, chosen, candidate, seeds, min_gap=5, max_gap=60):
    """Recover an omitted produced-ad lead only with strong edge structure.

    Whisper can omit most of a produced commercial at the very start or end of
    an episode.  Never bridge an interior gap.  At an edge, require both
    transcript-grounded commercial evidence in the candidate and either an
    immediately preceding verified opening ad or an explicit closing/outro.
    """
    try:
        index = segments.index(chosen)
    except ValueError:
        return chosen
    if index == 0:
        return chosen
    previous = segments[index - 1]
    previous_end = float(previous["end"])
    chosen_start = float(chosen["start"])
    gap = chosen_start - previous_end
    if not min_gap <= gap <= max_gap:
        return chosen
    episode_end = max(float(segment["end"]) for segment in segments)
    at_opening = previous_end <= 90
    at_closing = episode_end - chosen_start <= 90
    if not (at_opening or at_closing):
        return chosen
    candidate_text = _candidate_span_text(
        segments, candidate["start"], candidate["end"]
    )
    if not (_COMMERCIAL_CTA.search(candidate_text) or _OPENING_COMMERCIAL_CUE.search(candidate_text)):
        return chosen
    prior_verified_ad = at_opening and any(
        abs(float(end) - previous_end) <= 2
        and not (float(start) == float(candidate["start"]) and float(end) == float(candidate["end"]))
        for start, end in seeds
    )
    previous_text = _norm(previous.get("text"))
    explicit_outro = at_closing and bool(
        _FALLBACK_EXCLUSION.search(previous_text)
        or re.search(
            r"\b(?:thank you for (?:your )?(?:interest|listening)|last but (?:certainly )?not least|that's all for this episode)\b",
            previous_text,
        )
    )
    if not (prior_verified_ad or explicit_outro):
        return chosen
    return {
        "start": previous_end,
        "end": float(chosen["end"]),
        "text": chosen.get("text") or "",
    }


def _locate_end(segments, start, candidate, complete):
    window = _segments_between(segments, float(start["start"]) - 0.05, float(start["start"]) + _END_WINDOW_SECONDS)
    if not window:
        return None
    chosen = _chosen_grounded_segment(
        window,
        complete(build_end_prompt(window, candidate["sponsor"], candidate["evidence"])),
        "end_line",
        candidate["sponsor"],
    )
    if chosen is None:
        return None
    return _extend_closing(segments, _retreat_to_closing(window, chosen), candidate, complete)


def _extend_closing(segments, end, candidate, complete):
    for _ in range(_FOLLOW_UPS):
        later = [segment for segment in segments if float(segment["start"]) >= float(end["start"]) - 0.05]
        window = later[:3]
        if len(window) < 2:
            return end
        chosen = _chosen_grounded_segment(
            window,
            complete(build_end_prompt(window, candidate["sponsor"], candidate["evidence"])),
            "end_line",
            candidate["sponsor"],
        )
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


def _clip_at_sponsor_changes(span, candidate, candidates):
    start, end = span
    for other in candidates:
        if other is candidate or _same_sponsor(other["sponsor_key"], candidate["sponsor_key"]):
            continue
        if other["end"] <= candidate["start"]:
            start = max(start, other["end"])
        elif other["start"] >= candidate["end"]:
            end = min(end, other["start"])
        elif other["start"] < candidate["start"]:
            start = max(start, other["end"])
        else:
            end = min(end, other["start"])
    return start, end


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


def _chosen_grounded_segment(window, response, key, expected_sponsor):
    payload = _load_json_object(response)
    if not _same_sponsor(payload.get("sponsor"), expected_sponsor):
        return None
    index = _line_index(payload.get(key), len(window))
    if index is None:
        return None
    evidence = _norm(payload.get("evidence_quote"))
    line = _norm(window[index].get("text"))
    if len(evidence) < _MIN_QUOTE_CHARS or evidence not in line:
        return None
    return window[index]


def _line_number_of(window, segment):
    for index, candidate in enumerate(window, start=1):
        if candidate is segment:
            return index
    return None


def _window_key(window):
    return tuple(
        (float(item["start"]), float(item["end"]), item.get("text") or "")
        for item in window
    )


def _sponsor_key(sponsor):
    return re.sub(r"[^a-z0-9]+", "", _norm(sponsor))


def _same_sponsor(left, right):
    left_key = _sponsor_key(left)
    right_key = _sponsor_key(right)
    if not left_key or not right_key:
        return False
    return left_key == right_key or (
        min(len(left_key), len(right_key)) >= 4
        and (left_key in right_key or right_key in left_key)
    )


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


def _candidate_span_text(window, start, end):
    """Text from segments truly overlapping a candidate, without padding."""
    return _norm(" ".join(
        segment.get("text") or ""
        for segment in window
        if float(segment["end"]) > float(start)
        and float(segment["start"]) < float(end)
    ))


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
