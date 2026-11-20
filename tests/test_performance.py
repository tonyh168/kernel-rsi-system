import pytest

from flaggems_rsi.performance import PerformanceModel


def test_roofline_memory_bound_and_efficiency():
    model = PerformanceModel(
        workload_id="copy", bytes_transferred=1_000_000, flops=0,
        effective_bandwidth_bytes_per_second=1_000_000_000,
    )
    assert model.memory_seconds == pytest.approx(0.001)
    assert model.compute_seconds == 0
    assert model.efficiency(0.002) == pytest.approx(0.5)


def test_compute_and_overlap_assumptions_are_explicit():
    model = PerformanceModel(
        workload_id="compute", bytes_transferred=1000, flops=1_000_000,
        effective_bandwidth_bytes_per_second=1_000_000,
        effective_flops_per_second=1_000_000_000,
        overlap_assumed=False,
        launch_overhead_seconds=0.0001,
        synchronization_seconds=0.0002,
    )
    assert model.roofline_seconds == pytest.approx(0.002)
    assert model.lower_bound_seconds == pytest.approx(0.0023)
    assert model.summary(0.0023)["efficiency"] == pytest.approx(1.0)


def test_compute_bound_requires_effective_rate_and_valid_measurement():
    model = PerformanceModel(
        workload_id="compute", bytes_transferred=1000, flops=1_000_000,
        effective_bandwidth_bytes_per_second=1_000_000,
    )
    with pytest.raises(ValueError, match="effective FLOP"):
        _ = model.compute_seconds
    with pytest.raises(ValueError, match="positive"):
        model.efficiency(0)
