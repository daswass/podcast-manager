"""Measure production ad cuts against manually-reviewed timestamp truth.

The live path deliberately imports the same MLX Whisper and Qwen functions used by
the nightly podcast job.  Do not substitute a cloud transcriber or a different
local model here: the point is to measure the laptop production path.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ad_detect import classify_transcript
from ad_model import AD_MODEL, complete_ad_prompt
from whisper_transcribe import WHISPER_MODEL, transcribe_episode

SCHEMA = "podcast-manager.ad-eval.v1"


def _span(item):
    if isinstance(item, dict):
        return float(item["start"]), float(item["end"])
    return float(item[0]), float(item[1])


def normalize(spans):
    """Return the true union of valid spans, including touching spans.

    Production's merge policy is intentionally independent from evaluation:
    scoring must never double-count overlapping predictions or references.
    """
    ordered = sorted(
        (_span(span) for span in spans if _span(span)[1] > _span(span)[0]),
        key=lambda span: (span[0], span[1]),
    )
    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _valid_spans(spans):
    return sorted(
        (_span(span) for span in spans if _span(span)[1] > _span(span)[0]),
        key=lambda span: (span[0], span[1]),
    )


def duration(spans):
    return sum(end - start for start, end in normalize(spans))


def intersection_seconds(left, right):
    total = 0.0
    for start, end in normalize(left):
        for other_start, other_end in normalize(right):
            total += max(0.0, min(end, other_end) - max(start, other_start))
    return total


def _match_spans(predicted, reference):
    """Maximum-cardinality, then maximum-overlap one-to-one matching."""
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def assign(pi, used_references):
        if pi == len(predicted):
            return (0, 0.0, ())
        best = assign(pi + 1, used_references)
        ps, pe = predicted[pi]
        for ri, (rs, re) in enumerate(reference):
            if used_references & (1 << ri):
                continue
            overlap = max(0.0, min(pe, re) - max(ps, rs))
            if not overlap:
                continue
            count, total, pairs = assign(pi + 1, used_references | (1 << ri))
            candidate = (count + 1, total + overlap, ((pi, ri, overlap),) + pairs)
            if (candidate[0], candidate[1]) > (best[0], best[1]) or (
                (candidate[0], candidate[1]) == (best[0], best[1])
                and candidate[2] < best[2]
            ):
                best = candidate
        return best

    return [
        (predicted[pi], reference[ri], overlap)
        for pi, ri, overlap in assign(0, 0)[2]
    ]


def score(predicted, reference, boundary_tolerance=5.0):
    individual_predicted, individual_reference = _valid_spans(predicted), _valid_spans(reference)
    predicted, reference = normalize(individual_predicted), normalize(individual_reference)
    predicted_seconds, reference_seconds = duration(predicted), duration(reference)
    overlap = intersection_seconds(predicted, reference)
    precision = overlap / predicted_seconds if predicted_seconds else (1.0 if not reference_seconds else 0.0)
    recall = overlap / reference_seconds if reference_seconds else (1.0 if not predicted_seconds else 0.0)
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    matches = _match_spans(individual_predicted, individual_reference)
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
        "reference_spans_matched": round(len(matches) / len(individual_reference), 4) if individual_reference else 1.0,
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


def evaluate_production_pipeline(case, rendered_audio_path, boundary_tolerance):
    """Run the exact production render path, not a detector-only approximation."""
    info = case.get("pipeline_info")
    if not isinstance(info, dict):
        raise ValueError("Case needs pipeline_info from the real show/episode (including chapters when available)")
    source = (case.get("episode") or {}).get("audio_path")
    if not source:
        raise ValueError("Case needs episode.audio_path")
    # Keep the evaluator importable for pure scoring tests; the production
    # renderer (and its audio dependencies) is loaded only for a real run.
    from podcastsToShokz import render_sliced_episode

    segments = transcribe_episode(source)
    trace = {}
    audio_seconds = render_sliced_episode(info, source, rendered_audio_path, segments, trace=trace)
    if trace.get("phrase_fallback_used"):
        detail = trace.get("model_failure") or "production ad model returned no usable result"
        raise RuntimeError(f"Production ad-model evaluation failed: {detail}; phrase fallback was used")
    report = score(trace["final_tightened_cut_spans"], case.get("reference_ads") or [], boundary_tolerance)
    report.update({
        "schema": SCHEMA,
        "evaluation_mode": "production_render_sliced_episode",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode": case.get("episode") or {},
        "production_models": {"whisper": WHISPER_MODEL, "ad_agent": AD_MODEL},
        "transcript_segment_count": len(segments),
        "audio_seconds": audio_seconds,
        "pipeline_trace": trace,
        "rendered_audio_path": str(Path(rendered_audio_path).resolve()),
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
    pipeline = subcommands.add_parser("pipeline", help="Run the exact production render_sliced_episode path and score final cut spans")
    pipeline.add_argument("case")
    pipeline.add_argument("rendered_audio")
    pipeline.add_argument("--output")
    pipeline.add_argument("--boundary-tolerance", type=float, default=5.0)
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
    if args.command == "pipeline":
        report = evaluate_production_pipeline(case, args.rendered_audio, args.boundary_tolerance)
        output = args.output or str(Path(args.case).with_suffix(".pipeline-report.json"))
        save_json(output, report)
        print(json.dumps(report, indent=2))
        print(f"Report written to {output}")
        return
    report = evaluate_case(case, args.boundary_tolerance)
    output = args.output or str(Path(args.case).with_suffix(".report.json"))
    save_json(output, report)
    print(json.dumps(report, indent=2))
    print(f"Report written to {output}")


if __name__ == "__main__":
    main()
