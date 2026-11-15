"""Tests for `printer.py` — the fake, and the parsing of real payloads.

**This file lived in the project root until 2026-08-21** — two other agents were
inside `tests/` when it was written and the instruction was not to touch that
tree. `pytest.ini` has `testpaths = tests`, so for as long as that lasted none
of it was collected by the ordinary run, and any suite total anybody quoted
silently excluded every test here. It is in `tests/` now and collected with
everything else. `tests/conftest.py` puts the project root on `sys.path`, which
is what makes `import printer` work from in here.

The fifteen payloads in `REAL_IDLE` are not invented. They were captured off
`device/01S00A000000000/report` on 2026-08-21 in forty-five seconds of a real
idle A1, and they are here verbatim because the shape of them is the finding:
every one is a diff carrying one or two temperatures and nothing else.
"""

from __future__ import annotations

import ftplib
import io
import json
import logging
import struct
import threading
import time
import types

import pytest

import printer
from printer import (A1Printer, Config, FakePrinter, NotConfigured, PrinterError,
                     PrinterState, Snapshot, Status)

# NOT the real access code, deliberately. The real one lives in
# `_app_work/printer.json` and nowhere else — a credential checked into a test
# file is a credential in the source tree, and this file exists partly to prove
# the code never leaks it.
ACCESS_CODE = "test-access-code-9999"


# Verbatim from the real machine. Do not tidy these — the point is that they are
# what arrived, including `31` arriving as an int where its neighbours are floats.
REAL_IDLE = [
    '{"print":{"nozzle_temper":31.21875,"bed_temper":32.96875,"command":"push_status","msg":1,"sequence_id":"61"}}',
    '{"print":{"bed_temper":32.90625,"command":"push_status","msg":1,"sequence_id":"62"}}',
    '{"print":{"nozzle_temper":31.1875,"command":"push_status","msg":1,"sequence_id":"63"}}',
    '{"print":{"bed_temper":32.875,"command":"push_status","msg":1,"sequence_id":"64"}}',
    '{"print":{"nozzle_temper":31.15625,"command":"push_status","msg":1,"sequence_id":"65"}}',
    '{"print":{"bed_temper":32.84375,"command":"push_status","msg":1,"sequence_id":"66"}}',
    '{"print":{"bed_temper":32.8125,"command":"push_status","msg":1,"sequence_id":"67"}}',
    '{"print":{"nozzle_temper":31.125,"bed_temper":32.78125,"command":"push_status","msg":1,"sequence_id":"68"}}',
    '{"print":{"bed_temper":32.8125,"command":"push_status","msg":1,"sequence_id":"69"}}',
    '{"print":{"nozzle_temper":31.09375,"bed_temper":32.78125,"command":"push_status","msg":1,"sequence_id":"70"}}',
    '{"print":{"bed_temper":32.71875,"command":"push_status","msg":1,"sequence_id":"71"}}',
    '{"print":{"nozzle_temper":31.0625,"command":"push_status","msg":1,"sequence_id":"72"}}',
    '{"print":{"nozzle_temper":31.03125,"command":"push_status","msg":1,"sequence_id":"73"}}',
    '{"print":{"bed_temper":32.6875,"command":"push_status","msg":1,"sequence_id":"74"}}',
    '{"print":{"nozzle_temper":31,"bed_temper":32.65625,"command":"push_status","msg":1,"sequence_id":"75"}}',
]


class Clock:
    """Time the test drives. Nothing here ever sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def never_touch_the_real_machine_cache(tmp_path, monkeypatch):
    """No test may read or write `_app_work/printer_machine.json`.

    `start()` now asks `machine()` whether there is an AMS rather than having
    the answer typed into it, and `machine()` falls back to — and refreshes —
    that cache. Without this, a unit test's result would depend on a file
    describing the owner's actual printer, and running the suite would rewrite it.
    A test that wants a specific cache still points `MACHINE_CACHE` where it
    likes; setting it in the test body overrides this.
    """
    monkeypatch.setattr(printer, "MACHINE_CACHE", tmp_path / "machine_cache.json")


@pytest.fixture
def fake():
    clock = Clock()
    p = FakePrinter(clock=clock, speed=60.0, layers=900, minutes=1185.0,
                    jitter=False)
    p.connect()
    p.clock_handle = clock          # so a test can drive it
    return p


@pytest.fixture
def sliced(tmp_path):
    f = tmp_path / "planter.gcode.3mf"
    f.write_bytes(b"not really a 3mf" * 5000)
    return f


# --------------------------------------------------------------------------
# The real payloads
# --------------------------------------------------------------------------

class TestRealPayloads:

    def test_forty_five_real_seconds_leave_the_state_honestly_unknown(self):
        """The headline finding, pinned.

        A real idle printer never says what it is doing. If this ever starts
        returning IDLE it means somebody has decided to guess, and the screen
        will confidently say "ready" about a printer halfway through a job.
        """
        snap = Snapshot()
        for i, raw in enumerate(REAL_IDLE):
            snap.take(json.loads(raw), now=1000.0 + i * 3)

        s = snap.status(now=1000.0 + 45)
        assert s.state == PrinterState.UNKNOWN
        assert s.complete is False
        assert s.percent is None and s.layer is None and s.remaining_min is None

    def test_an_idle_real_printer_reporting_zeros_shows_no_progress_at_all(self):
        """The bug the real machine found on 2026-08-23, pinned.

        A real A1 sitting idle does not omit the progress fields — it sends
        `mc_percent: 0`, `layer_num: 0`, `total_layer_num: 0` and
        `mc_remaining_time: 0`. Passed straight through, that is "0%" and
        "0 min left" on the television for a printer that is not printing.

        `FakePrinter` returns None for all four when idle, so every screen built
        against the fake agreed with itself and was wrong about the real one.
        """
        snap = Snapshot()
        snap.take({"print": {"gcode_state": "IDLE", "mc_percent": 0,
                             "layer_num": 0, "total_layer_num": 0,
                             "mc_remaining_time": 0}}, now=1000.0)

        s = snap.status(now=1000.0)
        assert s.state == PrinterState.IDLE
        assert s.percent is None, "an idle printer is not 0% through anything"
        assert s.layer is None and s.layers is None
        assert s.remaining_min is None

    def test_a_print_that_has_only_just_started_keeps_its_real_zero(self):
        """Guards the fix against becoming a worse bug than the one it fixed.

        Blanking on the VALUE (`percent or None`) would erase this, and the
        screen would go blank for the first moments of every print.
        """
        snap = Snapshot()
        snap.take({"print": {"gcode_state": "RUNNING", "mc_percent": 0,
                             "layer_num": 0, "total_layer_num": 240,
                             "mc_remaining_time": 0}}, now=1000.0)

        s = snap.status(now=1000.0)
        assert s.state == PrinterState.PRINTING
        assert s.percent == 0, "0% of a running print is a fact, not a blank"
        assert s.layer == 0
        assert s.layers == 240

    def test_finished_and_failed_prints_keep_the_numbers_they_ended_on(self):
        """FINISHED is legitimately 100%; FAILED holds where it stopped.

        Both are states a person reads AFTER the fact, and both would be
        destroyed by blanking progress anywhere other than IDLE and UNKNOWN.
        """
        snap = Snapshot()
        snap.take({"print": {"gcode_state": "FINISH", "mc_percent": 100,
                             "layer_num": 240, "total_layer_num": 240}},
                  now=1000.0)
        s = snap.status(now=1000.0)
        assert s.state == PrinterState.FINISHED
        assert s.percent == 100 and s.layer == 240

        snap.take({"print": {"gcode_state": "FAILED", "mc_percent": 37,
                             "layer_num": 89, "total_layer_num": 240}},
                  now=1003.0)
        s = snap.status(now=1003.0)
        assert s.state == PrinterState.FAILED
        assert s.percent == 37, "a stopped print's last position is the finding"
        assert s.layer == 89

    def test_the_temperatures_accumulate_across_messages_that_omit_them(self):
        """Message 63 carries only the nozzle. The bed must not go blank.

        This is the test that would fail if `Snapshot` were replaced with
        "read the latest message" — which is what every documented example does.
        """
        snap = Snapshot()
        snap.take(json.loads(REAL_IDLE[0]), now=1000.0)     # both
        snap.take(json.loads(REAL_IDLE[2]), now=1003.0)     # nozzle only

        s = snap.status(now=1003.0)
        assert s.nozzle == pytest.approx(31.1875)
        assert s.bed == pytest.approx(32.96875), "the bed reading was dropped"

    def test_an_int_temperature_is_read_the_same_as_a_float_one(self):
        """The last real message carries `"nozzle_temper":31` — an int."""
        snap = Snapshot()
        snap.take(json.loads(REAL_IDLE[-1]), now=1000.0)
        assert snap.status(now=1000.0).nozzle == pytest.approx(31.0)

    def test_reading_a_documented_field_off_a_real_message_would_have_crashed(self):
        """States the finding as an executable fact, not a comment.

        Every published example reads `payload["print"]["gcode_state"]`. Against
        the real traffic that is a KeyError on all fifteen messages.
        """
        for raw in REAL_IDLE:
            with pytest.raises(KeyError):
                json.loads(raw)["print"]["gcode_state"]


class TestSnapshot:

    def test_a_full_report_marks_the_picture_complete(self):
        snap = Snapshot()
        snap.take({"print": {"msg": 1, "bed_temper": 30}}, now=1.0)
        assert snap.status(now=1.0).complete is False
        snap.take({"print": {"msg": 0, "gcode_state": "IDLE"}}, now=2.0)
        assert snap.status(now=2.0).complete is True
        assert snap.status(now=2.0).state == PrinterState.IDLE

    def test_a_message_with_no_msg_field_is_treated_as_a_diff(self):
        """The safe direction: it may say "unknown" for longer, and the other
        way round claims a complete picture nobody has."""
        snap = Snapshot()
        snap.take({"print": {"gcode_state": "IDLE"}}, now=1.0)
        assert snap.status(now=1.0).complete is False

    def test_nested_objects_merge_rather_than_replace(self):
        """A report can carry one tray of `ams` and nothing else."""
        snap = Snapshot()
        snap.take({"print": {"msg": 0, "ams": {"tray_now": "0", "humidity": "4"}}},
                  now=1.0)
        snap.take({"print": {"msg": 1, "ams": {"humidity": "5"}}}, now=2.0)
        ams = snap.raw()["ams"]
        assert ams == {"tray_now": "0", "humidity": "5"}, "a flat update lost tray_now"

    def test_a_fault_list_can_become_empty_again(self):
        """Lists are replaced, not merged.

        Merged element-wise, `hms` could never shrink — so a fault would stay on
        the television after the printer had cleared it.
        """
        snap = Snapshot()
        snap.take({"print": {"msg": 0, "hms": [{"attr": "1", "code": "2"}]}}, now=1.0)
        assert snap.status(now=1.0).errors == ("HMS 0000_0001_0000_0002",)
        snap.take({"print": {"msg": 1, "hms": []}}, now=2.0)
        assert snap.status(now=2.0).errors == ()

    def test_messages_that_are_not_status_are_ignored(self):
        """`device/<serial>/report` also carries `info` and `system` messages,
        and their keys overlap. Merging them into the status corrupts it."""
        snap = Snapshot()
        snap.take({"info": {"command": "get_version", "module": [{"name": "ota"}]}},
                  now=1.0)
        assert snap.raw() == {}
        assert snap.status(now=1.0).state == PrinterState.CONNECTING

    def test_silence_becomes_offline_rather_than_a_frozen_number(self):
        snap = Snapshot()
        snap.take(json.loads(REAL_IDLE[0]), now=1000.0)
        assert snap.status(now=1020.0).state == PrinterState.UNKNOWN
        late = snap.status(now=1000.0 + printer.SILENCE_IS_OFFLINE + 1)
        assert late.state == PrinterState.OFFLINE
        assert "31s" in late.detail

    def test_a_quiet_printer_on_a_live_link_is_not_called_offline(self):
        """Measured on the real A1, 2026-09-14: paused mid-job with a
        print_error, it published nothing for minutes at a time while the MQTT
        link stayed up and paho logged not one disconnect. Thirty seconds of
        that produced ten false "I can't see the printer" alerts in ten
        minutes, each one tagging the owner's phone. Silence on a live link is a
        quiet printer, not a missing one."""
        snap = Snapshot()
        snap.take(json.loads(REAL_IDLE[0]), now=1000.0)
        quiet = 1000.0 + printer.SILENCE_IS_OFFLINE + 1

        assert snap.status(now=quiet, connected=True).state != PrinterState.OFFLINE
        assert snap.status(now=quiet, connected=False).state == PrinterState.OFFLINE
        assert snap.status(now=quiet).state == PrinterState.OFFLINE, \
            "not knowing whether the link is up keeps the old, cautious rule"

        # A zombie socket still gets noticed, just far later.
        dead = 1000.0 + printer.QUIET_IS_OFFLINE + 1
        assert snap.status(now=dead, connected=True).state == PrinterState.OFFLINE
        assert printer.QUIET_IS_OFFLINE > printer.SILENCE_IS_OFFLINE

    def test_before_anything_arrives_it_says_connecting_not_idle(self):
        assert Snapshot().status(now=5.0).state == PrinterState.CONNECTING

    def test_a_diff_for_a_field_never_seen_before_is_simply_learned(self):
        """The first time `gcode_state` is ever mentioned may well be in a DIFF.

        There is no "unknown key" to reject and nothing to reconcile against —
        the snapshot has no schema, so a field arriving late is learned rather
        than dropped. Worth pinning because the tempting alternative, validating
        diffs against the last full report, would silently discard exactly the
        message that tells us a print has started.
        """
        snap = Snapshot()
        snap.take(json.loads(REAL_IDLE[0]), now=1000.0)
        assert snap.status(now=1000.0).state == PrinterState.UNKNOWN

        snap.take({"print": {"msg": 1, "gcode_state": "RUNNING",
                             "mc_percent": 3}}, now=1003.0)
        s = snap.status(now=1003.0)
        assert s.state == PrinterState.PRINTING
        assert s.percent == 3
        # ...and it is still not a complete picture, because no full report has
        # ever arrived. Knowing one new field is not knowing everything.
        assert s.complete is False
        assert s.layers is None

    def test_an_unrecognised_gcode_state_is_unknown_rather_than_a_guess(self):
        """Firmware adds states. `UNKNOWN` is honest; picking the nearest
        neighbour puts a confident wrong word on the television."""
        snap = Snapshot()
        snap.take({"print": {"msg": 0, "gcode_state": "SOMETHING_NEW"}}, now=1.0)
        assert snap.status(now=1.0).state == PrinterState.UNKNOWN

    def test_the_raw_copy_cannot_be_used_to_edit_the_snapshot(self):
        snap = Snapshot()
        snap.take({"print": {"msg": 0, "bed_temper": 30}}, now=1.0)
        snap.raw()["bed_temper"] = 999
        assert snap.status(now=1.0).bed == 30


class TestStatusWording:

    def test_nothing_told_is_not_the_same_as_zero(self):
        """A percent of None is silence; a percent of 0 is "not started yet"."""
        assert Status(state=PrinterState.PRINTING).spoken() == "Printing"
        assert "0%" in Status(state=PrinterState.PRINTING, percent=0).spoken()

    def test_it_says_hours_and_minutes_the_way_a_person_does(self):
        s = Status(state=PrinterState.PRINTING, percent=12, layer=110,
                   layers=900, remaining_min=1042)
        said = s.spoken()
        assert "17 hours 22 min left" in said
        assert "layer 110 of 900" in said
        assert "1 hours" not in said

    def test_one_hour_is_singular(self):
        assert "1 hour 5 min left" in Status(state=PrinterState.PRINTING,
                                             remaining_min=65).spoken()

    def test_no_state_ever_renders_as_a_sentence_about_a_different_state(self):
        """`CONNECTING` fell through the printing branch and said **"Paused"**.

        It lasts about a second every time the application starts, so the
        television said a false thing about the machine on every launch. Found by
        running the module against the real printer and reading its output — no
        assertion in this file was pointed at it. Every state is checked now, so
        adding one and forgetting the wording is a failing test.
        """
        for name in dir(PrinterState):
            if name.isupper() and isinstance(getattr(PrinterState, name), str):
                said = Status(state=getattr(PrinterState, name)).spoken()
                assert said, f"{name} says nothing"
                if name not in ("PAUSED",):
                    assert "Paused" not in said, f"{name} renders as Paused"
                if name not in ("PRINTING",):
                    assert not said.startswith("Printing"), f"{name} renders as Printing"

    def test_an_unconfigured_printer_reads_as_a_state_not_an_error(self):
        assert Status(state=PrinterState.NOT_CONFIGURED).spoken() == "No printer set up yet"


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

class TestCredentials:

    def test_a_blank_config_is_created_the_first_time(self, tmp_path):
        path = tmp_path / "printer.json"
        cfg = printer.load_config(path)
        assert path.is_file()
        assert cfg.configured is False
        assert json.loads(path.read_text(encoding="utf-8"))["access_code"] == ""

    def test_the_access_code_is_never_in_the_repr(self):
        cfg = Config(host="1.2.3.4", serial="S", access_code=ACCESS_CODE)
        assert ACCESS_CODE not in repr(cfg)
        assert "access_code=<set>" in repr(cfg)

    def test_a_printer_with_no_serial_is_not_configured(self):
        """The serial is the one that is allowed to be missing, and it is also
        the one whose absence is invisible on the wire: the broker GRANTS a
        wildcard subscription and then delivers nothing."""
        cfg = Config(host="1.2.3.4", serial="", access_code=ACCESS_CODE)
        assert cfg.configured is False
        s = A1Printer(cfg).status()
        assert s.state == PrinterState.NOT_CONFIGURED
        assert "serial number" in s.detail

    def test_every_command_refuses_honestly_with_no_printer_configured(self):
        """`NotConfigured` and not the interlock: "there is no printer at all"
        is a truer thing to say than "printing is switched off", so it is said
        first. The order is fixed in `_require_ready`."""
        p = A1Printer(Config())
        for call in (lambda: p.connect(),
                     lambda: p.start("x.gcode.3mf"),
                     lambda: p.pause(),
                     lambda: p.resume(),
                     lambda: p.stop(),
                     lambda: p.send("x.gcode.3mf")):
            with pytest.raises(NotConfigured):
                call()

    def test_status_still_answers_with_no_printer_configured(self):
        """`status()` never raises — it is on a polling path behind a screen."""
        assert A1Printer(Config()).status().state == PrinterState.NOT_CONFIGURED

    def test_a_corrupt_config_does_not_take_the_application_down(self, tmp_path):
        path = tmp_path / "printer.json"
        path.write_text("{ not json", encoding="utf-8")
        assert printer.load_config(path).configured is False

    def test_a_config_holding_a_list_is_refused_rather_than_used(self, tmp_path):
        path = tmp_path / "printer.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert printer.load_config(path).configured is False

    def test_scrub_removes_the_code_from_arbitrary_text(self):
        text = f"530 Login incorrect for bblp:{ACCESS_CODE}"
        assert ACCESS_CODE not in printer.scrub(text, ACCESS_CODE)
        assert "********" in printer.scrub(text, ACCESS_CODE)

    def test_scrub_with_no_secret_does_not_turn_the_text_into_confetti(self):
        """`"abc".replace("", "*")` is `*a*b*c*`. An unconfigured printer would
        otherwise make every log line in this module unreadable."""
        printer._CONFIG_CACHE.clear()
        assert printer.scrub("hello", "") == "hello"

    def test_the_access_code_reaches_neither_the_log_nor_the_exception(self, caplog):
        """The one that matters. Drive a real failure and read everything it
        wrote — a test that only checked the code's own log calls would pass
        against a library that quoted the credential back."""
        cfg = Config(host="127.0.0.1", serial="S", access_code=ACCESS_CODE)
        printer._CONFIG_CACHE["access_code"] = ACCESS_CODE
        p = A1Printer(cfg)
        with caplog.at_level(logging.DEBUG, logger="foundry"):
            with pytest.raises(PrinterError) as caught:
                # Port 990 on localhost: nothing is listening, so the login path
                # fails for real rather than being mocked into failing.
                p.send(__file__)
        assert ACCESS_CODE not in str(caught.value)
        assert ACCESS_CODE not in caplog.text


# --------------------------------------------------------------------------
# The fake
# --------------------------------------------------------------------------

class TestFake:

    def test_a_freshly_connected_fake_knows_nothing_until_it_asks(self, fake):
        """**This test used to assert the opposite, and it was wrong.**

        It read `test_it_starts_idle_and_knows_it` and pinned `IDLE` /
        `complete is True` at the instant of connecting — which is exactly what
        the real machine does NOT do, and what `FakePrinter.connect()`'s own
        comment said it was avoiding while doing it. A screen built against that
        never renders "not been told yet", and that state is the entire point of
        the sparse-diff finding: a client that has just connected must not say
        "Ready" about a printer it has never heard from.
        """
        s = fake.status()
        assert s.state == PrinterState.UNKNOWN
        assert s.complete is False
        assert s.detail == "the printer has not sent a full report yet"
        # Temperatures DO arrive from the first message, as on the real machine.
        assert s.nozzle is not None and s.bed is not None

    def test_asking_for_everything_is_what_makes_it_ready(self, fake):
        assert fake.ask_for_everything() is True
        s = fake.status()
        assert s.state == PrinterState.IDLE
        assert s.complete is True
        assert s.spoken() == "Ready"

    def test_a_job_has_to_be_sent_before_it_can_be_started(self, fake):
        with pytest.raises(PrinterError, match="has not been sent"):
            fake.start("planter.gcode.3mf")

    def test_sending_a_file_that_is_not_there_is_refused(self, fake, tmp_path):
        with pytest.raises(PrinterError, match="no file"):
            fake.send(tmp_path / "nope.gcode.3mf")

    def test_the_upload_calls_back_more_than_once(self, fake, sliced):
        """A screen with a progress bar needs more than one callback to be
        exercised at all."""
        seen = []
        fake.send(sliced, on_progress=seen.append)
        assert len(seen) > 1
        assert sum(len(b) for b in seen) == sliced.stat().st_size

    def test_a_whole_job_runs_layer_by_layer(self, fake, sliced):
        clock = fake.clock_handle
        name = fake.send(sliced)
        fake.start(name, job_name="planter")

        assert fake.status().state == PrinterState.PREPARING
        assert fake.status().spoken() == "Getting ready"

        clock.advance(5)                       # 5 min of print time at speed 60
        s = fake.status()
        assert s.state == PrinterState.PRINTING
        assert s.job == "planter"
        assert s.layers == 900

        # Watched continuously, and asserted on every reading — not inside an
        # `if`, which is how two tests in this project passed while making zero
        # assertions.
        readings = []
        for _ in range(20):
            clock.advance(60)                  # an hour of print time
            s = fake.status()
            if s.state == PrinterState.FINISHED:
                break
            assert s.state == PrinterState.PRINTING
            assert s.percent is not None and s.layer is not None
            readings.append((s.percent, s.layer, s.remaining_min))
        assert len(readings) >= 5

        percents = [r[0] for r in readings]
        layers = [r[1] for r in readings]
        remaining = [r[2] for r in readings]
        assert percents == sorted(percents) and percents[-1] > percents[0]
        assert layers == sorted(layers) and layers[-1] > layers[0]
        assert remaining == sorted(remaining, reverse=True)

    def test_it_finishes_at_a_hundred_percent_on_the_last_layer(self, fake, sliced):
        clock = fake.clock_handle
        fake.start(fake.send(sliced))
        clock.advance(2000)
        s = fake.status()
        assert s.state == PrinterState.FINISHED
        assert s.percent == 100
        assert s.layer == 900
        assert s.remaining_min == 0

    def test_pausing_freezes_the_progress_and_resuming_does_not_jump(self, fake, sliced):
        """The bug this is here for: if paused time counted, the time remaining
        would leap backwards the moment she reloaded the filament."""
        clock = fake.clock_handle
        fake.start(fake.send(sliced))
        clock.advance(300)
        fake.pause()
        before = fake.status()
        assert before.state == PrinterState.PAUSED

        clock.advance(600)                     # ten hours of print time, paused
        during = fake.status()
        assert during.state == PrinterState.PAUSED
        assert during.percent == before.percent
        assert during.remaining_min == before.remaining_min

        fake.resume()
        after = fake.status()
        assert after.state == PrinterState.PRINTING
        assert after.percent == before.percent

    def test_a_paused_printer_does_not_look_like_one_that_has_gone_away(self, fake, sliced):
        """Found by the test above, and worth pinning on its own.

        The fake only pushed when something changed, so a paused job with still
        temperatures emitted nothing — and `Snapshot`, correctly, called that
        silence OFFLINE after thirty seconds. A real paused A1 keeps sampling.
        """
        clock = fake.clock_handle
        fake.start(fake.send(sliced))
        clock.advance(60)
        fake.pause()
        for _ in range(6):
            clock.advance(printer.SILENCE_IS_OFFLINE)
            assert fake.status().state == PrinterState.PAUSED

    def test_a_filament_runout_pauses_and_reports_a_fault(self, fake, sliced):
        clock = fake.clock_handle
        fake.start(fake.send(sliced))
        clock.advance(300)
        fake.run_out_of_filament()
        s = fake.status()
        assert s.state == PrinterState.PAUSED
        assert s.errors, "a runout with no fault reported is invisible"
        assert s.spoken().startswith("Paused")

        fake.resume()
        assert fake.status().errors == (), "the fault stayed after it was fixed"

    def test_a_real_failure_stops_it_and_says_so(self, fake, sliced):
        clock = fake.clock_handle
        fake.start(fake.send(sliced))
        clock.advance(300)
        fake.break_it()
        s = fake.status()
        assert s.state == PrinterState.FAILED
        assert s.errors
        assert "stopped" in s.spoken()

    def test_stopping_a_print_ends_it(self, fake, sliced):
        clock = fake.clock_handle
        fake.start(fake.send(sliced))
        clock.advance(300)
        fake.stop()
        assert fake.status().state == PrinterState.FAILED
        assert ("stop", None) in fake.commands

    def test_stopping_when_nothing_is_printing_is_refused(self, fake):
        with pytest.raises(PrinterError, match="not printing"):
            fake.stop()

    def test_a_second_job_cannot_be_started_over_a_running_one(self, fake, sliced):
        fake.clock_handle.advance(5)
        fake.start(fake.send(sliced))
        fake.clock_handle.advance(5)
        with pytest.raises(PrinterError, match="already printing"):
            fake.start(fake.sent[0])

    def test_going_quiet_is_not_the_same_as_disconnecting(self, fake, sliced):
        """The failure a screen never gets tested against: the socket is fine
        and the messages simply stop."""
        clock = fake.clock_handle
        fake.start(fake.send(sliced))
        clock.advance(10)
        assert fake.status().state == PrinterState.PRINTING

        fake.go_offline()
        clock.advance(printer.SILENCE_IS_OFFLINE + 1)
        assert fake.status().state == PrinterState.OFFLINE

        fake.come_back()
        assert fake.status().state == PrinterState.PRINTING

    def test_closing_it_reports_offline(self, fake):
        fake.close()
        assert fake.status().state == PrinterState.OFFLINE

    def test_the_temperatures_move_while_it_heats(self, fake, sliced):
        clock = fake.clock_handle
        fake.start(fake.send(sliced))
        cold = fake.status().nozzle
        clock.advance(0.75)                    # halfway through the warm-up
        warming = fake.status().nozzle
        clock.advance(5)
        hot = fake.status()
        assert cold < warming < hot.nozzle
        assert hot.nozzle == hot.nozzle_target == 220.0

    def test_it_really_does_emit_sparse_diffs(self, fake, sliced):
        """The claim the fake's whole value rests on.

        If it pushed a full report every time, the screen would be built against
        a stream the real printer does not produce — which is precisely the trap
        SPEC names. Captured at the transport, not asserted from the docstring.
        """
        pushed = []
        original = fake.snapshot.take
        fake.snapshot.take = lambda payload, now=None: (
            pushed.append(payload["print"]), original(payload, now))

        clock = fake.clock_handle
        fake.start(fake.send(sliced))
        for _ in range(10):
            clock.advance(30)
            fake.status()

        diffs = [p for p in pushed if p.get("msg") == 1]
        assert diffs, "the fake never sent a diff"
        assert any("gcode_state" not in d for d in diffs), \
            "every diff carried the state — the real printer's do not"
        assert all(len(d) < 10 for d in diffs), "these are not sparse"

    def test_the_fake_is_the_same_thing_the_screen_will_talk_to(self):
        """Both implementations satisfy the interface, and neither is missing a
        method the other has."""
        for name in ("connect", "close", "status", "send", "start", "pause",
                     "resume", "stop"):
            assert callable(getattr(FakePrinter(), name))
            assert callable(getattr(A1Printer(Config()), name))

    def test_two_runs_of_the_fake_agree(self):
        """Seeded on purpose: a fake that differs run to run makes a screen bug
        impossible to reproduce."""
        def run():
            clock = Clock()
            p = FakePrinter(clock=clock, speed=60.0)
            p.connect()
            clock.advance(120)
            return p.status()
        assert run() == run()


# --------------------------------------------------------------------------
# The commands the real printer would receive
# --------------------------------------------------------------------------

class FakeClient:
    """Stands in for paho. Its failure mode is an exception, never a
    fallthrough — this project has already had a mock quietly stop matching the
    code and spawn a live process from a unit test."""

    def __init__(self, rc: int = 0, grant_subscription: bool = True,
                 grant_delay: float = 0.0) -> None:
        self.published = []
        self.subscribed = []
        self.rc = rc
        # How long the SUBACK takes to come back, on another thread — which is
        # where paho delivers it. Zero is the ordinary case; a delay is how a
        # test can tell "connect() waited" apart from "connect() got lucky".
        self.grant_delay = grant_delay
        # Whether this stub answers a SUBSCRIBE with a SUBACK. A real broker
        # does; one that does not is the state `connect()` now has to survive,
        # so it is a switch rather than an assumption.
        self.grant_subscription = grant_subscription
        self.on_connect = self.on_message = self.on_disconnect = None
        self.on_subscribe = None

    def connect(self, host, port, keepalive=60):
        self.connected_to = (host, port)

    def loop_start(self):
        # The network thread, modelled. paho calls `on_connect` from its own
        # thread AFTER `loop_start()` returns, and the ORDER of that against a
        # publish from the caller's thread is the whole subscribe race — a stub
        # where the caller has to fire `on_connect` by hand cannot express it.
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)
        if not self.grant_subscription or self.on_subscribe is None:
            return
        if self.grant_delay:
            threading.Timer(self.grant_delay,
                            self.on_subscribe, (self, None, 1, [0])).start()
        else:
            self.on_subscribe(self, None, 1, [0])

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))
        return types.SimpleNamespace(rc=self.rc)

    def __getattr__(self, name):
        raise AssertionError(f"the code called client.{name}(), which this stub "
                             f"does not model — update the stub deliberately")


@pytest.fixture
def wired():
    """Connected, and INTERLOCKED — the ordinary state of this application."""
    cfg = Config(host="192.168.1.50", serial="01S00A000000000",
                 access_code=ACCESS_CODE)
    client = FakeClient()
    p = A1Printer(cfg, client=client)
    p.connect()          # `loop_start()` fires on_connect, as paho does
    return p, client


@pytest.fixture
def wired_enabled():
    """The interlock deliberately open, to test the SHAPE of the commands.

    Safe, and worth saying why rather than trusting the reader: the client is
    `FakeClient`, which appends to a list. No socket is opened, nothing reaches
    192.168.1.50, and `TestTheInterlock` separately proves the default is off.
    Testing the payloads needs the interlock open; the alternative is shipping
    command shapes that nothing has ever checked.
    """
    cfg = Config(host="192.168.1.50", serial="01S00A000000000",
                 access_code=ACCESS_CODE, allow_real_prints=True,
                 allow_uploads=True)
    client = FakeClient()
    p = A1Printer(cfg, client=client)
    p.connect()          # `loop_start()` fires on_connect, as paho does
    return p, client


class TestRealCommands:

    def test_it_subscribes_to_the_exact_serial_and_never_a_wildcard(self, wired):
        """Verified against the real broker: `device/+/report` is accepted,
        granted, and then delivers nothing at all — indistinguishable from a
        printer that has gone quiet."""
        _, client = wired
        assert client.subscribed == ["device/01S00A000000000/report"]
        assert "+" not in client.subscribed[0]

    def test_a_message_off_the_wire_reaches_the_status(self, wired):
        p, client = wired
        for raw in REAL_IDLE:
            p._on_message(client, None,
                          types.SimpleNamespace(topic="t", payload=raw.encode()))
        assert p.status().nozzle == pytest.approx(31.0)

    def test_an_unreadable_message_does_not_kill_the_network_thread(self, wired):
        """It would stop every future message, with no error anywhere visible."""
        p, client = wired
        p._on_message(client, None,
                      types.SimpleNamespace(topic="t", payload=b"\xff not json"))
        p._on_message(client, None,
                      types.SimpleNamespace(topic="t", payload=REAL_IDLE[0].encode()))
        assert p.status().nozzle is not None

    def test_stop_publishes_the_documented_command_to_the_request_topic(self, wired):
        p, client = wired
        p.stop()
        topic, payload = client.published[-1]
        assert topic == "device/01S00A000000000/request"
        assert payload["print"]["command"] == "stop"

    def test_pause_and_resume_are_distinct_commands(self, wired_enabled):
        p, client = wired_enabled
        p.pause()
        p.resume()
        assert [c[1]["print"]["command"] for c in client.published] == ["pause", "resume"]

    def test_every_command_carries_its_own_sequence_id(self, wired_enabled):
        p, client = wired_enabled
        p.pause()
        p.resume()
        p.stop()
        ids = [c[1]["print"]["sequence_id"] for c in client.published]
        assert len(set(ids)) == 3

    def test_starting_names_the_file_and_does_not_use_an_ams(self, wired_enabled):
        p, client = wired_enabled
        p.start("planter.gcode.3mf", job_name="planter")
        payload = client.published[-1][1]["print"]
        assert payload["command"] == "project_file"
        assert payload["url"] == "file:///planter.gcode.3mf"
        assert payload["subtask_name"] == "planter"
        assert payload["use_ams"] is False, "an A1 with no AMS must not claim one"

    def test_a_latched_error_is_cleared_before_the_start(self, wired_enabled, monkeypatch):
        """2026-09-28: the A1 held print_error 0x05004003 from a failed start
        and refused every remote start after it, twenty in a row, while the same
        file started from its touchscreen. The error is cleared first."""
        monkeypatch.setattr(printer, "CLEAR_ERROR_WAIT_S", 0.05)
        p, client = wired_enabled
        p.snapshot.take({"print": {"gcode_state": "IDLE", "print_error": 83902467,
                                   "subtask_id": "0"}})
        p.start("planter.gcode.3mf", job_name="planter")
        # The fresh reconnect (2026-11-15) adds a pushall; the print commands are unchanged.
        sent = [c[1]["print"] for c in client.published if "print" in c[1]]
        assert [c["command"] for c in sent] == ["clean_print_error", "project_file"]
        assert sent[0]["print_error"] == 83902467

    def test_no_error_means_nothing_extra_is_sent(self, wired_enabled):
        p, client = wired_enabled
        p.snapshot.take({"print": {"gcode_state": "IDLE", "print_error": 0}})
        p.start("planter.gcode.3mf")
        assert [c[1]["print"]["command"] for c in client.published
                if "print" in c[1]] == ["project_file"]

    def test_a_paused_jobs_error_is_never_cleared(self, wired_enabled):
        """A paused job's error is a question (the filament-load prompt), not
        debris from a finished one."""
        p, _ = wired_enabled
        p.snapshot.take({"print": {"gcode_state": "PAUSE", "print_error": 134184967}})
        client = p._client
        before = len(client.published)
        p._clear_latched_error()
        assert len(client.published) == before

    def test_the_url_prefix_is_configurable_because_it_is_unverified(self):
        """It has never been exercised against the real machine, and the three
        candidates differ. Making it a setting is what stops the next person
        editing the module to try one."""
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE,
                     url_prefix="file:///sdcard/", allow_real_prints=True)
        client = FakeClient()
        p = A1Printer(cfg, client=client)
        p.connect()
        p.start("x.gcode.3mf")
        assert client.published[-1][1]["print"]["url"] == "file:///sdcard/x.gcode.3mf"

    def test_a_command_that_could_not_be_queued_is_reported_as_a_failure(self):
        """`publish()` returning a bad rc is a definite failure and must not
        pass as success — a `start()` that returns quietly having done nothing
        is the exact shape of bug this project keeps finding."""
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)
        p = A1Printer(cfg, client=FakeClient(rc=4))
        p.connect()
        with pytest.raises(PrinterError, match="did not accept"):
            p.stop()

    def test_closing_disconnects_before_it_stops_the_loop(self, wired):
        """Order matters here, and the wrong order HANGS rather than erroring.

        `loop_stop()` first kills the network thread, so `disconnect()` has
        nobody left to write its packet and blocks for ever. A probe against the
        real printer sat here until it was killed at two minutes and lost the
        data it had just captured. A server shutting down would do the same.
        """
        p, client = wired
        order = []
        client.disconnect = lambda: order.append("disconnect")
        client.loop_stop = lambda: order.append("loop_stop")
        p.close()
        assert order == ["disconnect", "loop_stop"]

    def test_starting_nothing_is_refused(self, wired_enabled):
        p, _ = wired_enabled
        with pytest.raises(PrinterError, match="no file name"):
            p.start("")

    def test_commands_are_refused_before_connecting(self):
        p = A1Printer(Config(host="h", serial="S", access_code=ACCESS_CODE))
        with pytest.raises(PrinterError, match="not connected"):
            p.stop()

    def test_it_reports_offline_rather_than_guessing_before_connecting(self):
        p = A1Printer(Config(host="h", serial="S", access_code=ACCESS_CODE))
        assert p.status().state == PrinterState.OFFLINE


class TestTheInterlock:
    """The tests that stand between an agent and the owner's filament.

    the owner, 2026-08-21: do not actually print anything, he does not want filament
    wasted. Enforced by the code rather than by everyone remembering.
    """

    def test_the_default_config_cannot_start_a_print(self, tmp_path):
        """**The one that matters most in this file.**

        Not "it raises" — *nothing is transmitted*. A refusal that still put the
        command on the wire would be a test passing while the filament burned.
        """
        path = tmp_path / "printer.json"
        cfg = printer.load_config(path)          # a freshly created blank file
        assert cfg.allow_real_prints is False, "the default is not off"

        wired_cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)
        assert wired_cfg.allow_real_prints is False
        client = FakeClient()
        p = A1Printer(wired_cfg, client=client)
        p.connect()

        with pytest.raises(printer.RealPrintingDisabled):
            p.start("planter.gcode.3mf")

        assert client.published == [], "the start command was transmitted anyway"

    def test_it_refuses_loudly_and_never_quietly_does_nothing(self):
        """A silent no-op is the worst outcome: the television would show a
        print starting that is not happening."""
        p = A1Printer(Config(host="h", serial="S", access_code=ACCESS_CODE),
                      client=FakeClient())
        p.connect()
        with pytest.raises(printer.RealPrintingDisabled):
            p.start("x.gcode.3mf")

    def test_the_refusal_is_its_own_class_the_screen_can_catch(self):
        """So the screen can tell "deliberately switched off" from "the printer
        refused" without matching on wording."""
        assert issubclass(printer.RealPrintingDisabled, PrinterError)
        exc = printer.RealPrintingDisabled("start a print", "allow_real_prints")
        assert exc.setting == "allow_real_prints"
        assert exc.path == printer.CONFIG_PATH

    def test_the_refusal_names_the_setting_and_a_way_out(self):
        """Whoever turns it on for the first real print must know exactly what
        they are enabling and where."""
        exc = printer.RealPrintingDisabled("start a print", "allow_real_prints")
        assert "allow_real_prints" in str(exc)
        assert "printer.json" in str(exc)
        assert "interlock" not in str(exc).lower(), "not a word for the television"

    def test_uploading_is_refused_too_while_it_is_unproven(self):
        p = A1Printer(Config(host="h", serial="S", access_code=ACCESS_CODE))
        with pytest.raises(printer.RealPrintingDisabled) as caught:
            p.send(__file__)
        assert caught.value.setting == "allow_uploads"

    def test_the_two_flags_are_independent(self):
        """Proving the upload is inert must not require opening the print
        interlock."""
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE,
                     allow_uploads=True)
        client = FakeClient()
        p = A1Printer(cfg, client=client)
        p.connect()
        with pytest.raises(printer.RealPrintingDisabled) as caught:
            p.start("x.gcode.3mf")
        assert caught.value.setting == "allow_real_prints"
        assert client.published == []

    def test_only_a_real_json_true_opens_it(self, tmp_path):
        """"false", "no", 0 and a typo are all a person trying to say off — and
        every non-empty string is truthy."""
        for value in ("false", "no", 0, "", None, "TRUE", 1):
            path = tmp_path / f"printer-{value!r}.json"
            path.write_text(json.dumps({"allow_real_prints": value}), encoding="utf-8")
            assert printer.load_config(path).allow_real_prints is False, value

        path = tmp_path / "yes.json"
        path.write_text(json.dumps({"allow_real_prints": True}), encoding="utf-8")
        assert printer.load_config(path).allow_real_prints is True

    def test_stopping_a_print_is_never_interlocked(self):
        """**The deliberate asymmetry.** A print can be started from the
        printer's own screen while FOUNDRY is watching, and halting it is the
        one thing the application must always be able to do. An interlock that
        disabled the emergency brake would be a safety feature that caused the
        accident.
        """
        client = FakeClient()
        p = A1Printer(Config(host="h", serial="S", access_code=ACCESS_CODE),
                      client=client)
        p.connect()
        p.stop()
        p.pause()
        assert [c[1]["print"]["command"] for c in client.published] == ["stop", "pause"]

    def test_resuming_is_interlocked_because_it_extrudes_again(self):
        p = A1Printer(Config(host="h", serial="S", access_code=ACCESS_CODE),
                      client=FakeClient())
        p.connect()
        with pytest.raises(printer.RealPrintingDisabled):
            p.resume()

    def test_asking_for_a_status_report_is_never_interlocked(self):
        """`pushall` makes the machine transmit, which is the same category as
        subscribing. It cannot waste a gram of filament, and an interlock that
        caught it would be drawn too wide."""
        client = FakeClient()
        p = A1Printer(Config(host="h", serial="S", access_code=ACCESS_CODE),
                      client=client)
        p.connect()
        p.ask_for_everything()
        assert client.published[-1][1]["pushing"]["command"] == "pushall"

    def test_the_fake_is_not_interlocked(self, fake, sliced):
        """Everything must be exercisable end to end with no filament — that is
        the whole point of the fake, and of the coming end-to-end test."""
        fake.start(fake.send(sliced))
        assert fake.status().state in (PrinterState.PREPARING, PrinterState.PRINTING)

    def test_reading_and_parsing_are_untouched_by_the_interlock(self, wired):
        p, client = wired
        for raw in REAL_IDLE:
            p._on_message(client, None,
                          types.SimpleNamespace(topic="t", payload=raw.encode()))
        assert p.status().nozzle is not None


def _fake_jpeg(width: int, height: int, filler: int = 40) -> bytes:
    """A byte string with a real SOF0 marker. Not a decodable image — these
    tests are about the FRAMING and the header read, not about pixels."""
    # An APP0 segment, with its length, because every marker except the
    # standalone ones carries one. The first version of this helper wrote
    # `ff d8 ff e0` straight into the SOF and had no APP0 length at all, so the
    # parser read the SOF's own `ff c0` as a 65472-byte segment length and
    # skipped off the end. The helper was wrong and the code was right — which
    # is worth a comment, because it looked exactly like the reverse.
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
    sof += b"\x00" * (17 - 2 - 1 - 4)
    return b"\xff\xd8" + app0 + sof + b"\x00" * filler + b"\xff\xd9"


class FakeSocket:
    """Hands out a scripted byte stream in small pieces, the way a real socket
    does — a framing bug that only shows up on a split read is exactly the kind
    this hides if the whole stream arrives in one `recv`."""

    def __init__(self, data: bytes, chunk: int = 7) -> None:
        self.data = data
        self.chunk = chunk
        self.pos = 0
        self.closed = False
        self.sent = b""

    def recv(self, _n):
        if self.pos >= len(self.data):
            return b""                          # the printer closed it
        out = self.data[self.pos:self.pos + self.chunk]
        self.pos += len(out)
        return out

    def sendall(self, data):
        self.sent += data

    def settimeout(self, _t):
        pass

    def close(self):
        self.closed = True


class TestCamera:
    """The transport was established against the real machine on 2026-08-21.

    It is NOT an HTTP MJPEG endpoint: TLS on 6000, an 80-byte auth packet, then
    the printer pushes 16-byte-header + JPEG frames until the socket closes.
    """

    def test_a_frame_is_read_out_of_the_pushed_stream(self):
        payload = _fake_jpeg(1536, 1080)
        stream = struct.pack("<I", len(payload)) + b"\0" * 12 + payload
        cam = printer.CameraStream("h", ACCESS_CODE)
        cam._last_asked = time.time()
        sock = FakeSocket(stream * 2)

        with pytest.raises(OSError):             # runs out of stream at the end
            cam._read_frames(sock)

        frame = cam._frame
        assert frame is not None
        assert frame.content_type == "image/jpeg"
        assert (frame.width, frame.height) == (1536, 1080)
        assert frame.number == 2, "the second frame did not replace the first"

    def test_the_real_frame_size_is_read_not_assumed(self):
        """**1536x1080, not the 1928x1088 SPEC records.** Read out of a real
        frame's SOF0 marker. Reserving 16:9 would letterbox it."""
        assert printer._jpeg_size(_fake_jpeg(1536, 1080)) == (1536, 1080)

    def test_a_frame_that_is_not_a_whole_jpeg_is_refused(self):
        """Better to reconnect than to hand the television half a picture."""
        payload = b"\xff\xd8" + b"\x00" * 30       # no EOI
        stream = struct.pack("<I", len(payload)) + b"\0" * 12 + payload
        cam = printer.CameraStream("h", ACCESS_CODE)
        cam._last_asked = time.time()
        with pytest.raises(ValueError, match="whole JPEG"):
            cam._read_frames(FakeSocket(stream))

    def test_an_implausible_length_bails_out_rather_than_resyncing(self):
        """Hunting for the next `ff d8` finds one INSIDE a JPEG and produces a
        permanently corrupt picture. Reconnecting is the honest response."""
        stream = struct.pack("<I", 999_999_999) + b"\0" * 12
        cam = printer.CameraStream("h", ACCESS_CODE)
        cam._last_asked = time.time()
        with pytest.raises(ValueError, match="implausible frame length"):
            cam._read_frames(FakeSocket(stream))

    def test_the_auth_packet_is_eighty_bytes_and_hides_the_code(self):
        cam = printer.CameraStream("h", ACCESS_CODE)
        packet = cam._auth_packet()
        assert len(packet) == 80
        assert struct.unpack("<IIII", packet[:16]) == (0x40, 0x3000, 0, 0)
        assert packet[16:20] == b"bblp"
        # The code IS in the packet — it has to be, it is the password. What
        # must never happen is it reaching a log or a screen.
        assert ACCESS_CODE.encode() in packet
        assert ACCESS_CODE not in printer.scrub(f"sent {packet!r}", ACCESS_CODE)

    def test_the_stream_stops_when_nobody_is_watching(self):
        """It costs a continuous ~18 KB/s and she is only watching while the
        print screen is open."""
        payload = _fake_jpeg(64, 48)
        stream = (struct.pack("<I", len(payload)) + b"\0" * 12 + payload) * 50
        cam = printer.CameraStream("h", ACCESS_CODE)
        cam._last_asked = time.time() - (printer.CAMERA_IDLE_TIMEOUT + 1)
        sock = FakeSocket(stream)
        cam._read_frames(sock)                   # returns, does not raise
        assert sock.pos == 0, "it kept reading with nobody watching"

    def test_no_camera_on_an_unconfigured_printer(self):
        assert A1Printer(Config()).camera_frame() is None

    def test_the_fake_draws_a_picture_that_moves(self, fake, sliced):
        """A still picture and a dead pane look identical on a television."""
        fake.start(fake.send(sliced))
        first = fake.camera_frame()
        assert first is not None
        assert first.content_type == "image/png"
        assert first.data[:8] == b"\x89PNG\r\n\x1a\n"

        fake.clock_handle.advance(120)
        second = fake.camera_frame()
        assert second.data != first.data, "the picture never changed"
        assert second.number > first.number

    def test_the_fakes_picture_is_the_real_ones_shape(self):
        """So a layout built against the fake fits the real frame without
        reflowing. 1536/1080 to four places."""
        f = FakePrinter()
        f.connect()
        frame = f.camera_frame()
        assert round(frame.width / frame.height, 4) == round(1536 / 1080, 4)

    def test_no_picture_while_it_is_offline_or_closed(self, fake):
        fake.go_offline()
        assert fake.camera_frame() is None
        fake.come_back()
        assert fake.camera_frame() is not None
        fake.close()
        assert fake.camera_frame() is None

    def test_the_hand_written_png_is_a_valid_one(self):
        """It is hand-written because there is no image library here, so its
        structure is checked rather than trusted."""
        import zlib
        data = printer._png(4, 3, bytearray(range(4 * 3 * 3)))
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        pos, kinds = 8, []
        while pos < len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            kind = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + length]
            crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
            assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, f"bad CRC on {kind}"
            kinds.append(kind)
            pos += 12 + length
        assert kinds == [b"IHDR", b"IDAT", b"IEND"]
        assert struct.unpack(">II", data[16:24]) == (4, 3)


class TestWhatTheScreenRelicesOn:
    """Guarantees `screen` asked for explicitly, made executable.

    A promise in a message is a promise nobody can check later. These are the
    same promises, in a form that goes red if they stop being true.
    """

    def test_no_field_on_a_status_can_ever_carry_the_access_code(self):
        """`screen` serialises `Status` wholesale with `asdict()` and puts it on
        a television. It asked for this stated; stating it as a test is better.

        `detail` is the risk — it is free text — so this drives the paths that
        actually write it rather than checking a fixed string.
        """
        import dataclasses

        printer._CONFIG_CACHE["access_code"] = ACCESS_CODE
        statuses = []

        # Every branch of the real client that produces a detail.
        statuses.append(A1Printer(Config()).status())                # not configured
        statuses.append(A1Printer(Config(host="h", serial="S",
                                         access_code=ACCESS_CODE)).status())
        unreachable = printer.open_printer(
            Config(host="127.0.0.1", serial="S", access_code=ACCESS_CODE))
        statuses.append(unreachable.status())                        # a real failure

        snap = Snapshot()
        statuses.append(snap.status(now=1.0))                        # connecting
        snap.take(json.loads(REAL_IDLE[0]), now=1000.0)
        statuses.append(snap.status(now=1000.0))                     # unknown
        statuses.append(snap.status(now=9999.0))                     # offline

        for s in statuses:
            blob = json.dumps(dataclasses.asdict(s), default=str) + s.spoken()
            assert ACCESS_CODE not in blob, f"leaked in {s.state}"

    def test_the_real_printer_cannot_be_asked_to_break_itself(self):
        """Empty by construction, not by a branch — so the server needs no
        `isinstance(printer, FakePrinter)`."""
        assert A1Printer(Config()).handles == {}

    def test_the_fake_offers_exactly_the_four_handles(self, fake):
        assert set(fake.handles) == {"break_it", "run_out_of_filament",
                                     "go_offline", "come_back"}
        assert all(callable(h) for h in fake.handles.values())

    def test_a_handle_reached_by_name_really_drives_the_fake(self, fake, sliced):
        """The point of `handles` is that the server can call one without
        knowing which class it holds."""
        fake.start(fake.send(sliced))
        fake.clock_handle.advance(60)
        fake.handles["break_it"]()
        assert fake.status().state == PrinterState.FAILED

    def test_camera_frame_is_on_both_implementations(self):
        for p in (A1Printer(Config()), FakePrinter()):
            assert callable(p.camera_frame)

    def test_status_is_serialisable_wholesale(self, fake, sliced):
        """`asdict()` plus `spoken`, `printing`, `busy` — exactly what `screen`
        said it would send to the page, including `None` staying `null`."""
        import dataclasses

        fake.start(fake.send(sliced))
        fake.clock_handle.advance(120)
        s = fake.status()
        payload = dataclasses.asdict(s)
        payload.update(spoken=s.spoken(), printing=s.printing, busy=s.busy)
        text = json.dumps(payload)
        assert json.loads(text)["percent"] == s.percent

        idle = Status(state=PrinterState.IDLE)
        assert json.loads(json.dumps(dataclasses.asdict(idle)))["percent"] is None


#: The machine's own account of itself, captured verbatim from a real `pushall`
#: on 2026-08-21. Trimmed to the fields `machine_from_report` reads, plus the
#: two that must NOT be read.
REAL_MACHINE_REPORT = {
    "nozzle_diameter": "0.4",
    "nozzle_type": "stainless_steel",
    "sdcard": True,
    "wifi_signal": "-35dBm",
    "ipcam": {"ipcam_dev": "1", "ipcam_record": "enable", "resolution": "1080p"},
    "ams": {"ams": [], "ams_exist_bits": "0", "tray_now": "255"},
    "hw_switch_state": 1,
    "vt_tray": {"tray_type": "", "tray_info_idx": "", "remain": 0},
}


class TestMachineFacts:
    """the owner: "all values cannot be hard coded any more. It has to be printer
    based if appropriate." These are the ones the machine genuinely knows."""

    def test_the_real_report_is_read_into_machine_facts(self):
        m = printer.machine_from_report(REAL_MACHINE_REPORT)
        assert m.nozzle_mm == 0.4
        assert m.nozzle_type == "stainless_steel"
        assert m.has_ams is False
        assert m.has_sdcard is True
        assert m.camera_resolution == "1080p"
        assert m.known is True and m.stale is False

    def test_no_ams_is_read_from_the_bitmask_not_from_the_empty_list(self):
        """`ams: []` is also empty on a machine whose AMS has not reported yet.
        `ams_exist_bits` is the field that actually answers it."""
        m = printer.machine_from_report(
            {"ams": {"ams": [], "ams_exist_bits": "1"}})
        assert m.has_ams is True

    def test_an_unreported_fact_is_none_and_not_a_default(self):
        """A machine nobody has reached must not present as an A1 with a
        0.0 mm nozzle."""
        m = printer.machine_from_report({})
        assert m.nozzle_mm is None
        assert m.has_ams is None
        assert m.has_sdcard is None

    def test_filament_presence_is_deliberately_not_read(self):
        """**The refusal this test used to pin was lifted — deliberately.**

        The old version asserted `filament_loaded` did not exist, "so that
        adding the guess later is a deliberate act rather than an afternoon's
        convenience." That act happened on 2026-09-06: the owner confirmed the
        starter spool physically loaded while `hw_switch_state` read `1` —
        see SPEC's "VERIFIED — the filament sensor's polarity on THIS machine".
        It is no longer a guess; it is the one observed mapping.

        What this now pins: the REAL machine report reads as filament present.
        The never-guess arm (0, absent, junk values) is pinned in
        `tests/test_filament_warning.py`.
        """
        m = printer.machine_from_report(REAL_MACHINE_REPORT)
        assert m.filament_loaded is True

    def test_the_build_volume_is_not_read_because_it_is_not_reported(self):
        """**Sixty-four fields in a real full report and no bed size in any of
        them.** The bed is a fact about the model and lives in Bambu Studio's
        machine profile. Reading it "from the printer" would be inventing a
        value and calling it measured.
        """
        m = printer.machine_from_report(REAL_MACHINE_REPORT)
        for field in printer.Machine.__dataclass_fields__:
            assert "bed" not in field and "volume" not in field
            assert "printable" not in field and "height" not in field

    def test_the_facts_survive_the_printer_being_asleep(self, tmp_path, monkeypatch):
        """She may open FOUNDRY at any time. The design screen must not wait on
        a machine that is off."""
        cache = tmp_path / "printer_machine.json"
        monkeypatch.setattr(printer, "MACHINE_CACHE", cache)

        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)
        live = A1Printer(cfg, client=FakeClient())
        live.connect()
        live.snapshot.take({"print": dict(REAL_MACHINE_REPORT, msg=0)}, now=1.0)
        fresh = live.machine()
        assert fresh.stale is False and fresh.nozzle_mm == 0.4
        assert cache.is_file()

        asleep = A1Printer(Config(host="h", serial="S", access_code=ACCESS_CODE))
        remembered = asleep.machine()
        assert remembered.nozzle_mm == 0.4
        assert remembered.stale is True, "old facts must say they are old"
        assert remembered.age_hours() is not None

    def test_with_no_cache_and_no_printer_nothing_is_claimed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(printer, "MACHINE_CACHE", tmp_path / "nothing.json")
        m = A1Printer(Config()).machine()
        assert m.known is False
        assert m.nozzle_mm is None
        assert m.age_hours() is None

    def test_a_corrupt_cache_claims_nothing_rather_than_crashing(self, tmp_path, monkeypatch):
        cache = tmp_path / "printer_machine.json"
        cache.write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(printer, "MACHINE_CACHE", cache)
        assert A1Printer(Config()).machine().known is False

    def test_the_facts_need_a_full_report_not_a_diff(self, tmp_path, monkeypatch):
        """They live only in `msg: 0`. Reading them off a diff would report
        whatever half-picture had accumulated."""
        monkeypatch.setattr(printer, "MACHINE_CACHE", tmp_path / "m.json")
        p = A1Printer(Config(host="h", serial="S", access_code=ACCESS_CODE),
                      client=FakeClient())
        p.connect()
        p.snapshot.take({"print": {"msg": 1, "nozzle_diameter": "0.4"}}, now=1.0)
        assert p.machine().known is False

    def test_the_fake_reports_the_same_shape_as_the_real_one(self):
        m = FakePrinter().machine()
        real = printer.machine_from_report(REAL_MACHINE_REPORT)
        assert m.nozzle_mm == real.nozzle_mm
        assert m.has_ams == real.has_ams
        assert m.camera_resolution == real.camera_resolution

    def test_use_ams_false_is_now_verified_rather_than_assumed(self):
        """`ams_exist_bits: "0"` on the real machine — there is no AMS, so
        `start()` claiming one would be wrong."""
        assert printer.machine_from_report(REAL_MACHINE_REPORT).has_ams is False


class TestOpenPrinter:

    def test_it_always_returns_a_printer_even_with_nothing_configured(self, tmp_path):
        """`None` would push an `if printer is not None` into every caller, and
        the branch nobody writes is the one where the screen shows nothing."""
        p = printer.open_printer(Config())
        assert isinstance(p, A1Printer)
        assert p.status().state == PrinterState.NOT_CONFIGURED

    def test_kind_fake_gives_a_connected_fake_that_already_knows_what_it_is(self):
        """`open_printer()` asks for a full report itself, so a caller gets a
        printer that knows what it is doing rather than one that has to be
        prompted. `app/server.py` never prompted it, which against the real
        machine meant a screen stuck on "unknown" for ever."""
        p = printer.open_printer(Config(kind="fake"))
        assert isinstance(p, FakePrinter)
        assert p.status().state == PrinterState.IDLE
        assert p.status().complete is True

    def test_an_unreachable_printer_comes_back_offline_rather_than_raising(self):
        """The server has to end up holding a printer object either way."""
        cfg = Config(host="127.0.0.1", serial="S", access_code=ACCESS_CODE)
        p = printer.open_printer(cfg)
        assert p.status().state == PrinterState.OFFLINE


# --------------------------------------------------------------------------
# The two bugs the real machine exposed on 2026-08-21
# --------------------------------------------------------------------------

class FakeFtpServer:
    """The printer's FTPS server, as far as `send()` can tell.

    It exists to reproduce ONE thing faithfully: a `STOR` whose bytes all land
    and which then fails on the way out. That is what really happened — the
    transfer completed and `ftplib` timed out waiting for the `226` — and the
    old `send()` called it a total failure while the file sat complete on the
    printer.
    """

    def __init__(self) -> None:
        self.files: dict = {}           # name -> size, as the printer holds them
        self.store_error = None         # raised by STOR, AFTER the bytes land
        self.store_lands = True         # whether the bytes land at all
        self.truncate_to = None         # land a SHORT file, as a half-failed STOR does
        self.fail_from_connection = None    # 1-based; refuse this connection onward
        self.timeouts: list = []        # the timeout each connection was opened with

    @property
    def connections(self) -> int:
        return len(self.timeouts)

    def install(self, monkeypatch):
        server = self

        class _Ftp:
            def cwd(self, directory):
                pass

            def storbinary(self, cmd, fh, blocksize=8192, callback=None):
                name = cmd.split(" ", 1)[1]
                data = fh.read()
                if callback is not None:
                    callback(data)
                if server.store_lands:
                    size = len(data) if server.truncate_to is None else server.truncate_to
                    server.files[name] = size
                if server.store_error is not None:
                    raise server.store_error

            def size(self, name):
                if name not in server.files:
                    # Verified against the real printer: an absent name answers
                    # `error_perm 550`, which is a definite no.
                    raise ftplib.error_perm("550 File not found")
                return server.files[name]

        class _CM:
            def __init__(self, config, timeout=printer.FTPS_CONTROL_TIMEOUT):
                server.timeouts.append(timeout)

            def __enter__(self):
                if (server.fail_from_connection is not None
                        and server.connections >= server.fail_from_connection):
                    raise OSError("the printer is not answering")
                return _Ftp()

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(printer, "_ftps", _CM)
        return self


@pytest.fixture
def uploader():
    """An A1 with the upload flag open and no socket anywhere near it."""
    cfg = Config(host="192.168.1.50", serial="01S00A000000000",
                 access_code=ACCESS_CODE, allow_uploads=True)
    client = FakeClient()
    p = A1Printer(cfg, client=client)
    p.connect()
    return p


class TestSendTellsTheTruthAboutWhatLanded:
    """2026-08-21: `send()` raised `PrinterError: could not send the file:
    TimeoutError` while `plate_1.gcode.3mf` was sitting on the printer at all
    999,701 bytes. A caller that retries on that error uploads a megabyte that
    is already there, and a screen that reports it tells the owner the opposite of
    what happened."""

    def test_a_transfer_error_on_a_file_that_landed_whole_is_reported_as_sent(
            self, uploader, sliced, monkeypatch):
        server = FakeFtpServer().install(monkeypatch)
        server.store_error = TimeoutError("The read operation timed out")

        assert uploader.send(sliced) == sliced.name, (
            "the file was complete on the printer and send() called it a failure")

    def test_it_checks_over_a_FRESH_connection(self, uploader, sliced, monkeypatch):
        """The old code's size check sat inside the same `try` as the transfer,
        so the one thing that would have caught this never ran."""
        server = FakeFtpServer().install(monkeypatch)
        server.store_error = TimeoutError("The read operation timed out")

        uploader.send(sliced)
        assert server.connections == 2, (
            "the verification has to be a new connection — the old one is the "
            "one that just timed out")

    def test_a_transfer_error_with_nothing_on_the_printer_is_still_a_failure(
            self, uploader, sliced, monkeypatch):
        server = FakeFtpServer().install(monkeypatch)
        server.store_lands = False
        server.store_error = TimeoutError("The read operation timed out")

        with pytest.raises(PrinterError) as caught:
            uploader.send(sliced)
        assert "no " + sliced.name in str(caught.value)

    def test_a_short_file_is_a_failure_even_when_the_transfer_reported_success(
            self, uploader, sliced, monkeypatch):
        server = FakeFtpServer().install(monkeypatch)
        server.truncate_to = 12

        with pytest.raises(PrinterError, match="did not upload completely"):
            uploader.send(sliced)

    def test_it_never_claims_success_when_it_could_not_find_out(
            self, uploader, sliced, monkeypatch):
        """Unreachable is not the same as absent, and must not be reported as
        either success or a clean failure."""
        server = FakeFtpServer().install(monkeypatch)
        server.store_error = TimeoutError("The read operation timed out")
        server.fail_from_connection = 2      # the verification cannot get through

        with pytest.raises(PrinterError, match="could not.*find out"):
            uploader.send(sliced)

    def test_the_interlock_still_shuts_before_any_connection_is_opened(
            self, sliced, monkeypatch):
        server = FakeFtpServer().install(monkeypatch)
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)   # both flags off
        p = A1Printer(cfg, client=FakeClient())
        p.connect()

        with pytest.raises(printer.RealPrintingDisabled):
            p.send(sliced)
        assert server.connections == 0, "it reached the network before refusing"


class TestTheTransferTimeout:
    """30 s was not enough for a megabyte, because the wait is not the transfer
    — it is the printer finalising a write to its SD card. Measured on the same
    machine the same day: a 23,565,397-byte RETR ran at 226 KB/s and its `226`
    arrived in 0.00 s, so the link is not the slow part."""

    def test_it_scales_with_the_file_and_is_bigger_than_the_control_timeout(
            self, uploader, sliced, monkeypatch):
        server = FakeFtpServer().install(monkeypatch)
        uploader.send(sliced)

        expected = printer._transfer_timeout(sliced.stat().st_size)
        assert server.timeouts[0] == expected
        assert expected > printer.FTPS_CONTROL_TIMEOUT

    def test_a_megabyte_gets_comfortably_more_than_the_30s_that_failed(self):
        assert printer._transfer_timeout(999_701) > 60.0

    def test_it_is_capped_so_a_hung_transfer_cannot_wait_for_ever(self):
        assert printer._transfer_timeout(10 ** 12) == printer.FTPS_TRANSFER_TIMEOUT_CAP

    def test_the_ordinary_control_connection_keeps_the_short_timeout(
            self, uploader, sliced, monkeypatch):
        server = FakeFtpServer().install(monkeypatch)
        server.store_error = TimeoutError("timed out")
        uploader.send(sliced)
        assert server.timeouts[1] == printer.FTPS_CONTROL_TIMEOUT, (
            "asking the printer what it has must not inherit the upload's budget")


class TestTheSubscribeRace:
    """Why the full report never arrived, twice, and why it was blamed on the
    printer. Measured 2026-08-21: with the subscription granted the printer
    answers `pushall` in 70 ms with 64 fields, first time, every time. What was
    wrong was asking before the SUBACK — the answer is published to a topic
    nobody is listening to and is never repeated."""

    def test_connect_does_not_return_until_the_subscription_is_granted(self):
        """The SUBACK arrives LATE and on another thread, which is the only
        shape in which this bug exists at all. A stub that grants it
        synchronously lets a `connect()` with no wait in it pass — measured:
        that exact mutation survived until this test was written this way."""
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)
        client = FakeClient(grant_delay=0.2)
        p = A1Printer(cfg, client=client)

        started = time.time()
        p.connect()
        assert p._subscribed.is_set(), (
            "connect() returned while the printer was not yet delivering to us")
        assert time.time() - started >= 0.2, "it did not actually wait"
        assert client.subscribed == ["device/S/report"]

    def test_asking_before_the_suback_refuses_instead_of_going_unheard(
            self, monkeypatch):
        monkeypatch.setattr(printer, "SUBSCRIBE_TIMEOUT", 0.05)
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)
        client = FakeClient(grant_subscription=False)
        p = A1Printer(cfg, client=client)
        p.connect()

        with pytest.raises(PrinterError, match="not subscribed"):
            p.ask_for_everything()
        assert client.published == [], (
            "it published a request whose answer could not reach it")

    def test_losing_the_link_takes_the_subscription_with_it(self):
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)
        client = FakeClient()
        p = A1Printer(cfg, client=client)
        p.connect()
        p._on_disconnect(client, None)
        assert not p._subscribed.is_set(), (
            "a reconnect re-subscribes; anything asked in the gap is lost")

    def test_stopping_is_never_blocked_by_the_subscription(self, monkeypatch):
        """The emergency brake does not need a reply and must not wait for one.
        A print can be started from the printer's own screen."""
        monkeypatch.setattr(printer, "SUBSCRIBE_TIMEOUT", 0.05)
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)
        client = FakeClient(grant_subscription=False)
        p = A1Printer(cfg, client=client)
        p.connect()

        p.stop()
        assert client.published[0][1]["print"]["command"] == "stop"

    def test_it_reports_whether_a_full_report_actually_arrived(self):
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)
        client = FakeClient()
        p = A1Printer(cfg, client=client)
        p.connect()

        assert p.ask_for_everything() is False, (
            "nothing had answered yet — False is the honest reading")
        p.snapshot.take({"print": {"msg": 0, "gcode_state": "IDLE"}})
        assert p.ask_for_everything() is True


class TestUseAmsComesFromTheMachine:
    """`use_ams` was `False` with a comment saying "an A1 with no AMS; an A1
    Combo needs True" — a fact about the owner's hardware typed into the code, when
    the printer states it in every full report. Bolting an AMS on would have
    meant editing that line, and nothing would have said so: the print would
    just have run from the external spool."""

    def _wired(self, report=None):
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE,
                     allow_real_prints=True)
        client = FakeClient()
        p = A1Printer(cfg, client=client)
        p.connect()
        if report is not None:
            p.snapshot.take({"print": dict(report, msg=0)})
        return p, client

    def test_a_machine_that_reports_an_ams_is_told_to_use_it(self):
        p, client = self._wired({"ams": {"ams_exist_bits": "1"}})
        p.start("x.gcode.3mf")
        assert client.published[-1][1]["print"]["use_ams"] is True

    def test_a_machine_that_reports_none_is_not(self):
        """`ams_exist_bits: "0"` — what this printer actually reports."""
        p, client = self._wired({"ams": {"ams_exist_bits": "0"}})
        p.start("x.gcode.3mf")
        assert client.published[-1][1]["print"]["use_ams"] is False

    def test_it_is_read_and_not_typed(self):
        """The two above would both pass against a hardcoded value if only one
        of them existed. This states the actual claim: the same code, given two
        different machines, sends two different answers."""
        p_none, c_none = self._wired({"ams": {"ams_exist_bits": "0"}})
        p_some, c_some = self._wired({"ams": {"ams_exist_bits": "4"}})
        p_none.start("x.gcode.3mf")
        p_some.start("x.gcode.3mf")
        assert (c_none.published[-1][1]["print"]["use_ams"]
                != c_some.published[-1][1]["print"]["use_ams"])

    def test_with_no_full_report_it_assumes_no_ams_and_says_so(self, caplog):
        """Unknown is not True. Telling a machine with no AMS to use one fails
        the print outright, and this is the state a freshly connected client is
        in — see the sparse-diff finding."""
        p, client = self._wired()
        with caplog.at_level(logging.WARNING):
            p.start("x.gcode.3mf")
        assert client.published[-1][1]["print"]["use_ams"] is False
        assert "assuming" in caplog.text and "AMS" in caplog.text


class TestTheFakeAndTheRealOneAgree:
    """A fake is only worth having where it diverges from the real machine in
    ways nobody notices. These pin the three places it did.

    All three were found by asking a specific question — "what will the real
    printer do to this screen that the fake will not?" — rather than by a test
    failing.
    """

    def test_both_start_out_knowing_nothing(self):
        """The divergence that mattered most: the fake reported
        `state=idle, complete=True` at the instant of connecting, and the real
        machine reports nothing until asked. A screen built on the first says
        "Ready" about a printer it has never heard from."""
        f = FakePrinter()
        f.connect()
        cfg = Config(host="h", serial="S", access_code=ACCESS_CODE)
        real = A1Printer(cfg, client=FakeClient())
        real.connect()

        # The invariant that matters, and it holds on both: nothing is known,
        # and neither one claims to be idle.
        assert f.status().complete is False
        assert real.status().complete is False
        assert f.status().state != PrinterState.IDLE
        assert real.status().state != PrinterState.IDLE

        # One difference that is REAL rather than a fake bug, written down so
        # the next person does not "fix" it: the real client reports CONNECTING
        # — "nothing received at all yet" — for the moment between connecting
        # and the first message, roughly 3 s on the real machine. The fake
        # delivers its first temperature diff immediately, so it goes straight
        # to UNKNOWN and **never renders CONNECTING**. A screen must handle both;
        # only one of them is reachable by driving the fake.
        assert real.status().state == PrinterState.CONNECTING
        assert f.status().state == PrinterState.UNKNOWN

    def test_ask_for_everything_exists_on_both_and_on_the_interface(self):
        """It lived only on `A1Printer`, so a screen built against the fake and
        then pointed at the real machine crashed on `AttributeError` — and one
        that never called it left the real printer blank for ever. Neither
        failure was discoverable from the fake."""
        assert hasattr(printer.Printer, "ask_for_everything")
        assert hasattr(FakePrinter(), "ask_for_everything")
        assert hasattr(A1Printer(Config()), "ask_for_everything")

    def test_the_fake_can_be_made_to_stall_after_the_last_byte(self, sliced):
        """The real machine goes SILENT for over 30 s finalising a megabyte to
        its SD card — no callback, no progress, indistinguishable from a hang.
        The fake returns instantly by default, so an upload bar built against it
        never stalls and the owner's first real print freezes at 100%.

        Kept small here (60 ms) so the suite does not sleep; the point is that
        the knob exists and that the silence lands AFTER the last callback."""
        f = FakePrinter(finalise_seconds=0.06)
        f.connect()

        calls = []
        started = time.time()
        f.send(sliced, on_progress=lambda block: calls.append(time.time()))
        elapsed = time.time() - started

        assert calls, "no progress was reported at all"
        assert elapsed >= 0.06, "it did not stall after the transfer"
        assert calls[-1] - started < elapsed, (
            "the silence must come AFTER the last callback, not during")

    def test_the_upload_stall_is_off_by_default_so_no_test_ever_sleeps(self, sliced):
        f = FakePrinter()
        f.connect()
        started = time.time()
        f.send(sliced)
        assert time.time() - started < 0.5


class TestTheUploadDoesNotWaitForATlsGoodbye:
    """2026-09-07: every real upload sat for the whole transfer timeout and was
    then found complete. `ftplib.FTP_TLS.storbinary` ends with `conn.unwrap()`,
    a TLS close handshake the printer never answers. `_FTPS.storbinary` must
    send the bytes, close the data socket, and read the reply — never unwrap.
    """

    class _DataConn:
        def __init__(self, log):
            self.log = log
            self.sent = b""

        def sendall(self, buf):
            self.sent += buf

        def unwrap(self):
            self.log.append("unwrap")
            raise AssertionError("storbinary waited for the TLS goodbye")

        def close(self):
            self.log.append("close")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def _ftps(self, log, conn):
        ftps = printer._FTPS.__new__(printer._FTPS)
        ftps._sock = None
        ftps.voidcmd = lambda cmd: log.append(cmd)
        ftps.transfercmd = lambda cmd, rest=None: (log.append(cmd), conn)[1]
        ftps.voidresp = lambda: (log.append("voidresp"), "226 Transfer complete")[1]
        return ftps

    def test_it_sends_everything_closes_and_reads_the_reply_without_unwrap(self):
        log, blocks = [], []
        conn = self._DataConn(log)
        ftps = self._ftps(log, conn)
        payload = b"x" * 100_000

        reply = ftps.storbinary("STOR a.gcode.3mf", io.BytesIO(payload),
                                blocksize=32768, callback=blocks.append)

        assert reply == "226 Transfer complete"
        assert conn.sent == payload
        assert b"".join(blocks) == payload, "the progress callback saw every block"
        assert log == ["TYPE I", "STOR a.gcode.3mf", "close", "voidresp"]
        assert "unwrap" not in log

    def test_the_stock_ftplib_version_really_does_unwrap(self):
        """Pin the reason the override exists: if a future Python stops
        unwrapping, the override becomes dead weight and this says so."""
        import inspect
        assert "unwrap()" in inspect.getsource(ftplib.FTP_TLS.storbinary)


# -- HMS codes in Bambu's own form (2026-09-23) ------------------------------


def test_the_real_a1s_decimal_hms_pair_reads_as_bambus_hex_code():
    """The A1 sends each half of an HMS record as a DECIMAL integer: on
    2026-09-23 it reported attr 50338304, code 131073 for most of a print.
    That is 0x03001A00 / 0x00020001, Bambu's `0300_1A00_0002_0001` — the
    form their wiki and tables are keyed on. It used to reach the screen and
    the sentinel as "HMS 50338304:131073", which nothing could look up."""
    snap = printer.Snapshot()
    snap.take({"print": {"hms": [{"attr": 50338304, "code": 131073}]}}, now=1.0)
    errors = snap.status(now=1.0).errors
    assert errors == ("HMS 0300_1A00_0002_0001",)
    assert printer.hms_meaning(errors[0]) == (
        "the nozzle is wrapped in filament, or the build plate is placed incorrectly")


def test_hms_halves_as_strings_or_hex_read_the_same():
    assert printer.hms_code("50338304", "131073") == "0300_1A00_0002_0001"
    assert printer.hms_code("0300_1a00", "0002_0001") == "0300_1A00_0002_0001"


def test_an_hms_record_that_is_not_a_pair_is_kept_raw_not_guessed():
    assert printer.hms_code("nonsense", 1) == "nonsense:1"
    assert printer.hms_code(None, None) == "None:None"


def test_an_unknown_code_has_no_meaning_and_nothing_is_invented():
    assert printer.hms_meaning("HMS 0700_2000_0002_0001") == ""
    assert printer.hms_meaning("print_error 134184967") == ""


def test_the_fake_sends_hms_the_way_the_real_a1_does():
    """Decimal integers, so anything keyed on the hex form is exercised
    against the same shape the machine sends."""
    assert printer._hms("0300_1A00_0002_0001") == {"attr": 50338304, "code": 131073}
    fake = printer.FakePrinter(layers=5, minutes=1.0)
    fake.connect()
    fake.ask_for_everything()
    fake.break_it("0300_1A00_0002_0001")
    assert fake.status().errors == ("HMS 0300_1A00_0002_0001",)


def _paused_with(p, client, code):
    msg = json.dumps({"print": {"command": "push_status", "gcode_state": "PAUSE",
                                "print_error": code, "subtask_id": "777"}})
    p._on_message(client, None, types.SimpleNamespace(topic="t", payload=msg.encode()))


def test_resume_clears_a_paused_jobs_error_first(wired_enabled, monkeypatch):
    """2026-11-14: paused on 0x03008014, plain resume x4 did nothing; clear
    then resume ran it."""
    p, client = wired_enabled
    monkeypatch.setattr(printer.time, "sleep", lambda s: None)
    _paused_with(p, client, 50364436)
    p.resume()
    sent = [c[1]["print"] for c in client.published]
    assert [c["command"] for c in sent] == ["clean_print_error", "resume"]
    assert sent[0]["print_error"] == 50364436 and sent[0]["subtask_id"] == "777"


def test_resume_never_clears_the_load_question(wired_enabled, monkeypatch):
    p, client = wired_enabled
    monkeypatch.setattr(printer.time, "sleep", lambda s: None)
    _paused_with(p, client, printer.LOAD_QUESTION_ERROR)
    p.resume()
    assert [c[1]["print"]["command"] for c in client.published] == ["resume"]




def test_start_reconnects_fresh_before_sending_the_start(wired_enabled, monkeypatch):

    """2026-11-15: since 11-11 only the first start on a fresh connection was

    taken; the reconnect must happen BEFORE project_file goes out."""

    p, client = wired_enabled

    monkeypatch.setattr(printer.time, "sleep", lambda s: None)

    events = []

    client.disconnect = lambda: events.append("disconnect")

    real_publish = client.publish

    def publish(topic, payload, qos=0):

        events.append(json.loads(payload).get("print", {}).get("command")

                      or "pushall")

        return real_publish(topic, payload, qos)

    client.publish = publish

    p.start("planter.gcode.3mf", job_name="planter")

    assert "disconnect" in events

    assert events.index("disconnect") < events.index("project_file")

    assert client.subscribed[-1] == "device/01S00A000000000/report"

