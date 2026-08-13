"""`make bench` - human-readable accuracy report over the gold set."""

from __future__ import annotations

import os

os.environ.setdefault("REDIS_ENABLED", "false")


def main() -> int:
    from brilink.nlp.ensemble import EnsembleSentimentEngine, ScoreInput

    from .gold_cases import GOLD_CASES

    engine = EnsembleSentimentEngine()
    results = engine.score_batch(
        [ScoreInput(i, body=text) for i, (text, _, _) in enumerate(GOLD_CASES)]
    )

    print(f"\nEngine mode : {engine.settings.engine_mode}")
    print(
        f"Transformer : {'available' if engine.transformer and engine.transformer.available else 'unavailable (no model loaded)'}"
    )
    print("=" * 108)
    print(f"{'OK':<3} {'expected':<9} {'predicted':<10} {'score':>7} {'conf':>6}  text")
    print("-" * 108)

    correct = 0
    for result, (text, expected, why) in zip(results, GOLD_CASES):
        hit = result.label == expected
        correct += hit
        print(
            f"{'✓' if hit else '✗':<3} {expected:<9} {result.label:<10} "
            f"{result.score:+7.3f} {result.confidence:6.2f}  {text[:58]}"
        )
        if not hit:
            print(f"{'':<3} why hard: {why}")

    accuracy = correct / len(GOLD_CASES)
    print("-" * 108)
    print(f"Accuracy: {correct}/{len(GOLD_CASES)} = {accuracy:.1%}")
    print(f"Routed to human review: {sum(1 for r in results if r.requires_review)}")
    print()
    return 0 if accuracy >= 0.75 else 1


if __name__ == "__main__":
    raise SystemExit(main())
