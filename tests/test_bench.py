from mandate.bench.harness import CONFIGS, run_config
from mandate.bench.scenarios import FAMILIES, build_scenarios
from mandate.bench.scorer import score_config, wilson


def test_scenario_set_shape_and_holdout():
    sc = build_scenarios()
    assert len(sc) == 360 == sum(n for _, n, _ in FAMILIES.values())
    assert len({s.id for s in sc}) == 360
    ho = sum(s.holdout for s in sc)
    assert 70 <= ho <= 140
    assert [s.id for s in sc] == [s.id for s in build_scenarios()]  # deterministic


def test_wilson_interval_is_sane():
    lo, hi = wilson(0, 290)
    assert lo == 0.0 and 0.0 < hi < 0.02
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi


def test_bench_ordering_and_no_harness_errors():
    sc = build_scenarios()
    asr = {}
    for name, cfg in CONFIGS.items():
        score = score_config(run_config(cfg, sc))
        assert score["errors"] == 0, name
        asr[name] = score["attacks"]["asr"]["rate"]
    assert asr["C0"] == 1.0  # a gullible agent with no checks loses every time
    assert asr["C0"] > asr["C1"] > asr["C2"] > asr["C3"] >= asr["C4"]
    assert asr["C4"] == 0.0
