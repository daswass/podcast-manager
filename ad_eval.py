"""Measure production ad cuts against manually-reviewed timestamp truth.

The live path deliberately imports the same MLX Whisper and Qwen functions used by
the nightly podcast job.  Do not substitute a cloud transcriber or a different
local model here: the point is to measure the laptop production path.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ad_detect import classify_transcript, merge_spans
from ad_model import AD_MODEL, complete_ad_prompt
from whisper_transcribe import WHISPER_MODEL, transcribe_episode

SCHEMA = "podcast-manager.ad-eval.v1"


def _span(item):
    if isinstance(item, dict):
        return float(item["start"]), float(item["end"])
    return float(item[0]), float(item[1])


def normalize(spans):
    return merge_spans([_span(span) for span in spans if _span(span)[1] > _span(span)[0]])


def duration(spans):
    return sum(end - start for start, end in normalize(spans))


def intersection_seconds(left, right):
    total = 0.0
    for start, end in normalize(left):
        for other_start, other_end in normalize(right):
            total += max(0.0, min(end, other_end) - max(start, other_start))
    return total


def _match_spans(predicted, reference):
    """One-to-one maximum-overlap matching for boundary diagnostics."""
    pairs = []
    for pi, (ps, pe) in enumerate(predicted):
        for ri, (rs, re) in enumerate(reference):
            overlap = max(0.0, min(pe, re) - max(ps, rs))
            if overlap:
                pairs.append((overlap, pi, ri))
    matched = []
    used_predicted, used_reference = set(), set()
    for overlap, pi, ri in sorted(pairs, reverse=True):
        if pi not in used_predicted and ri not in used_reference:
            used_predicted.add(pi)
            used_reference.add(ri)
            matched.append((predicted[pi], reference[ri], overlap))
    return matched


def score(predicted, reference, boundary_tolerance=5.0):
    predicted, reference = normalize(predicted), normalize(reference)
    predicted_seconds, reference_seconds = duration(predicted), duration(reference)
    overlap = intersection_seconds(predicted, reference)
    precision = overlap / predicted_seconds if predicted_seconds else (1.0 if not reference_seconds else 0.0)
    recall = overlap / reference_seconds if reference_seconds else (1.0 if not predicted_seconds else 0.0)
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    matches = _match_spans(predicted, reference)
    boundaries = [
        {
            "predicted": {"start": ps, "end": pe},
            "reference": {"start": rs, "end": re},
            "overlap_seconds": overlap_seconds,
            "start_error_seconds": round(ps - rs, 3),
            "end_error_seconds": round(pe - re, 3),
            "both_edges_within_tolerance": abs(ps-rs) <= boundary_tolerance and abs(pe-re) <= boundary_tolerance,
        }
        for (ps, pe), (rs, re), overlap_seconds in matches
    ]
    return {
        "predicted_spans": [{"start": s, "end": e} for s, e in predicted],
        "reference_spans": [{"start": s, "end": e} for s, e in reference],
        "predicted_seconds": round(predicted_seconds, 3),
        "reference_seconds": round(reference_seconds, 3),
        "overlap_seconds": round(overlap, 3),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "matched_spans": len(matches),
        "reference_spans_matched": round(len(matches) / len(reference), 4) if reference else 1.0,
        "boundary_tolerance_seconds": boundary_tolerance,
        "boundary_pairs": boundaries,
        "all_matched_boundaries_within_tolerance": bool(boundaries) and all(pair["both_edges_within_tolerance"] for pair in boundaries),
    }


def load_case(path):
    payload = json.loads(Path(path).read_text())
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"Expected schema {SCHEMA!r}")
    if not isinstance(payload.get("segments"), list):
        raise ValueError("Case must contain a segments list")
    return payload


def save_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n")


def initialize_case(audio_path, output_path, title=None):
    segments = transcribe_episode(audio_path)
    payload = {
        "schema": SCHEMA,
        "episode": {"title": title or Path(audio_path).stem, "audio_path": str(Path(audio_path).resolve())},
        "production_models": {"whisper": WHISPER_MODEL, "ad_agent": AD_MODEL},
        "segments": segments,
        "reference_ads": [],
        "review_instructions": "Add reviewed start/end seconds to reference_ads after listening to the source audio.",
    }
    save_json(output_path, payload)
    return payload


def evaluate_case(case, boundary_tolerance):
    segments = case["segments"]
    duration_seconds = max((float(item["end"]) for item in segments), default=0.0)
    predicted = classify_transcript(segments, duration_seconds, complete_ad_prompt) if segments else []
    report = score(predicted or [], case.get("reference_ads") or [], boundary_tolerance)
    report.update({
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode": case.get("episode") or {},
        "production_models": {"whisper": WHISPER_MODEL, "ad_agent": AD_MODEL},
        "transcript_segment_count": len(segments),
    })
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init", help="Transcribe audio using the production MLX Whisper model and create a review case")
    init.add_argument("audio")
    init.add_argument("case")
    init.add_argument("--title")
    subcommands.add_parser("models", help="Print the exact production model identifiers used by this harness")
    run = subcommands.add_parser("run", help="Run the production Qwen ad agent and score it against reviewed timestamps")
    run.add_argument("case")
    run.add_argument("--output")
    run.add_argument("--boundary-tolerance", type=float, default=5.0)
    args = parser.parse_args()
    if args.command == "models":
        print(json.dumps({"whisper": WHISPER_MODEL, "ad_agent": AD_MODEL}, indent=2))
        return
    if args.command == "init":
        case = initialize_case(args.audio, args.case, args.title)
        print(f"Wrote {args.case} with {len(case['segments'])} production-Whisper segments.")
        print("Now add reviewed reference_ads spans before running the local Qwen evaluation.")
        return
    case = load_case(args.case)
    report = evaluate_case(case, args.boundary_tolerance)
    output = args.output or str(Path(args.case).with_suffix(".report.json"))
    save_json(output, report)
    print(json.dumps(report, indent=2))
    print(f"Report written to {output}")


if __name__ == "__main__":
    main()
