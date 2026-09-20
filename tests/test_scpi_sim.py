"""Transports, mnemonic normalisation, and the behaviour of the simulated instruments."""

import numpy as np
import pytest

from labauto.drivers.chamber import StabilityCriterion
from labauto.drivers.vna import SweepSettings
from labauto.scpi import InstrumentError, normalise, short_form, split_commands
from labauto.validation import validate_network


def test_short_forms_follow_the_scpi_rule():
    assert short_form("FREQuency") == "FREQ"
    assert short_form("STARt") == "STAR"
    assert short_form("POWer") == "POW"          # 4th letter is a vowel -> 3 letters
    assert short_form("ACTivate") == "ACT"
    assert short_form("SENSe1") == "SENS"        # numeric suffix stripped
    assert normalise(":SENSe1:FREQuency:STARt?") == "SENS:FREQ:STAR?"
    assert normalise("CALC1:DATA:SNP:PORTs?") == "CALC:DATA:SNP:PORT?"
    assert split_commands('SENS:CORR:CSET:ACT "a;b",1;*OPC?') == ['SENS:CORR:CSET:ACT "a;b",1', "*OPC?"]


def _bench(lab):
    l, _ = lab
    return l.bench, l.instrument("vna"), l.instrument("chamber"), l.instrument("dmm"), l.clock


def test_vna_configure_reads_back_what_was_set(lab):
    bench, vna, *_ = _bench(lab)
    s = SweepSettings(1e6, 600e6, 1200, 1e3, 0.0, 1)
    rb = vna.configure(s)
    assert rb == s
    assert vna.identity.serial == "SIM0001"
    assert vna.capabilities.ports == 4 and vna.capabilities.fmax_hz == 8.5e9
    # the exact command strings a real analyser would get
    sent = vna.t.log
    assert any(c.startswith("SENS1:FREQ:STAR 1e+06") for c in sent)
    assert any("SENS1:BWID 1000" in c for c in sent)


def test_vna_reports_errors_through_the_queue(lab):
    bench, vna, *_ = _bench(lab)
    with pytest.raises(InstrumentError) as e:
        vna.configure(SweepSettings(1e3, 20e9, 201, 1e3, 0.0))       # both ends out of range
    assert "-222" in str(e.value) or "out of range" in str(e.value)
    assert vna.errors() == []                                          # drained
    with pytest.raises(Exception):
        vna.select_calset("does-not-exist")


def test_uncorrected_data_is_obviously_wrong_and_corrected_is_not(lab):
    bench, vna, *_ = _bench(lab)
    vna.configure(SweepSettings(1e6, 600e6, 601, 1e3, 0.0))
    bench.connect("C1000T1A-L24071-00418")
    raw = vna.measure([1, 2, 3, 4])
    assert not vna.correction_on()
    vna.select_calset(vna.calsets()[0])
    assert vna.correction_on()
    cor = vna.measure([1, 2, 3, 4])
    il_raw = -20 * np.log10(np.abs(raw.s[:, 2, 0]))
    il_cor = -20 * np.log10(np.abs(cor.s[:, 2, 0]))
    assert np.max(np.abs(il_raw - il_cor)) > 0.5                # the raw tracking/source-match ripple is gross
    # the uncorrected source-match ripple is not even reciprocal; the corrected data is
    assert raw.reciprocity_error().max() > 0.1 and cor.reciprocity_error().max() < 0.02


def test_trace_noise_scales_with_ifbw_averaging_and_power(lab):
    bench, vna, *_ = _bench(lab)
    bench.connect("C1000T1A-L24071-00418")
    vna.select_calset(vna.calsets()[0])
    entry = lab[0].registry.by_id["C1000T1A-L24071-0041"]
    checks = {"passivity_max": 0.02, "reciprocity_max_db": 0.1, "trace_noise_max_db": 0.05, "connection_min_db": -60,
              "length_tol_pct": 12, "nvp": 0.68, "il_rdc_ratio": [0.8, 4], "il_per_m_100mhz_db": [0.05, 0.6]}

    def noise(ifbw, avg, power):
        s = vna.configure(SweepSettings(1e6, 600e6, 601, ifbw, power, avg))
        net = vna.measure([1, 2, 3, 4])
        c = {x.name: x for x in validate_network(net, "A+near,A-near,A+far,A-far", s, entry, checks)}
        return c["trace_noise"].value
    n_quiet = noise(1e3, 1, 0.0)
    n_wide = noise(100e3, 1, 0.0)
    n_avg = noise(100e3, 16, 0.0)
    n_low = noise(100e3, 1, -20.0)
    # the metric has a floor from the cable's own fine structure (ripple, roughness), so ratios are modest
    assert n_wide > 2 * n_quiet
    assert n_avg < n_wide / 1.5
    assert n_low > 3 * n_wide
    assert vna.sim.noise_sigma_db() == pytest.approx(-100 + 20 + 20)


def test_sweep_time_is_spent_on_the_clock(lab):
    bench, vna, ch, dmm, clock = _bench(lab)
    vna.configure(SweepSettings(1e6, 600e6, 1200, 1e3, 0.0, 4))
    bench.connect("C1000T1A-L24071-00418")
    t0 = clock.time()
    vna.measure([1, 2, 3, 4])
    dt = clock.time() - t0
    assert dt == pytest.approx(1200 * (1e-3 + 25e-6) * 4 * 4, rel=1e-6)


def test_chamber_two_node_model_and_stability_criterion(lab):
    bench, vna, ch, dmm, clock = _bench(lab)
    assert ch.capabilities.tmin_c <= -40 and ch.capabilities.tmax_c >= 125
    ch.set_temperature(85.0)
    crit = StabilityCriterion(tol_k=0.5, stable_s=300, max_slope_k_per_min=0.2, sample_s=10, timeout_s=4 * 3600)
    t0 = clock.time()
    tlog = ch.wait_stable(85.0, crit, progress=None)
    dt = clock.time() - t0
    # ramp of 3 K/min from 23 to 85 is ~21 min, plus the air lag and the 5 min hold
    assert 22 * 60 < dt < 60 * 60
    assert abs(tlog.air_c[-1] - 85.0) <= 0.5
    assert abs(tlog.slope_k_per_min(300)) <= 0.2
    # the sample lags: not yet at 85 C when the air is
    assert bench.chamber.dut_c < 84.0
    clock.sleep(3600)
    assert abs(bench.chamber.dut_c - 85.0) < 0.5
    with pytest.raises(ValueError):
        ch.set_temperature(500.0)
    ch.set_temperature(-40.0)
    with pytest.raises(TimeoutError):
        ch.wait_stable(-40.0, StabilityCriterion(0.5, 300, 0.2, 10, timeout_s=120))


def test_dmm_reads_loop_resistance_with_temperature(lab):
    bench, vna, ch, dmm, clock = _bench(lab)
    bench.connect("C1000T1A-L24071-00418")
    r23 = dmm.resistance_4w()
    m = bench.model_for("C1000T1A-L24071-00418")
    assert r23 == pytest.approx(2 * m.r_dc * 15.0 * (1 + 0.00393 * 3), rel=0.01)
    ch.set_temperature(105.0)
    clock.sleep(3 * 3600)
    r105 = dmm.resistance_4w()
    assert r105 / r23 == pytest.approx((1 + 0.00393 * 85) / (1 + 0.00393 * 3), rel=0.01)
    bench.connect("OPEN")
    assert dmm.resistance_4w() > 1e30


def test_driver_registry_and_socket_address_parsing():
    from labauto.drivers import DRIVERS, _load_all
    from labauto.scpi import ScpiError, open_transport
    _load_all()
    for name in ("vna.scpi", "vna.sim", "chamber.scpi", "chamber.sim", "dmm.scpi", "dmm.sim", "scanner.queue", "scanner.console"):
        assert name in DRIVERS
    with pytest.raises(ScpiError):
        open_transport("sim")                       # no simulator object
    with pytest.raises((OSError, ScpiError)):
        open_transport("TCPIP::127.0.0.1::1::SOCKET", timeout=0.2)   # nothing listening
