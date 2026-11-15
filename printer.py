"""The printer — sending a job to it, starting it, watching it, stopping it.

One interface, two implementations: the real Bambu Lab A1 on the LAN, and a fake
that speaks the same protocol so the monitoring screen can be built and driven
with no hardware, no filament and nobody watching a machine.

Everything below that is non-obvious was established by connecting to the real
printer on 2026-08-21 and reading what it actually sends, not by reading
documentation. Where something is still only documented and has NOT been seen on
this machine, it says so in the comment, because the difference is the whole
lesson of this project.

The one that changes how you write every client
-----------------------------------------------
**The printer publishes SPARSE DIFFS, not status objects.** Every published
example, in the vendor notes and in every library README, shows a `print` object
carrying `gcode_state`, `mc_percent`, `layer_num`, `mc_remaining_time` and the
rest. Forty-five seconds of a real idle A1 produced fifteen messages and every
one of them looked like this, in full:

    {"print":{"bed_temper":32.90625,"command":"push_status","msg":1,"sequence_id":"62"}}

Nothing else. `msg: 1` means "these are the fields that changed"; `msg: 0` means
"here is everything". A client that reads `payload["print"]["gcode_state"]`
raises `KeyError` on essentially every real message, and a client that rebuilds
its idea of the printer from each message alone watches the job vanish and come
back several times a second.

So the state is ACCUMULATED: `Snapshot` merges each message onto what it already
knows, and a field nobody has ever mentioned stays `None` — which is the honest
value for "not told", and is not the same as zero.

The second consequence is quieter and worse
-------------------------------------------
Because the interesting fields only appear in a full report, and a full report
only arrives when it is asked for (`pushall`) or when something changes,
**a client that has just connected knows nothing at all** — and an idle printer
will happily send it temperatures for ever without ever mentioning that it is
idle. `Status.complete` is False until a full report has landed, and until then
the state is `UNKNOWN` rather than `IDLE`. Guessing IDLE there is how a screen
comes to say "ready to print" about a printer that is halfway through a job.

Credentials
-----------
The access code is a credential. It lives in `_app_work/printer.json`, it is
never written to `foundry.log`, never put into an exception message, and never
appears on any `Status` the screen renders. `scrub()` is applied to everything
this module logs or raises; see the comment on it for why belt AND braces.
"""

from __future__ import annotations

import ftplib
import json
import logging
import random
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "_app_work"

# Where the credentials live. One file, outside the code, so the access code is
# never in a source file and there is exactly one thing to protect.
CONFIG_PATH = WORK / "printer.json"

log = logging.getLogger("foundry")

# The printer's own broker and file server. Fixed by the firmware, not settings.
MQTT_PORT = 8883
FTPS_PORT = 990
MQTT_USER = "bblp"

# If nothing has arrived for this long the printer is not there. A real idle A1
# sends something every ~3 s (measured: 15 messages in 45 s), so 30 s is ten
# missed heartbeats — long enough that a Wi-Fi hiccup does not flap the screen,
# short enough that she is not watching a frozen number.
SILENCE_IS_OFFLINE = 30.0

# The same silence, but with the MQTT link still up. Measured 2026-09-14 on a
# real A1 paused mid-job with a print_error: it published nothing for minutes at
# a time while staying connected, and paho logged not one disconnect. Thirty
# seconds of that produced ten "I can't see the printer" alerts in ten minutes,
# each one tagging the owner's phone, and every one of them false. A live connection
# that has gone quiet is a quiet printer, not a missing one — so this is a
# backstop for a zombie socket, not the normal way to notice silence.
QUIET_IS_OFFLINE = 600.0

#: How long `connect()` waits for the broker to GRANT the subscription before
#: giving up on it. Measured 2026-08-21: with the subscription granted the
#: printer answers `pushall` in 70 ms with 64 fields, first time, every time.
#: What was wrong was asking BEFORE the SUBACK — the answer is published to a
#: topic nobody is listening to, and the printer never repeats it. So this is
#: not a politeness delay; it is the difference between a full report and
#: silence that gets blamed on the machine.
SUBSCRIBE_TIMEOUT = 5.0

#: The ordinary control connection: logging in, asking what a file's size is.
#: These are single round trips and 30 s is generous for them.
FTPS_CONTROL_TIMEOUT = 30.0

#: A `STOR` is not a control round trip and must not share its budget. 30 s was
#: not enough for a megabyte, and the reason is NOT link speed — measured on the
#: same machine the same day, a 23,565,397-byte RETR ran at 226 KB/s and its
#: `226` came back in 0.00 s. The wait is the printer finalising a write to its
#: own SD card, which does not scale with the link at all.
#:
#: 2026-09-07 CORRECTION: the wait was NOT the SD card. It was `ftplib` waiting
#: for a TLS close handshake on the data connection that the printer never
#: sends — every upload ran to the full timeout and was then found complete.
#: `_FTPS.storbinary` now skips that handshake, so an upload takes about as long
#: as the bytes do and this budget is only the ceiling for a genuinely hung one.
#:
#: So: a flat allowance for that finalisation, plus a deliberately pessimistic
#: transfer rate — a quarter of what was measured, because the number that
#: matters is the slowest plausible one, not the observed one.
FTPS_TRANSFER_FINALISE_S = 90.0

#: How long `start()` waits for a cleared `print_error` to read 0 before
#: starting anyway. See `_clear_latched_error`.
CLEAR_ERROR_WAIT_S = 5.0
FTPS_TRANSFER_MIN_RATE = 100 * 1024         # bytes/second
#: And a ceiling, so a genuinely hung transfer eventually gives up instead of
#: holding the upload for ever.
FTPS_TRANSFER_TIMEOUT_CAP = 600.0


def _transfer_timeout(size_bytes: int) -> float:
    """How long a `STOR` of this many bytes is allowed to take."""
    return min(FTPS_TRANSFER_TIMEOUT_CAP,
               FTPS_TRANSFER_FINALISE_S + size_bytes / FTPS_TRANSFER_MIN_RATE)


class PrinterState:
    """What the screen is allowed to believe.

    Strings rather than an Enum because these cross into JSON for the screen and
    appear in the log, and an Enum member's repr in a log line is noise.
    """

    NOT_CONFIGURED = "not configured"   # no serial yet — see the module docstring
    OFFLINE = "offline"                 # configured, but nothing is answering
    CONNECTING = "connecting"
    UNKNOWN = "unknown"                 # connected, but it has not said what it is doing
    IDLE = "idle"
    PREPARING = "preparing"             # heating, levelling, calibrating
    PRINTING = "printing"
    PAUSED = "paused"
    FINISHED = "finished"
    FAILED = "failed"

    #: The states in which sending a new job is meaningless. Not a policy — a
    #: fact about the machine: it prints one thing at a time.
    BUSY = (PREPARING, PRINTING, PAUSED)


class PrinterError(Exception):
    """Something the caller asked for did not happen."""


class RealPrintingDisabled(PrinterError):
    """The safety interlock refused a command that would burn filament.

    Its own class, and not a bare `PrinterError`, because the screen has to tell
    "deliberately switched off" apart from "the printer refused" and word the two
    differently. Matching on wording would break the moment this sentence is
    edited.

    It carries `setting` and `path` so a caller can write its own sentence
    naming the way out without parsing this one. The word "interlock" is never
    for the television.
    """

    def __init__(self, doing: str, setting: str) -> None:
        self.setting = setting
        self.path = CONFIG_PATH
        super().__init__(
            f"refusing to {doing}: real printing is switched off. "
            f'Set "{setting}": true in {CONFIG_PATH} to allow it.')


class NotConfigured(PrinterError):
    """There is no printer set up yet.

    Its own class because it is not a fault: it is the ordinary state of this
    application on a machine where nobody has typed a serial number in. The
    screen says "no printer yet"; it does not say "error".
    """


@dataclass(frozen=True)
class Status:
    """Everything the monitoring screen needs, and nothing it does not.

    **`None` means "the printer has not told us", and is not zero.** A percent of
    `None` is a screen that says nothing; a percent of `0` is a screen that says
    the print has not started. Those are different and the difference has to
    survive all the way to the television, so no field here is ever defaulted to
    a number that was not received.
    """

    state: str
    detail: str = ""                    # plain language, ready for the screen
    complete: bool = False              # has a FULL report ever arrived?
    job: str = ""                       # what it is printing, in the file's own name
    percent: Optional[int] = None
    layer: Optional[int] = None
    layers: Optional[int] = None
    remaining_min: Optional[int] = None
    nozzle: Optional[float] = None
    nozzle_target: Optional[float] = None
    bed: Optional[float] = None
    bed_target: Optional[float] = None
    errors: tuple = ()                  # the printer's OWN fault reports, verbatim
    age: Optional[float] = None         # seconds since the printer last said anything

    @property
    def printing(self) -> bool:
        return self.state == PrinterState.PRINTING

    @property
    def busy(self) -> bool:
        return self.state in PrinterState.BUSY

    def spoken(self) -> str:
        """How it reads on the television.

        `slicing.Estimate.spoken()` sets the pattern: hours and minutes, no
        seconds, and nothing that needs decoding. "1 hours 56 min" was on this
        television once and it is the sentence that says nobody read it aloud.
        """
        if self.state == PrinterState.NOT_CONFIGURED:
            return "No printer set up yet"
        if self.state == PrinterState.OFFLINE:
            return "The printer isn't answering"
        if self.state == PrinterState.UNKNOWN:
            return "Connected — waiting for the printer to say what it's doing"
        if self.state == PrinterState.IDLE:
            return "Ready"
        if self.state == PrinterState.FAILED:
            return "The print stopped" + (f" — {self.errors[0]}" if self.errors else "")
        if self.state == PrinterState.FINISHED:
            return "Finished"
        if self.state == PrinterState.PREPARING:
            return "Getting ready"
        if self.state not in (PrinterState.PRINTING, PrinterState.PAUSED):
            # An explicit floor, because the fall-through below assumes one of
            # those two. Without it `CONNECTING` — a state that lasts about a
            # second every time the application starts — rendered as **"Paused"**
            # on the television, which is a screen stating something false about
            # a machine rather than saying it does not know yet. Caught by
            # running the module against the real printer and reading the output,
            # not by any assertion.
            return "Connecting to the printer"

        # Printing or paused: only say the parts it has actually told us.
        lead = "Printing" if self.printing else "Paused"
        bits = []
        if self.percent is not None:
            bits.append(f"{self.percent}%")
        if self.layer is not None and self.layers:
            bits.append(f"layer {self.layer} of {self.layers}")
        if self.remaining_min:
            h, m = divmod(int(self.remaining_min), 60)
            hours = f"{h} hour" if h == 1 else f"{h} hours"
            bits.append(f"{hours} {m} min left" if h else f"{m} min left")
        return f"{lead} · " + " · ".join(bits) if bits else lead


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

def scrub(text: str, secret: str = "") -> str:
    """Take the access code out of anything on its way to a log or a screen.

    Belt AND braces, deliberately. Every log call in this module is already
    written not to include the code — but the failures that matter here are the
    ones nobody wrote: a library putting a URL with credentials in it into an
    exception, an `ftplib` error quoting the line it sent, a traceback rendering
    a local variable. Those all end up as text, and this is applied to the text.

    The empty-secret guard is not defensive noise: `"x".replace("", "*")` puts a
    star between every character, so an unconfigured printer would turn every log
    line in this module into confetti.
    """
    out = str(text)
    for value in (secret, _CONFIG_CACHE.get("access_code", "")):
        if value:
            out = out.replace(value, "********")
    return out


# The loaded access code, kept here so `scrub()` can reach it from anywhere
# without every call site having to pass it. It is the ONE place the plaintext
# lives in this process.
_CONFIG_CACHE: dict = {}


@dataclass
class Config:
    """What it takes to reach the printer. Read from disk, never from the code.

    `serial` is the one that is allowed to be empty: on 2026-08-21 the printer
    was on the desk and reachable while nobody knew its serial number, and the
    application has to be honest about that rather than crash or pretend. Every
    other field has a real default.
    """

    host: str = ""
    serial: str = ""
    access_code: str = ""
    kind: str = "a1"                    # "a1" or "fake"
    # Where an uploaded job lands on the printer's SD card. DOCUMENTED, NOT
    # VERIFIED: the root works for Bambu Studio's own LAN send, but nothing here
    # has uploaded a file to this machine yet. If a job is refused with "file not
    # found", this and `url_prefix` are the two things to try.
    remote_dir: str = "/"
    url_prefix: str = "file:///"

    # ---- the safety interlock -------------------------------------------
    #
    # the owner, 2026-08-21: do not actually print anything, he does not want
    # filament wasted. Enforced here rather than by everyone remembering, and
    # OFF by default so the safe state is the one you get by doing nothing.
    #
    # It refuses loudly and never no-ops. A silent no-op is the worse outcome by
    # a distance: the television would show a print starting that is not
    # happening, which is this project's single most repeated bug wearing a
    # safety feature's clothes.
    allow_real_prints: bool = False
    #
    # SEPARATE, and separate on purpose. Uploading a file is a write to the
    # printer's storage over FTPS; starting a print is an MQTT command. They
    # share no transport. Almost certainly an upload cannot make the machine
    # begin — but that is an expectation and it has filament riding on it, so
    # `send()` is gated too until somebody has watched an upload land and seen
    # the printer do nothing.
    #
    # It is its own flag so that proving it does not require turning the print
    # interlock on. When the upload is proven inert, this default becomes True
    # and the print interlock is untouched.
    allow_uploads: bool = False

    @property
    def configured(self) -> bool:
        """Enough to talk to a printer at all.

        The serial is in here because without it there is nothing to subscribe
        to: the broker accepts `device/+/report`, grants the subscription, and
        then silently delivers nothing. A wildcard looks exactly like a printer
        that has gone quiet, which is the worst possible failure to design
        around, so the absence of a serial is made loud instead.
        """
        return bool(self.host and self.serial and self.access_code)

    def __repr__(self) -> str:
        """Never render the access code.

        A dataclass's generated repr puts every field in, and a repr reaches a
        log the moment anybody writes `log.info("config %s", cfg)` or an
        exception carries a local variable into a traceback.
        """
        got = "set" if self.access_code else "missing"
        return (f"Config(host={self.host!r}, serial={self.serial!r}, "
                f"access_code=<{got}>, kind={self.kind!r})")


#: Written when the file does not exist yet, so the first person to look at it
#: sees the shape rather than having to guess it.
_BLANK = {
    "host": "",
    "serial": "",
    "access_code": "",
    "kind": "a1",
    "remote_dir": "/",
    "url_prefix": "file:///",
    "allow_real_prints": False,
    "allow_uploads": False,
    "_note": ("The access code is a credential: it is shown on the printer's own "
              "screen under LAN Only mode. Nothing outside this file needs it. "
              "The serial is printed on the sticker, and is also the first field "
              "of every filename in the printer's /logger folder over FTPS."),
}


def load_config(path: Optional[Path] = None) -> Config:
    """Read the credentials, creating a blank file the first time.

    Never raises for a missing or unreadable file. A printer that cannot be
    configured is a printer that is not configured, which is a state this module
    already handles honestly — turning it into an exception would take down the
    server on a machine that has simply never had a printer.

    **`None`, resolved at CALL time. Do not "tidy" this back to
    `path: Path = CONFIG_PATH`.** A default argument is evaluated once at
    import, so that form captures the REAL `_app_work/printer.json` for the life
    of the process and the autouse `printer_config` fixture in `tests/conftest.py`
    redirects nothing — every test then reads the machine's actual access code,
    and `test_no_test_reads_the_real_printer_credentials` goes red.

    It has been tidied back once already: the fix landed 2026-08-21 and was lost
    in the rollback described in SPEC fact 29, which is how a live safety test
    ended up red for two days. `tests/conftest.py` says the same thing from the
    other side; the two comments exist because one of them was not enough.
    """
    path = CONFIG_PATH if path is None else path
    data = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("printer.json does not contain an object")
    except FileNotFoundError:
        _write_blank(path)
    except (OSError, ValueError) as exc:
        # Deliberately not fatal, and deliberately not silent. A hand-edited file
        # with a trailing comma should say so once, not every poll.
        #
        # `data` is reset here and not only in the `try`: a file holding a JSON
        # LIST parsed successfully, failed the isinstance check, was logged as
        # unreadable — and was then still used, so the next line raised
        # `AttributeError: 'list' object has no attribute 'get'` and took the
        # caller down anyway. Found by a test, not by reading it.
        data = {}
        log.warning("could not read the printer configuration: %s",
                    scrub(f"{type(exc).__name__}: {exc}"))

    cfg = Config(
        host=str(data.get("host", "") or ""),
        serial=str(data.get("serial", "") or "").strip(),
        access_code=str(data.get("access_code", "") or "").strip(),
        kind=str(data.get("kind", "a1") or "a1").strip().lower(),
        remote_dir=str(data.get("remote_dir", "/") or "/"),
        url_prefix=str(data.get("url_prefix", "file:///") or "file:///"),
        # `is True` and not truthiness. A config holding "false", "no" or 0 is a
        # person trying to say off, and every one of those is truthy as a
        # non-empty string. The interlock may only be opened by a real JSON
        # `true` — anything else, including a typo, leaves it shut.
        allow_real_prints=data.get("allow_real_prints") is True,
        allow_uploads=data.get("allow_uploads") is True,
    )
    _CONFIG_CACHE["access_code"] = cfg.access_code
    return cfg


def _write_blank(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_BLANK, indent=2), encoding="utf-8")
        log.info("wrote a blank printer configuration to %s", path)
    except OSError as exc:
        # Including ENOSPC, which this project has already been bitten by. There
        # is nothing to do about it here and nothing is lost: the printer is
        # simply not configured, which is a state that already works.
        log.warning("could not create %s: %s", path, scrub(str(exc)))


# --------------------------------------------------------------------------
# Accumulating what the printer says
# --------------------------------------------------------------------------

def _merge(into: dict, update: dict) -> None:
    """Deep-merge one report onto the accumulated picture.

    Nested rather than flat because the nested objects arrive partially too — a
    report can carry one tray of `ams` and nothing else, and `into.update()`
    would throw away every other tray. Lists are REPLACED, not merged: `hms` is
    the current fault list, and a list that merged element-wise could never
    become empty, so a fault would still be on screen after it was cleared.
    """
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


def _int(value) -> Optional[int]:
    """The printer sends numbers as strings about as often as it sends them as
    numbers — `mc_percent` has been seen both ways in the wild. Anything that
    cannot be read as a number is `None`, which is "not told", rather than 0."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: `gcode_state` -> what the screen believes.
#:
#: VERIFIED: none of them. `gcode_state` has never appeared in a message from
#: this machine's printer, because it has been idle and idle printers only send
#: temperature diffs. The values themselves are well attested across every
#: implementation, but treat this table as the first thing to check if a real
#: print shows the wrong thing on screen.
_GCODE_STATE = {
    "IDLE": PrinterState.IDLE,
    "PREPARE": PrinterState.PREPARING,
    "SLICING": PrinterState.PREPARING,
    "RUNNING": PrinterState.PRINTING,
    "PAUSE": PrinterState.PAUSED,
    "FINISH": PrinterState.FINISHED,
    "FAILED": PrinterState.FAILED,
}


class Snapshot:
    """What the printer has told us so far, merged into one picture.

    Thread-safe because the MQTT client delivers on its own network thread while
    the server reads on a request thread. The lock is around the dict, not around
    the network, so a slow reader never stalls the client's loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._print: dict = {}
        self.complete = False           # has a full report (msg 0) ever landed?
        self.last_message: Optional[float] = None

    def take(self, payload: dict, now: Optional[float] = None) -> None:
        """Fold one MQTT report in. Anything that is not a report is ignored."""
        report = payload.get("print")
        if not isinstance(report, dict):
            # `device/<serial>/report` also carries `info` (firmware versions)
            # and `system` messages. They are not status and must not be merged
            # into it — they have overlapping key names.
            return
        with self._lock:
            _merge(self._print, report)
            # msg 0 is a full report; msg 1 is a diff. A message with no `msg`
            # at all is treated as a diff, which is the safe direction: it may
            # leave us saying "unknown" for longer, and the alternative is
            # claiming a complete picture we do not have.
            if _int(report.get("msg")) == 0:
                self.complete = True
            self.last_message = time.time() if now is None else now

    def raw(self) -> dict:
        """A copy, for the report and for tests. Never the live dict."""
        with self._lock:
            return json.loads(json.dumps(self._print))

    def status(self, now: Optional[float] = None,
               connected: Optional[bool] = None) -> Status:
        """`connected` is whether the MQTT link is still up, when the caller
        knows. A live link that has gone quiet is a quiet printer, not an
        absent one, so it is given `QUIET_IS_OFFLINE` before being called
        offline instead of `SILENCE_IS_OFFLINE`."""
        with self._lock:
            p = dict(self._print)
            complete = self.complete
            last = self.last_message

        now = time.time() if now is None else now
        age = None if last is None else max(0.0, now - last)
        quiet_for = QUIET_IS_OFFLINE if connected else SILENCE_IS_OFFLINE

        if last is None:
            return Status(state=PrinterState.CONNECTING, complete=False,
                          detail="connected, nothing received yet")
        if age is not None and age > quiet_for:
            # It said something once and has stopped. Keep the last numbers —
            # they are the last true thing known — but do not let the screen
            # imply they are current.
            return Status(state=PrinterState.OFFLINE, complete=complete, age=age,
                          detail=f"nothing heard for {int(age)}s")

        raw_state = p.get("gcode_state")
        if raw_state is None:
            # Connected and talking, but it has never said what it is doing.
            # See the module docstring: this is the ordinary case for a printer
            # that has been sitting idle, and it must not be read as IDLE.
            state = PrinterState.UNKNOWN
        else:
            state = _GCODE_STATE.get(str(raw_state).upper(), PrinterState.UNKNOWN)

        # Progress numbers only mean something while there is a job to be a
        # fraction OF. A real A1 sitting idle reports `mc_percent: 0`,
        # `layer_num: 0`, `total_layer_num: 0` and `mc_remaining_time: 0` — it
        # does not omit them — so passing them straight through puts "0%" and
        # "0 min left" on the television for a printer that is not printing.
        # `FakePrinter` returns None for all four when idle, so this is
        # invisible against the fake and was only found by pointing the probe at
        # the real machine (2026-08-23).
        #
        # Blanked for IDLE and UNKNOWN ONLY. Every other state keeps its numbers
        # and must: PREPARING and a just-started PRINTING are legitimately at 0%,
        # FINISHED is at 100%, and FAILED holds the last true position of a print
        # that stopped. Blanking on value (`or None`) instead of on state would
        # erase all three of those.
        progress_is_meaningless = state in (PrinterState.IDLE, PrinterState.UNKNOWN)

        def _progress(key: str) -> Optional[int]:
            return None if progress_is_meaningless else _int(p.get(key))

        return Status(
            state=state,
            complete=complete,
            age=age,
            job=str(p.get("subtask_name") or p.get("gcode_file") or ""),
            percent=_progress("mc_percent"),
            layer=_progress("layer_num"),
            layers=_progress("total_layer_num"),
            remaining_min=_progress("mc_remaining_time"),
            nozzle=_float(p.get("nozzle_temper")),
            nozzle_target=_float(p.get("nozzle_target_temper")),
            bed=_float(p.get("bed_temper")),
            bed_target=_float(p.get("bed_target_temper")),
            errors=_faults(p),
            detail="" if complete else "the printer has not sent a full report yet",
        )


#: The A1's filament LOAD confirmation, 0x07FF8007 — the one `print_error`
#: this project has a meaning for, because it has been read out of Bambu's own
#: table AND seen on this machine nine times (SPEC facts 46, 74). It is the
#: machine asking a PERSON whether filament came out of the nozzle, on its own
#: touchscreen, which goes dark. Not a fault.
LOAD_QUESTION_ERROR = 134184967


def asks_about_the_load(errors) -> bool:
    """Whether these `Status.errors` carry the load question."""
    return any(str(e) == f"print_error {LOAD_QUESTION_ERROR}" for e in errors or ())


def _hms_half(value) -> Optional[str]:
    """One half of an HMS record as Bambu writes it: eight hex digits, split
    four and four. The real A1 sends each half as a DECIMAL integer (seen
    2026-09-23: attr 50338304, code 131073, i.e. 0x03001A00 / 0x00020001);
    an already-hex "0300_1A00" is accepted too. None if it is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    else:
        text = str(value if value is not None else "").strip()
        if "_" in text:
            digits = text.replace("_", "")
            if not digits or len(digits) > 8 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                return None
            n = int(digits, 16)
        elif text.isdigit():
            n = int(text)
        else:
            return None
    if not 0 <= n <= 0xFFFFFFFF:
        return None
    h = f"{n:08X}"
    return f"{h[:4]}_{h[4:]}"


def hms_code(attr, code) -> str:
    """An HMS record in Bambu's own form, `0300_1A00_0002_0001` — the form
    their wiki and Bambu Studio's tables are keyed on, so it can be looked
    up. Anything that is not a recognisable pair is kept raw as
    `attr:code` rather than guessed at."""
    a, c = _hms_half(attr), _hms_half(code)
    if a is None or c is None:
        return f"{attr}:{code}"
    return f"{a}_{c}"


#: What an HMS code MEANS — only codes this machine has actually reported AND
#: whose meaning Bambu has published. Never filled in from memory.
#: 0300_1A00_0002_0001: reported by this A1 from 25 % to the end of the TV
#: Camera Clamp Thread Test (2026-09-23), which finished normally. Bambu's wiki
#: page (its A1 mini section; there is no A1 page for this code) is titled
#: "The nozzle is wrapped in the filament or the build plate is placed
#: incorrectly"; six of the seven tables bundled with Bambu Studio here
#: (`tools/bambu-studio/resources/hms/hms_en_*.json`, ecode 03001A0000020001)
#: say "The nozzle is covered with filament, or the build plate is crooked",
#: the seventh "A nozzle clumping has been detected".
HMS_KNOWN = {
    "0300_1A00_0002_0001": (
        "the nozzle is wrapped in filament, or the build plate is placed incorrectly",
        "https://wiki.bambulab.com/en/a1-mini/troubleshooting/hmscode/0300_1A00_0002_0001",
    ),
}


def hms_meaning(error) -> str:
    """Bambu's published meaning of an `HMS ...` entry of `Status.errors`,
    or "" when this project has none for it (which is most of them)."""
    text = str(error)
    if not text.startswith("HMS "):
        return ""
    known = HMS_KNOWN.get(text[4:].upper())
    return known[0] if known else ""


def _faults(p: dict) -> tuple:
    """The printer's own fault reporting, passed through and NOT interpreted.

    SPEC settled this on 2026-08-20: failure detection v1 trusts what the printer
    says about itself. So these are surfaced verbatim as codes, and this module
    deliberately does not decide which of them means "out of filament, leave it
    paused" and which means "stop it now". That distinction is real and it is in
    SPEC's failure table — but the mapping from an HMS code to it has never been
    seen on this machine, and a table of hex codes nobody here has observed is
    exactly the confident guess this project keeps writing down as a mistake.
    """
    out = []
    hms = p.get("hms")
    if isinstance(hms, list):
        for entry in hms:
            if isinstance(entry, dict) and ("attr" in entry or "code" in entry):
                # The two halves are the documented shape of an HMS record.
                out.append("HMS " + hms_code(entry.get("attr"), entry.get("code")))
    error = _int(p.get("print_error"))
    if error:
        out.append(f"print_error {error}")
    return tuple(out)


def filament_trace(p: dict) -> str:
    """One log line of what the machine believes about its filament. For the
    log ONLY — nothing decides anything from it.

    Why it exists (2026-09-22): the A1 sometimes runs a filament LOAD inside the
    start G-code's `M620 S0A ... T0 ... M621 S0A` block and ends it on the
    0x07FF8007 "did filament come out?" question, and sometimes does not, with
    byte-identical start G-code and an identical start command. The difference
    is the machine's own state, which FOUNDRY never wrote down. `ams.tray_now`
    (254 = external spool loaded, 255 = nothing loaded) is the leading suspect;
    `stg_cur`/`stg` 4 is the "changing filament" stage that block claims
    (`M1002 gcode_claim_action : 4`); the 13:14 die's `stg` was
    [2, 13, 24, 4, 8, 3, 14, 1]."""
    ams = p.get("ams") if isinstance(p.get("ams"), dict) else {}
    vt = p.get("vt_tray") if isinstance(p.get("vt_tray"), dict) else {}
    bits = [f"tray_now={ams.get('tray_now', '?')}",
            f"tray_pre={ams.get('tray_pre', '?')}",
            f"tray_tar={ams.get('tray_tar', '?')}",
            f"vt_tray.type={vt.get('tray_type', '?') or '-'}",
            f"switch={p.get('hw_switch_state', '?')}",
            f"stg_cur={p.get('stg_cur', '?')}",
            f"stg={p.get('stg', '?')}",
            f"sub_stage={p.get('mc_print_sub_stage', '?')}",
            f"line={p.get('mc_print_line_number', '?')}",
            f"state={p.get('gcode_state', '?')}"]
    return " ".join(str(b) for b in bits)


# --------------------------------------------------------------------------
# The camera
# --------------------------------------------------------------------------
#
# ESTABLISHED FROM THE MACHINE on 2026-08-21, and it is NOT what the obvious
# guess would be. Port 6000 is not an HTTP endpoint and there is no MJPEG URL to
# fetch. It is:
#
#   1. a plain TCP connection wrapped in TLS (the same self-signed certificate
#      as MQTT and FTPS — ECDHE-RSA-AES256-GCM-SHA384 negotiated),
#   2. an 80-byte authentication packet: four little-endian words
#      (0x40, 0x3000, 0, 0) followed by the username and the access code, each
#      null-padded to 32 bytes,
#   3. and then the printer PUSHES frames for as long as the socket is open.
#      Nothing is ever requested. Each frame is a 16-byte header whose first
#      little-endian word is the JPEG's length, followed by exactly that many
#      bytes, starting `ff d8 ff` and ending `ff d9`.
#
# Measured: 207,938 bytes and five whole frames in twelve seconds, so roughly
# 0.4 frames per second at ~44 KB each. It is a slow stream, not a video feed.
#
# **The frame is 1536x1080, not the 1928x1088 SPEC records.** Read out of the
# SOF0 marker of a real frame. Anything reserving space on the television should
# use 1536:1080 — about 1.42:1, which is noticeably squarer than 16:9.
#
# The interface over it is a FRAME GRAB, not a stream: `camera_frame()` returns
# the most recent picture and never blocks. That is the shape the screen needs —
# it polls — and it means one socket serves any number of readers.

CAMERA_PORT = 6000
#: Stop the stream when nobody has looked for this long. The socket costs a
#: continuous ~18 KB/s, and she is only watching while the print screen is open.
#: Reopening takes about a second, which is cheaper than streaming all night.
CAMERA_IDLE_TIMEOUT = 30.0


@dataclass(frozen=True)
class Frame:
    """One picture from the printer.

    Carries its own content type because the two implementations honestly
    produce different formats: the real machine sends JPEG, and the fake draws
    PNG — PNG being the one image format that can be written correctly in twenty
    lines with nothing installed, and this project has no image library. The
    server sets the header from this field rather than assuming.
    """

    data: bytes
    content_type: str
    width: int
    height: int
    at: float
    number: int


def _png(width: int, height: int, pixels: bytearray) -> bytes:
    """A minimal, valid, truecolour PNG. `pixels` is RGB, row-major.

    Hand-written because there is no image library in this virtualenv and adding
    one to draw a test pattern would be a dependency for a fake. PNG is chosen
    over JPEG for exactly this reason: it is losslessly simple to emit correctly,
    where a hand-rolled JPEG encoder is a bug farm.
    """
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)                                   # filter: None
        start = y * width * 3
        raw += pixels[start:start + width * 3]

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


class CameraStream:
    """Holds the printer's camera socket and keeps the latest frame.

    A background thread, because the transport is a push stream and the screen
    is a poller: something has to be draining the socket between requests or the
    frames back up and every picture she sees is stale by however long she
    waited.

    Never raises at the caller. A camera that cannot be reached is a camera that
    has no picture, which the screen already has to handle — a printer with no
    camera is a real configuration.
    """

    def __init__(self, host: str, access_code: str,
                 on_open=None, on_close=None) -> None:
        self.host = host
        self._code = access_code
        #: Called when the stream opens / lets go: the printer switches its
        #: light on for a watcher and off again when nobody is looking.
        #: Both are guarded — a light that will not answer must not take
        #: the camera down with it.
        self._on_open = on_open
        self._on_close = on_close
        self._lock = threading.Lock()
        self._frame: Optional[Frame] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_asked = 0.0
        self._count = 0
        self.trouble = ""

    # -- what the printer calls -------------------------------------------

    def frame(self) -> Optional[Frame]:
        """The most recent picture, or None. Never blocks, never raises."""
        self._last_asked = time.time()
        self._ensure_running()
        with self._lock:
            return self._frame

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None

    # -- the thread -------------------------------------------------------

    def _say(self, callback) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:                      # noqa: BLE001
            log.warning("camera: a light callback failed: %s: %s", type(exc).__name__, exc)

    def _ensure_running(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def _auth_packet(self) -> bytes:
        return (struct.pack("<IIII", 0x40, 0x3000, 0, 0)
                + MQTT_USER.encode().ljust(32, b"\0")
                + self._code.encode().ljust(32, b"\0"))

    def _run(self) -> None:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            raw = socket.create_connection((self.host, CAMERA_PORT), timeout=10)
            sock = context.wrap_socket(raw, server_hostname=self.host)
            sock.sendall(self._auth_packet())
            sock.settimeout(10)
            self.trouble = ""
            self._say(self._on_open)
        except (OSError, ssl.SSLError) as exc:
            # Scrubbed: an SSL or socket error can quote what it was given.
            self.trouble = scrub(f"no camera: {type(exc).__name__}")
            log.info("printer: %s", self.trouble)
            return

        try:
            self._read_frames(sock)
        except (OSError, ssl.SSLError, ValueError) as exc:
            self.trouble = scrub(f"camera stopped: {type(exc).__name__}")
            log.info("printer: %s", self.trouble)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _read_frames(self, sock) -> None:
        buf = b""
        while not self._stop.is_set():
            # Nobody has asked for a picture in a while — let the socket go.
            # Checked here rather than on a timer so it costs nothing.
            if time.time() - self._last_asked > CAMERA_IDLE_TIMEOUT:
                log.info("printer: closing the camera, nobody is watching")
                self._say(self._on_close)
                return

            while len(buf) < 16:
                buf += self._recv(sock)
            length = struct.unpack("<I", buf[:4])[0]
            if not 0 < length <= 8 * 1024 * 1024:
                # The stream has lost its place. Bailing out and reconnecting is
                # the honest response — hunting for the next `ff d8` inside a
                # buffer that may contain one *inside* a JPEG is how a resync
                # turns into a permanently corrupt picture.
                raise ValueError(f"implausible frame length {length}")

            while len(buf) < 16 + length:
                buf += self._recv(sock)
            payload, buf = buf[16:16 + length], buf[16 + length:]

            if not (payload[:2] == b"\xff\xd8" and payload[-2:] == b"\xff\xd9"):
                raise ValueError("frame was not a whole JPEG")

            self._count += 1
            width, height = _jpeg_size(payload)
            with self._lock:
                self._frame = Frame(data=payload, content_type="image/jpeg",
                                    width=width, height=height,
                                    at=time.time(), number=self._count)

    def _recv(self, sock) -> bytes:
        chunk = sock.recv(65536)
        if not chunk:
            raise OSError("the printer closed the camera connection")
        return chunk


def _jpeg_size(data: bytes) -> tuple:
    """Width and height out of the SOF marker, or (0, 0).

    Read rather than assumed: the real frames are **1536x1080**, where SPEC
    records the A1's camera as 1928x1088. Reserving 16:9 for a 1.42:1 picture
    would letterbox it on the television.
    """
    i = 2
    try:
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return width, height
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except (struct.error, IndexError):
        pass
    return 0, 0


# --------------------------------------------------------------------------
# What the machine says about itself
# --------------------------------------------------------------------------
#
# the owner, 2026-08-21: "all values cannot be hard coded any more. It has to be
# printer based if appropriate." This is the "if appropriate" half — the facts
# the machine genuinely knows about itself, read from it rather than typed in.
#
# **And the finding that shapes what belongs here: the A1 does NOT report its
# build volume.** Sixty-four fields in a full report and there is no printable
# area, no bed dimension, no build height anywhere in it. The bed size is a fact
# about the MODEL, and it lives in Bambu Studio's machine profile
# (`printable_height: 256`), not on the wire. Anything wanting the bed should
# read the profile through `slicing._resolve`, and putting it here would be
# inventing a value and calling it measured.

#: Where the last-seen machine facts are kept, so the design screen works with
#: the printer asleep. She may open FOUNDRY at any time; blocking a design on a
#: machine that is off would be a worse product than showing slightly old facts
#: and saying they are old.
MACHINE_CACHE = WORK / "printer_machine.json"


@dataclass(frozen=True)
class Machine:
    """The facts the printer reports about itself.

    Every field is Optional for the same reason every `Status` field is: not
    told is not zero, and a machine nobody has ever reached must not present as
    an A1 with a 0.0 mm nozzle.
    """

    nozzle_mm: Optional[float] = None
    nozzle_type: str = ""
    has_ams: Optional[bool] = None
    has_sdcard: Optional[bool] = None
    camera_resolution: str = ""
    wifi: str = ""
    # The filament switch, tri-state like everything else here: True on
    # `hw_switch_state: 1`, False on 0, None when the machine has not said.
    # Polarity is a VERIFIED fact about this A1, not a reading of the docs —
    # see SPEC "VERIFIED — the filament sensor's polarity on THIS machine".
    filament_loaded: Optional[bool] = None
    # What the machine BELIEVES is on the external spool — `vt_tray.tray_type`,
    # "PLA"/"PETG"/..., "" when unconfigured or not told. Read back so the
    # screen can say whether a set_filament actually took (2026-09-11).
    spool_type: str = ""
    seen_at: Optional[float] = None     # when these were last read from the machine
    stale: bool = False                 # True when they came from the cache

    @property
    def known(self) -> bool:
        return self.seen_at is not None

    def age_hours(self) -> Optional[float]:
        if self.seen_at is None:
            return None
        return max(0.0, (time.time() - self.seen_at) / 3600.0)


def machine_from_report(report: dict) -> Machine:
    """Read the machine's own account of itself out of a full status report.

    Deliberately narrow. Only fields whose meaning is unambiguous are taken.

    `hw_switch_state` IS read now — the refusal that used to stand here was
    "its polarity has never been established", and it has been: SPEC's
    "VERIFIED — the filament sensor's polarity on THIS machine (2026-09-06)"
    records `1` observed live alongside a physically loaded spool, with the owner
    at the machine. The apparent contradiction that justified the refusal —
    `1` beside an all-zero `vt_tray` — dissolved with it: the tray metadata
    was zeros because the spool was never CONFIGURED on the printer's screen,
    so `vt_tray` means "unconfigured", never "empty". Presence comes from the
    switch only. `0` = absent is still inference (nobody has yet seen the
    sensor with nothing in it), and anything that is not exactly 1 or 0
    stays None — never guessed.
    """
    ams = report.get("ams")
    has_ams = None
    if isinstance(ams, dict):
        # `ams_exist_bits` is a bitmask of which AMS units are attached; "0" is
        # none. Verified on this machine: no AMS, and it reads "0".
        bits = str(ams.get("ams_exist_bits", "")).strip()
        if bits:
            has_ams = bits not in ("0", "0x0", "00")

    ipcam = report.get("ipcam")
    resolution = ""
    if isinstance(ipcam, dict):
        resolution = str(ipcam.get("resolution", "") or "")

    # Strictly 1 or 0, whether the machine sends it as an int or a string;
    # everything else — absent, empty, some future enum — is "not told".
    switch = str(report.get("hw_switch_state", "")).strip()
    filament = True if switch == "1" else False if switch == "0" else None

    # The external spool's configured type. A string the machine sent, or
    # "" — never a guess from the switch, which only knows presence.
    vt_tray = report.get("vt_tray")
    spool_type = ""
    if isinstance(vt_tray, dict):
        spool_type = str(vt_tray.get("tray_type", "") or "").strip().upper()

    return Machine(
        nozzle_mm=_float(report.get("nozzle_diameter")),
        nozzle_type=str(report.get("nozzle_type", "") or ""),
        has_ams=has_ams,
        has_sdcard=report.get("sdcard") if isinstance(report.get("sdcard"), bool) else None,
        camera_resolution=resolution,
        wifi=str(report.get("wifi_signal", "") or ""),
        filament_loaded=filament,
        spool_type=spool_type,
        seen_at=time.time(),
        stale=False,
    )


def _load_cached_machine() -> Machine:
    try:
        data = json.loads(MACHINE_CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return Machine()
        data.pop("stale", None)
        return Machine(stale=True, **{k: v for k, v in data.items()
                                      if k in Machine.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        # No cache and an unreadable cache are the same thing: nothing is known.
        return Machine()


def _save_cached_machine(machine: Machine) -> None:
    try:
        from dataclasses import asdict
        MACHINE_CACHE.write_text(json.dumps(asdict(machine), indent=2),
                                 encoding="utf-8")
    except OSError as exc:
        # Including ENOSPC. Losing the cache costs nothing that matters.
        log.info("printer: could not cache the machine facts: %s", scrub(str(exc)))


# --------------------------------------------------------------------------
# The interface
# --------------------------------------------------------------------------

class Printer:
    """What the screen may ask of a printer. Both implementations satisfy it.

    Two rules hold for every implementation:

    1. **`status()` never raises.** It is called from a polling loop behind a
       screen. A screen that goes blank because the printer's Wi-Fi dropped is
       worse than one that says the printer is offline, and an exception on a
       poll path is how a whole page goes dark.
    2. **Everything else raises `PrinterError` and never lies.** `start()`
       returning quietly when nothing started is the one failure this project has
       recorded more often than any other.
    """

    def connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def status(self) -> Status:
        raise NotImplementedError

    def send(self, gcode_3mf: Path, on_progress: Optional[Callable] = None) -> str:
        """Put a sliced file on the printer. Returns the name it landed under.

        Blocking, and slow — a real `.gcode.3mf` is several megabytes over FTPS.
        The caller runs it on a worker thread; `app/server.py` already has that
        shape for the slicer.
        """
        raise NotImplementedError

    def start(self, remote_name: str, job_name: str = "",
              ams_mapping: list | None = None) -> None:
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def load(self) -> None:
        """Load the external spool — push filament to the nozzle and purge.
        Interlocked (it extrudes). 2026-09-07: a fresh spool that was never
        loaded ran a whole job dry; this is the one-tap version of the
        printer's own Load menu."""
        raise NotImplementedError

    def unload(self) -> None:
        raise NotImplementedError

    def confirm_filament(self) -> None:
        """Answer the A1's "did filament come out of the nozzle?" question —
        the Continue button on its own touchscreen. Interlocked; only ever
        called from a person's tap. See `LOAD_QUESTION_ERROR`."""
        raise NotImplementedError

    #: The light as last commanded: True on, False off, None never told.
    light: Optional[bool] = None
    #: True after a press of the button: the camera's own on-for-a-watcher /
    #: off-when-nobody-looks switching then leaves the light alone until the
    #: next print starts. Without this the button "did nothing" — the camera
    #: put the light straight back (the owner, 2026-09-12 11:20: "the light
    #: button also is a little buggy").
    light_held: bool = False

    def set_light(self, on: bool) -> None:
        """The toolhead light. Off when nobody is watching the camera, on
        when someone is (the owner, 2026-09-12: "turn the light off for it if it
        doesn't need it"). Not interlocked: it moves nothing."""
        raise NotImplementedError

    def light_on(self) -> None:
        """By hand. Holds against the camera until the next print."""
        self.light_held = True
        self.set_light(True)

    def light_off(self) -> None:
        self.light_held = True
        self.set_light(False)

    def set_filament(self, material: str) -> None:
        """Tell the machine what is on the external spool (PLA or PETG), so
        its own Load screen heats to the right temperature and it does not
        flag a PETG file as a mismatch against a spool it believes is PLA.
        Not interlocked: it extrudes nothing."""
        raise NotImplementedError

    def set_ams_filament(self, slot: int, material: str, colour: str) -> None:
        """Tell the AMS Lite what is in one of its four slots (no RFID, so it
        knows nothing unless told). Not interlocked: it extrudes nothing.
        `colour` is "RRGGBB"."""
        raise NotImplementedError

    def ams_reading(self):
        """What the printer's last report says about its AMS, as an
        `ams.Ams`, or None (no AMS, nothing reported yet). Never raises."""
        import ams
        snap = getattr(self, "snapshot", None)
        try:
            return ams.parse_report(snap.raw()) if snap is not None else None
        except Exception:                               # noqa: BLE001
            return None

    def resume(self) -> None:
        """Carry on after a pause.

        In the interface because SPEC's failure handling requires it: the A1
        pauses itself when the filament runs out, and the settled response is to
        leave it paused, say so, and resume when it has been reloaded. Without
        `resume()` the recoverable half of that table cannot be acted on at all.
        """
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def gcode(self, text: str) -> None:
        """Send raw G-code lines (auto-eject's plate moves). Refused while
        the printer is busy, so it can never move a plate under a print."""
        raise NotImplementedError

    def machine(self) -> Machine:
        """What the machine says about itself. Never raises, never blocks.

        Falls back to the last-seen facts, marked `stale`, when the printer is
        asleep — she may open FOUNDRY at any time and the design screen must not
        wait on a machine that is off.
        """
        return Machine()

    @property
    def handles(self) -> dict:
        """Ways to make this printer misbehave on purpose, by name.

        Empty on the real machine, and empty BY CONSTRUCTION rather than by a
        branch somewhere: a real A1 simply has no way to be told to fail, so the
        caller cannot reach for one. The fake returns four, which is how a screen
        exercises its failed, paused and offline states without hardware.

        This exists so that `app/server.py` never needs
        `isinstance(printer, FakePrinter)`. An isinstance in the server is a test
        concern leaking into the thing being tested, and it is one edit away from
        someone adding a second branch behind it.
        """
        return {}

    def ask_for_everything(self) -> bool:
        """Ask for a full report. Returns whether one has arrived.

        **On the interface, not just on `A1Printer`.** It lived only on the real
        one, so a screen built against the fake and then pointed at the machine
        crashed on `AttributeError` — and a screen that never called it left the
        real printer blank for ever, because the fields it needs never appear in
        a sparse diff. Neither failure was discoverable from the fake.
        """
        raise NotImplementedError

    def camera_frame(self) -> Optional[Frame]:
        """The most recent picture, or None if there isn't one.

        `None` rather than raising, for the same reason `status()` never raises:
        it is polled behind a screen. "No picture" is an ordinary answer — the
        camera may be unreachable, the printer may not have one, or the stream
        may simply not have delivered its first frame yet, and the screen shows
        the same honest empty panel for all three.
        """
        raise NotImplementedError


# --------------------------------------------------------------------------
# The real one
# --------------------------------------------------------------------------

class A1Printer(Printer):
    """The Bambu Lab A1 over MQTT and FTPS on the LAN.

    **Nothing in this class has been executed against the real machine except
    `connect()`, `status()` and the FTPS login.** On 2026-08-21 the printer had
    no filament loaded and nobody was in the room, so every method that makes it
    act was written and left unrun on purpose. They are marked individually.
    """

    def __init__(self, config: Config, client=None) -> None:
        self.config = config
        self.snapshot = Snapshot()
        self._client = client           # injectable, so tests never open a socket
        self._connected = False
        # Set when the broker GRANTS the report subscription, cleared when the
        # link drops. An Event and not a bool because paho delivers the SUBACK
        # on its own network thread while `connect()` waits on the caller's.
        self._subscribed = threading.Event()
        self._sequence = 0
        self._trouble = ""
        self._camera: Optional[CameraStream] = None

    # -- plumbing ---------------------------------------------------------

    @property
    def _topic_report(self) -> str:
        return f"device/{self.config.serial}/report"

    @property
    def _topic_request(self) -> str:
        return f"device/{self.config.serial}/request"

    def _next_sequence(self) -> str:
        self._sequence += 1
        return str(self._sequence)

    def connect(self) -> None:
        if not self.config.configured:
            raise NotConfigured("no printer configured yet")

        import paho.mqtt.client as mqtt   # imported here so the module loads
                                          # on a machine without paho installed

        if self._client is None:
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                     protocol=mqtt.MQTTv311)
            except AttributeError:        # paho 1.x
                client = mqtt.Client(protocol=mqtt.MQTTv311)
            client.username_pw_set(MQTT_USER, self.config.access_code)
            # The printer presents a self-signed certificate for a hostname it
            # does not have. Verification is off because there is nothing to
            # verify against, NOT as a shortcut — the connection is still
            # encrypted, and it is a link between two machines on this LAN.
            client.tls_set(cert_reqs=ssl.CERT_NONE,
                           tls_version=ssl.PROTOCOL_TLS_CLIENT)
            client.tls_insecure_set(True)
            # paho's own logger is deliberately NOT enabled: it renders the
            # packets it sends, and the CONNECT packet carries the access code.
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            self._client = client

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._subscribed.clear()
        try:
            self._client.connect(self.config.host, MQTT_PORT, keepalive=60)
            self._client.loop_start()
        except (OSError, socket.timeout) as exc:
            raise PrinterError(scrub(f"could not reach the printer: "
                                     f"{type(exc).__name__}")) from None
        self._connected = True

        # Wait for the SUBACK before returning. A caller that publishes the
        # moment `connect()` returns is the bug this exists to close, and it is
        # not the caller's job to know that.
        #
        # A timeout is NOT an error here: the link is up, messages the printer
        # sends unprompted still arrive, and `status()` already reports silence
        # honestly. What must not happen is pretending we are subscribed — so
        # the event simply stays clear and `ask_for_everything()` refuses.
        if not self._subscribed.wait(SUBSCRIBE_TIMEOUT):
            log.warning("printer: no SUBACK within %.1fs — connected, but not "
                        "yet delivering to us", SUBSCRIBE_TIMEOUT)

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        # Subscribing to the EXACT serial matters: `device/+/report` is accepted
        # and granted by this broker and then delivers nothing at all, which is
        # indistinguishable from a printer that has gone quiet. Verified against
        # the real machine on 2026-08-21.
        client.subscribe(self._topic_report, qos=0)
        log.info("printer: connected, subscribed to its report topic")

    def _on_subscribe(self, client, userdata, mid, granted_qos=None,
                      properties=None) -> None:
        # paho 1.x passes (client, userdata, mid, granted_qos); 2.x adds
        # `properties` and calls it `reason_code_list`. Both land here, and
        # neither argument is needed — the call itself IS the grant.
        self._subscribed.set()
        log.info("printer: subscription granted; its reports now reach us")

    def _on_disconnect(self, client, userdata, *args, **kwargs) -> None:
        # Not an error and not fatal: paho reconnects on its own, and `status()`
        # already reports silence honestly. Logged so a flapping link is visible.
        #
        # The subscription does NOT survive the drop. paho re-subscribes on
        # reconnect via `_on_connect`, and anything published in the gap is
        # answered to a topic nobody is listening to — exactly the original bug.
        self._subscribed.clear()
        log.info("printer: disconnected; the client will retry")

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8", "replace"))
        except ValueError:
            # A malformed message must not kill the network thread — that would
            # stop every future message with no error anywhere but here.
            log.warning("printer: unreadable message on %s", message.topic)
            return
        if isinstance(payload, dict):
            self.snapshot.take(payload)

    def close(self) -> None:
        if self._client is not None and self._connected:
            try:
                # ORDER MATTERS, and getting it wrong hangs rather than errors.
                # `loop_stop()` first stops the network thread, and `disconnect()`
                # then has nobody left to write its DISCONNECT packet, so it
                # blocks for ever. Measured: a probe against the real printer sat
                # here until it was killed at two minutes, and took the captured
                # data with it. Disconnect first, then stop the loop.
                self._client.disconnect()
                self._client.loop_stop()
            except Exception as exc:                    # noqa: BLE001
                log.info("printer: %s while closing", type(exc).__name__)
        if self._camera is not None:
            self._camera.close()
            self._camera = None
        self._connected = False

    # -- reading ----------------------------------------------------------

    def status(self) -> Status:
        """Never raises. See `Printer`."""
        if not self.config.configured:
            missing = "serial number" if not self.config.serial else "address"
            return Status(state=PrinterState.NOT_CONFIGURED,
                          detail=f"no printer set up yet — the {missing} is missing")
        if not self._connected:
            return Status(state=PrinterState.OFFLINE,
                          detail=self._trouble or "not connected")
        try:
            # `self._connected` is the honest signal and was already checked
            # above, so reaching here means the link is up.
            return self.snapshot.status(connected=True)
        except Exception as exc:                        # noqa: BLE001
            # A bug in the parsing must not blank the screen either.
            log.error("printer: could not read the status: %s",
                      scrub(f"{type(exc).__name__}: {exc}"), exc_info=True)
            return Status(state=PrinterState.UNKNOWN,
                          detail="could not read the printer's status")

    def machine(self) -> Machine:
        """Read from the live report if there is one, else from the cache.

        A full report is required — the machine facts live only in `msg: 0`, so
        until `ask_for_everything()` has been answered this returns the cached
        ones. That is the honest order: cached-and-labelled beats invented.
        """
        try:
            if self.snapshot.complete:
                fresh = machine_from_report(self.snapshot.raw())
                _save_cached_machine(fresh)
                return fresh
        except Exception as exc:                        # noqa: BLE001
            log.error("printer: could not read the machine facts: %s",
                      scrub(f"{type(exc).__name__}: {exc}"))
        return _load_cached_machine()

    def camera_frame(self) -> Optional[Frame]:
        """A real picture from the real camera. **This one IS verified** —
        the transport was established against the machine and a frame decoded.

        Receive-only: it authenticates and then reads. Nothing it sends can move
        or heat anything.
        """
        if not self.config.configured:
            return None
        if self._camera is None:
            self._camera = CameraStream(self.config.host, self.config.access_code,
                                        on_open=lambda: self._light_for_camera(True),
                                        on_close=lambda: self._light_for_camera(False))
        try:
            return self._camera.frame()
        except Exception as exc:                        # noqa: BLE001
            log.error("printer: camera: %s", scrub(f"{type(exc).__name__}: {exc}"))
            return None

    def ask_for_everything(self) -> bool:
        """Request a full report (`pushall`). Returns whether one has arrived.

        **Refuses unless the subscription has been granted.** Publishing before
        the SUBACK is not a slow success — the printer answers once, to a topic
        nobody is listening to, and never repeats it. Raising is the only honest
        outcome: it is louder than a report that silently never comes, which is
        what this cost twice before it was understood.

        The return value is `Snapshot.complete` — whether a FULL report has ever
        landed — and not whether the publish succeeded. MQTT confirms delivery
        of a packet, never that the printer answered it.

        **NOT RUN against the real machine.** Its whole effect is "tell me what
        you are doing" — it heats nothing, moves nothing and starts nothing — but
        it is a publish, and this module was written under an instruction to
        read and subscribe only.

        It is here because without it a freshly connected client is blind: the
        fields the screen needs never appear in a diff, so `Status.complete`
        stays False and the state stays UNKNOWN until the printer happens to
        change what it is doing. This is the call that fixes that, and it is the
        first thing to run once somebody is standing at the machine.
        """
        if not self._subscribed.is_set():
            raise PrinterError(
                "not subscribed to the printer's reports yet, so the answer "
                "would go unheard — nothing was asked")
        self._publish({"pushing": {"command": "pushall",
                                   "sequence_id": self._next_sequence()}})
        return self.snapshot.complete

    # -- acting -----------------------------------------------------------

    def _require_ready(self, doing: str, setting: str) -> None:
        """The two checks that come before any command that makes it act.

        One helper so the ORDER is the same everywhere: "there is no printer at
        all" is a truer thing to say than "printing is switched off", so it is
        said first. Written once because three call sites getting it right by
        hand is three chances to get it wrong.
        """
        if not self.config.configured:
            raise NotConfigured("no printer configured yet")
        if not getattr(self.config, setting):
            raise RealPrintingDisabled(doing, setting)

    def _publish(self, payload: dict) -> None:
        if not self.config.configured:
            raise NotConfigured("no printer configured yet")
        if not self._connected or self._client is None:
            raise PrinterError("not connected to the printer")
        result = self._client.publish(self._topic_request, json.dumps(payload), qos=1)
        # `publish()` returns whether the packet was QUEUED, which is not whether
        # the printer accepted it — see the caveat on `start()`. But a failure to
        # queue is a definite failure and must not pass as success.
        rc = getattr(result, "rc", 0)
        if rc != 0:
            raise PrinterError(f"the printer did not accept the command (rc {rc})")

    def send(self, gcode_3mf: Path, on_progress: Optional[Callable] = None) -> str:
        """Upload a sliced file over FTPS. **Login VERIFIED; upload NOT RUN.**

        The connection, the implicit-TLS handling and the directory listing were
        all exercised against the real printer on 2026-08-21. `STOR` was not: it
        writes to the machine's SD card, and the instruction was read-only.
        """
        # Provisional, and its own flag: see `Config.allow_uploads`. An upload
        # is very probably inert, and this comes off the moment somebody has
        # watched one land and seen the printer do nothing.
        self._require_ready("send a file to the printer", "allow_uploads")
        gcode_3mf = Path(gcode_3mf)
        if not gcode_3mf.is_file():
            raise PrinterError(f"there is no file at {gcode_3mf}")

        name = gcode_3mf.name
        expected = gcode_3mf.stat().st_size

        # 2026-08-21: this raised `PrinterError: could not send the file:
        # TimeoutError` while the file sat on the printer at all 999,701 bytes.
        # A transfer error is therefore NOT the answer to "did it arrive" — the
        # printer is, and only the printer. So the STOR's failure is remembered
        # rather than raised, and the question is put to the machine.
        transfer_failure = None
        try:
            with _ftps(self.config, timeout=_transfer_timeout(expected)) as ftp:
                if self.config.remote_dir not in ("", "/"):
                    ftp.cwd(self.config.remote_dir)
                with gcode_3mf.open("rb") as fh:
                    ftp.storbinary(f"STOR {name}", fh, blocksize=32768,
                                   callback=on_progress)
        except ftplib.all_errors as exc:
            # `all_errors` already includes OSError, so this catches a refused
            # login, a dropped data connection and an unreachable printer alike.
            transfer_failure = scrub(f"{type(exc).__name__}: {exc}")
            log.info("printer: the transfer of %s reported %s — asking the "
                     "printer what it actually has", name, transfer_failure)

        # A FRESH connection, on the ORDINARY timeout. The old code asked inside
        # the same `try`, over the connection that had just timed out, so the one
        # check that would have caught this never ran.
        try:
            with _ftps(self.config) as ftp:
                if self.config.remote_dir not in ("", "/"):
                    ftp.cwd(self.config.remote_dir)
                landed = ftp.size(name)
        except ftplib.error_perm:
            # A definite "no such file" — verified against the real printer,
            # which answers an absent name with `550`.
            raise PrinterError(
                f"the transfer failed and there is no {name} on the printer"
                + (f" ({transfer_failure})" if transfer_failure else "")) from None
        except ftplib.all_errors as exc:
            # Unreachable is not absent. Reporting either success or a clean
            # failure here would be inventing an answer nobody has.
            raise PrinterError(scrub(
                f"sent {name}, but could not then reach the printer to find "
                f"out whether it landed: {type(exc).__name__}")) from None

        if landed != expected:
            raise PrinterError(
                f"the file arrived at {landed} bytes, not {expected} — "
                f"it did not upload completely")
        if transfer_failure:
            log.info("printer: %s landed complete at %d bytes despite %s",
                     name, expected, transfer_failure)
        log.info("printer: sent %s (%d bytes)", name, expected)
        return name

    def start(self, remote_name: str, job_name: str = "",
              ams_mapping: list | None = None) -> None:
        """Start a file that is already on the printer. **NOT RUN.**

        Two things about this are documented rather than verified, and they are
        the two most likely to be wrong:

        * **the `url`.** `file:///` plus the bare name is what Bambu Studio's own
          LAN send uses for this generation of machine; other implementations use
          `file:///sdcard/` and `file:///mnt/sdcard/`. Both are in the config file
          as `url_prefix` for exactly that reason.
        * **`param`.** `Metadata/plate_1.gcode` is the path INSIDE the 3MF
          archive, not on the printer.

        And a third thing that is true of every command here: MQTT tells you the
        packet was delivered, never that the printer agreed to it. The only
        honest confirmation that a print started is the status changing, so
        whoever calls this must watch `status()` afterwards and say so if it
        does not.
        """
        # The interlock, before anything else and before any socket is touched.
        # This is the line that stands between an agent and the owner's filament.
        self._require_ready("start a print", "allow_real_prints")
        if not remote_name:
            raise PrinterError("nothing to start: no file name")

        # Read off the machine, never typed. `use_ams` was hardcoded False with
        # a comment saying "an A1 with no AMS; an A1 Combo needs True" — a fact
        # about the owner's hardware living in the source, where bolting an AMS on
        # would silently print from the external spool instead and nothing would
        # say so. The printer states this in every full report.
        #
        # Unknown is NOT True: telling a machine with no AMS to use one fails
        # the print outright, and unknown is exactly the state of a freshly
        # connected client that has not been answered yet.
        has_ams = self.machine().has_ams
        if has_ams is None:
            log.warning("printer: no full report yet, so it has not said "
                        "whether it has an AMS — assuming none")
            has_ams = False
        if ams_mapping is not None:
            # Phase 3: the caller chose an AMS slot (amsprint.py). Only on a
            # machine that said it has an AMS; a mapping sent to one without
            # fails the print. UNVERIFIED on hardware (SPEC fact 78).
            if not has_ams:
                raise PrinterError("this printer has not said it has an AMS, "
                                   "so I won't ask it to print from one")
            ams_mapping = [int(n) for n in ams_mapping]

        try:
            log.info("printer: before the start it believes %s",
                     filament_trace(self.snapshot.raw()))
        except Exception:                                  # noqa: BLE001
            pass                                           # a log line, never a reason not to print

        self._fresh_session()
        self._clear_latched_error()

        self.light_held = False                   # a new print: the camera runs the light again
        command = {
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "url": f"{self.config.url_prefix}{remote_name}",
            "subtask_name": job_name or Path(remote_name).stem,
            "use_ams": has_ams,         # read from the machine, above
            "timelapse": False,
            "bed_leveling": True,
            "flow_cali": True,
            "vibration_cali": True,
            "layer_inspect": False,
            "profile_id": "0", "project_id": "0",
            "subtask_id": "0", "task_id": "0",
            "sequence_id": self._next_sequence(),
        }
        if ams_mapping is not None:
            # One entry per filament in the file, in the file's order: the
            # global tray id to feed it from (unit 0, so the slot number),
            # -1 for a filament the plate does not use. UNVERIFIED.
            command["ams_mapping"] = ams_mapping
        self._publish({"print": command})
        log.info("printer: asked it to start %s", remote_name)

    def _fresh_session(self) -> None:
        """Drop and reopen the MQTT connection just before a start.

        2026-11-15: from 2026-11-11 20:20 on, a remote start sent on a
        connection that had been up for a while was ignored ("never started"
        after 30 s; 13 times). The first start after a fresh connection
        worked every time: 11-13 14:50 (process restart), 11-14 12:51
        (printer power cycle), 11-15 11:21 (process restart, the printer's
        reply to project_file was result success and it went FINISH -> IDLE
        -> PREPARE in 7 s). Cause inside the printer unknown; this is the
        workaround the evidence supports. Never fatal: if the reconnect
        fails the start goes out on whatever link there is.
        """
        client = self._client
        if client is None:
            return
        try:
            log.info("printer: reconnecting fresh before the start")
            try:
                client.disconnect()
            finally:
                client.loop_stop()
            self.connect()
            self.ask_for_everything()
            deadline = time.time() + 5.0
            while time.time() < deadline and not self.snapshot.complete:
                time.sleep(0.2)
        except Exception as exc:                          # noqa: BLE001
            log.warning("printer: fresh reconnect before the start failed "
                        "(%s); starting on the existing link",
                        type(exc).__name__)

    def _clear_latched_error(self) -> None:
        """Clear a `print_error` the last job left behind, before a new start.

        2026-09-28: the A1 LATCHES the error a job ended with (here the
        nozzle-clump HMS 0300_1A00 and then 0x05004002/0x05004003) and, while
        one is latched, refuses every remote `project_file` — and each refusal
        latches a fresh error, so it never recovers on its own. Twenty starts
        failed in a row while the SAME file started at once from the printer's
        touchscreen, which clears the latch. Bambu Studio sends
        `clean_print_error` for exactly this. Never while printing: a paused
        job's error (the filament-load question) is a question, not debris.
        """
        raw = self.snapshot.raw()
        latched = _int(raw.get("print_error"))
        if not latched or self.snapshot.status().busy:
            return
        log.info("printer: clearing the error the last job left (print_error %s) "
                 "before starting", latched)
        self._publish({"print": {
            "command": "clean_print_error",
            "subtask_id": str(raw.get("subtask_id") or "0"),
            "print_error": latched,
            "sequence_id": self._next_sequence(),
        }})
        deadline = time.time() + CLEAR_ERROR_WAIT_S
        while time.time() < deadline:
            if not _int(self.snapshot.raw().get("print_error")):
                return
            time.sleep(0.2)
        log.warning("printer: print_error %s still showing after %.0fs; starting anyway",
                    latched, CLEAR_ERROR_WAIT_S)

    # `pause()` and `stop()` are DELIBERATELY NOT behind the interlock, and the
    # asymmetry is the point rather than an oversight.
    #
    # The interlock exists to stop filament being burned. Pausing and stopping
    # are how you STOP burning it. **A print can be started from the printer's
    # own screen while FOUNDRY is watching, so the brake has to work on a job we
    # did not start.** SPEC's failure table is "stop immediately" for a part that
    # has come off the plate, and an interlock that also disabled the emergency
    # brake would be a safety feature that caused the accident.
    #
    # `resume()` IS behind it, because resuming starts the extruder again.

    def pause(self) -> None:
        """**NOT RUN.** Deliberately not interlocked — see above."""
        self._publish({"print": {"command": "pause", "param": "",
                                 "sequence_id": self._next_sequence()}})

    def resume(self) -> None:
        """Interlocked: resuming extrudes again.

        2026-11-14 15:00: paused at layer 3 with print_error 0x03008014 (HMS
        0300_1A00), Resume pressed four times in FOUNDRY and the A1 stayed
        PAUSE. `clean_print_error` (subtask_id + that code) and then `resume`
        both answered success and it went RUNNING. So a paused job's error is
        cleared first, as Bambu Studio's Resume does. Never the load question
        (0x07FF8007): that one is answered by `confirm_filament`, not cleared.
        """
        self._require_ready("resume a print", "allow_real_prints")
        raw = self.snapshot.raw()
        err = _int(raw.get("print_error"))
        if err and err != LOAD_QUESTION_ERROR:
            log.info("printer: clearing print_error %s before resuming", err)
            self._publish({"print": {
                "command": "clean_print_error",
                "subtask_id": str(raw.get("subtask_id") or "0"),
                "print_error": err,
                "sequence_id": self._next_sequence(),
            }})
            time.sleep(1.0)
        self._publish({"print": {"command": "resume", "param": "",
                                 "sequence_id": self._next_sequence()}})

    def stop(self) -> None:
        """**NOT RUN.** Deliberately not interlocked — see above."""
        self._publish({"print": {"command": "stop", "param": "",
                                 "sequence_id": self._next_sequence()}})

    def gcode(self, text: str) -> None:
        """Plate moves for auto-eject. `gcode_line` RUN 2026-11-09 against
        the A1 (G28 Y, G1 Y60..256): it answers `result: success`."""
        if self.status().busy:
            raise PrinterError("it is printing; the plate stays where it is")
        self._publish({"print": {"command": "gcode_line", "param": text,
                                 "sequence_id": self._next_sequence()}})

    # The external spool's nozzle temperature for a load, per material. The
    # A1's own Load screen heats to the filament's setting; with no AMS the
    # machine only knows what it has been told (`set_filament`).
    LOAD_TEMP_C = 220
    def _load_temp(self) -> int:
        """The Load temperature for what the spool record says is loaded
        (`materials.py`, by way of spool.py). Read at press time, so a reel
        changed on the tray loads at its own temperature rather than PLA's."""
        try:
            import materials
            import spool
            return int(materials.get(spool.material()).nozzle)
        except Exception:                             # noqa: BLE001
            return self.LOAD_TEMP_C

    def set_light(self, on: bool) -> None:
        """The toolhead light, by MQTT `ledctrl` on the `chamber_light` node —
        the payload Bambu's own apps send. **UNVERIFIED on this A1
        (2026-09-12)**: the first press with the owner at the machine is the
        test; if the A1 names its light differently, this is where to look."""
        self._publish({"system": {"command": "ledctrl",
                                  "led_node": "chamber_light",
                                  "led_mode": "on" if on else "off",
                                  "led_on_time": 500, "led_off_time": 500,
                                  "loop_times": 0, "interval_time": 0,
                                  "sequence_id": self._next_sequence()}})
        self.light = bool(on)

    def _light_for_camera(self, on: bool) -> None:
        """The camera opened (a watcher) or let go (nobody): light to match.
        Never raises — the camera must not depend on the light answering.
        A light set by hand is left alone (`light_held`)."""
        if self.light_held:
            log.info("printer: light left as she set it (camera %s)", "open" if on else "closed")
            return
        try:
            self.set_light(on)
            log.info("printer: light %s for the camera", "on" if on else "off")
        except Exception as exc:                      # noqa: BLE001
            log.warning("printer: could not switch the light: %s: %s", type(exc).__name__, exc)

    def set_filament(self, material: str) -> None:
        """Tell the A1 what is on the external spool.

        **UNVERIFIED against this machine (2026-09-11)** — the first press is
        the test, with the owner at the printer's screen. The payload is the one
        Bambu's apps send for the virtual external tray: `ams_filament_setting`
        with `ams_id 255`, `tray_id 254`, the filament id, the type and the
        nozzle range. What the machine then reports in `vt_tray.tray_type` is
        read back as `Machine.spool_type`; if that does not follow, this is
        where to look, and nothing here claims it took.
        """
        self._filament_setting(255, 254, material, "FFFFFFFF")

    def set_ams_filament(self, slot: int, material: str, colour: str) -> None:
        """Tell the AMS Lite what is in `slot` (0..3).

        **UNVERIFIED against this machine (2026-10-17)** - the same
        `ams_filament_setting` the external tray uses, with `ams_id 0`,
        `tray_id <slot>` and the colour as `tray_color` (RRGGBB + FF alpha).
        That is what Bambu's apps send for an AMS Lite tray, but no unit has
        been attached when this was written. The first save with a person at
        the printer is the test; what the machine then reports in
        `print.ams.ams[0].tray[slot]` is read back by `ams.parse_report`.
        """
        import re
        if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot <= 3:
            raise PrinterError(f"not an AMS slot: {slot!r}")
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", str(colour or "")):
            raise PrinterError(f"not a colour: {colour!r}")
        self._filament_setting(0, slot, material, str(colour).upper() + "FF")

    def _filament_setting(self, ams_id: int, tray_id: int, material: str,
                          colour_rgba: str) -> None:
        import materials
        try:
            spec = materials.get(material)
        except ValueError:
            raise PrinterError(f"not a material I know: {material!r}") from None
        self._publish({"print": {"command": "ams_filament_setting",
                                 "ams_id": ams_id,
                                 "tray_id": tray_id,
                                 "tray_info_idx": spec.idx,
                                 "tray_color": colour_rgba,
                                 "nozzle_temp_min": spec.low,
                                 "nozzle_temp_max": spec.high,
                                 "tray_type": spec.name,
                                 "setting_id": "",
                                 "sequence_id": self._next_sequence()}})

    def load(self) -> None:
        """Load the external spool: push filament to the nozzle and purge.
        Interlocked — it extrudes.

        **UNVERIFIED against this machine (2026-09-07).** The payload is the
        one Bambu's own apps send for the virtual external tray (id 254):
        `ams_change_filament` with `target 254`. No document this project has
        read shows it for an A1 without an AMS, so the first press is the
        test: watch the printer's screen. If the machine does something else
        or nothing, this is where to look, and SPEC's rule about unobserved
        commands applies — record what it did, do not guess.
        """
        self._require_ready("load filament", "allow_real_prints")
        temp = self._load_temp()
        self._publish({"print": {"command": "ams_change_filament",
                                 "target": 254,
                                 "curr_temp": temp,
                                 "tar_temp": temp,
                                 "sequence_id": self._next_sequence()}})

    def unload(self) -> None:
        """Unload the external spool. **UNVERIFIED** — see `load()`. Not
        interlocked: retracting filament extrudes nothing."""
        self._publish({"print": {"command": "unload_filament",
                                 "sequence_id": self._next_sequence()}})

    def confirm_filament(self) -> None:
        """Press Continue on the A1's "did filament come out?" question.

        **The command: `ams_control` with `param: "done"`.** Two independent
        reasons to believe it, and one command that is NOT it:

        * **Proven live, 2026-09-22 13:35**, with the owner's say-so and the lead
          at the keyboard: the A1 was paused at 0 % on `print_error 134184967`;
          one `ams_control`/`done` took it to RUNNING, and it printed through
          layer 2 by 13:41 without asking again. A read-only MQTT capture has
          the printer's own reply (`param: "done", result: "success"`) at
          13:35:06 and `ams.tray_pre` flipping to 254 straight after.
        * **The bundled slicer agrees.** `tools/bambu-studio/BambuStudio.dll`
          carries the params of `command_ams_control` as adjacent strings —
          `reset`, `done`, `abort`, `ams_control` — and the button labels for
          this dialog: "Filament Extruded, Continue" / "Not Extruded Yet,
          Retry" (`resources/hms/hms_action_093.json` gives 07FF8007 the two
          actions `[7, 8]`).
        * **`resume` is NOT Continue — it is Retry.** Measured the same
          afternoon: a resume at 13:31:58 restarted the load and the printer
          asked the same question again 57 s later.

        Interlocked like `resume()`, because saying Continue starts the print.
        Refuses unless the machine is actually asking: this must never become a
        generic "unstick it" button, and a person tapping Continue at a moment
        when the question is not on the screen would be answering nothing.
        """
        self._require_ready("answer the printer's filament question",
                            "allow_real_prints")
        if not asks_about_the_load(self.status().errors):
            raise PrinterError("the printer is not asking about the filament "
                               "right now")
        self._publish({"print": {"command": "ams_control",
                                 "param": "done",
                                 "sequence_id": self._next_sequence()}})


class _FTPS(ftplib.FTP_TLS):
    """Port 990 is IMPLICIT TLS: the socket is wrapped BEFORE the greeting.

    `ftplib.FTP_TLS` speaks explicit TLS — it connects in the clear and issues
    `AUTH TLS`. Against the printer that hangs waiting for a greeting that is
    already encrypted, and the symptom is a timeout with no message, which reads
    like the printer being off. Overriding the `sock` property is the standard
    way to bend `ftplib` into implicit mode; verified against the real printer.
    """

    def __init__(self, *args, **kwargs) -> None:
        self._sock = None
        super().__init__(*args, **kwargs)

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        """`ftplib`'s own `storbinary`, minus the TLS shutdown the printer never
        answers.

        The stock version ends every upload with `conn.unwrap()` — a polite TLS
        `close_notify` on the data connection, after which it waits for the
        printer to say the same back. The printer never does. So every upload
        that had in fact landed in well under a second sat there for the WHOLE
        transfer timeout (90 s for a coaster, 2026-09-07 09:26 → 09:27:41), was
        reported as `TimeoutError`, and was then found complete by `size()`.
        Three uploads in a row did exactly that (2026-08-21, 2026-09-06,
        2026-09-07). The "SD-card finalisation" story above
        `FTPS_TRANSFER_FINALISE_S` was the wrong explanation for that wait.

        Closing the data socket without the handshake is what `with` does on
        exit anyway; the bytes are already sent, and `send()` still asks the
        printer for the landed size before believing anything.
        """
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while buf := fp.read(blocksize):
                conn.sendall(buf)
                if callback:
                    callback(buf)
            # Deliberately NO `conn.unwrap()` here. See above.
        return self.voidresp()


class _ftps:
    """`with _ftps(cfg) as ftp:` — logged in, protected, and closed afterwards."""

    def __init__(self, config: Config,
                 timeout: float = FTPS_CONTROL_TIMEOUT) -> None:
        self.config = config
        # Explicit, and defaulted to the SHORT one. An upload passes
        # `_transfer_timeout(size)`; everything else — logging in, asking what
        # landed — must keep the ordinary budget rather than inherit an
        # upload's, or the check that catches a half-failed transfer takes ten
        # minutes to notice the printer has stopped answering.
        self.timeout = timeout
        self.ftp: Optional[_FTPS] = None

    def __enter__(self) -> _FTPS:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False      # same self-signed certificate as MQTT
        context.verify_mode = ssl.CERT_NONE
        self.ftp = _FTPS(context=context)
        self.ftp.connect(self.config.host, FTPS_PORT, timeout=self.timeout)
        self.ftp.login(MQTT_USER, self.config.access_code)
        self.ftp.prot_p()                   # encrypt the DATA connection too
        return self.ftp

    def __exit__(self, *exc) -> None:
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except Exception:                           # noqa: BLE001
                # A printer that has already dropped the control connection
                # raises here, and it is not a failure of whatever we came to do.
                try:
                    self.ftp.close()
                except Exception:                       # noqa: BLE001
                    pass


# --------------------------------------------------------------------------
# The fake
# --------------------------------------------------------------------------

class FakePrinter(Printer):
    """A printer that is not there, behaving like one that is.

    **It emits the same sparse diffs the real machine emits and feeds them
    through the same `Snapshot`.** That is the whole point: a fake that returns
    a tidy `Status` object directly would let the screen be built against a
    parser that has never run, and the parser is where the real printer's
    surprises live. Here, a job progressing on the fake genuinely exercises the
    merge, the missing-field handling and the `complete` flag.

    What it simulates honestly
    --------------------------
    A job starting, heating, printing layer by layer with a percentage and a
    falling time remaining, finishing; being paused and resumed; being stopped;
    reporting a fault; going off the network. Temperatures that move and wobble
    rather than sitting on the target.

    What it does NOT simulate, and must not be trusted for
    -----------------------------------------------------
    The AMS, the camera, HMS code semantics, real firmware timing, the printer
    refusing a command, and every field the real machine has never been observed
    to send. SPEC is explicit that the fake is for building the screen against
    and NOT for proving the integration, and the list above is what that sentence
    means concretely.

    Driving it from a screen
    ------------------------
    The default 600x runs SPEC's real 19h45m planter in about two minutes, which
    is right for watching the progress numbers behave — but it makes the warm-up
    invisible, because 1.5 print-minutes of heating passes in 0.15 s. To see
    every state including PREPARING, build a short job instead:

        FakePrinter(speed=20, minutes=6)    # a whole run in ~22 s

    and use `break_it()`, `run_out_of_filament()` and `go_offline()` to reach the
    three states that are otherwise impossible to photograph.
    """

    def __init__(self, clock: Optional[Callable] = None, speed: float = 600.0,
                 layers: int = 900, minutes: float = 1185.0,
                 jitter: bool = True, finalise_seconds: float = 0.0,
                 ams: bool = False) -> None:
        #: An AMS Lite attached (4 empty slots, no RFID, as the real unit
        #: reports until told). Off by default: the real A1 here has none,
        #: and every existing test relies on `has_ams` False.
        self.has_ams = bool(ams)
        self._ams_trays = [dict(id=str(i), tray_color="00000000", tray_type="",
                                tray_info_idx="", remain=-1) for i in range(4)]
        self._ams_active = 255          # 255 nothing loaded, 0..3 a slot
        #: Every ams_mapping a start was given (phase 3), for the tests.
        self.ams_mappings: list = []
        #: Slots whose spool a test has pulled out (tray_exist_bits bit off).
        self._ams_pulled: set = set()
        #: Injected so tests drive time rather than sleeping through it. The
        #: default 600x means a 19h45m planter — the real one SPEC measured —
        #: runs in two minutes, which is long enough to watch a screen behave and
        #: short enough to sit through.
        self.clock = clock or time.monotonic
        #: How long `send()` goes SILENT after the last byte, simulating the
        #: real machine finalising a write to its SD card — over 30 s for a
        #: megabyte, with no callback and no progress, indistinguishable from a
        #: hang. Off by default so no ordinary test ever sleeps; turn it on to
        #: exercise an upload bar's waiting state, which is otherwise
        #: unreachable and therefore untested.
        self.finalise_seconds = finalise_seconds
        self.speed = speed
        self.total_layers = layers
        self.total_minutes = minutes
        self._jitter = jitter
        self._random = random.Random(20260821)   # seeded: a fake that differs
                                                 # run to run makes a screen bug
                                                 # impossible to reproduce
        self.snapshot = Snapshot()
        self.sent: list = []            # files handed to it, for tests
        self.commands: list = []        # what it was asked to do, in order
        self._state = PrinterState.IDLE
        self._job = ""
        self._started_at: Optional[float] = None
        self._paused_at: Optional[float] = None
        self._paused_total = 0.0
        self._faults: list = []
        self._offline = False
        self._connected = False
        self._sequence = 0
        self._last_push: Optional[float] = None
        self._frames = 0
        self._pushed: dict = {}         # what it has already told the client

    # -- the interface ----------------------------------------------------

    def connect(self) -> None:
        self._connected = True
        # A real client learns nothing until it ASKS. The A1's periodic diffs
        # carry temperatures and not `gcode_state`, so a freshly connected
        # client sits at UNKNOWN with `complete` False until `pushall` is
        # answered — and a screen built against a fake that says IDLE the
        # instant it connects renders "Ready" about a printer it has never
        # heard from. This used to call `_push(full=True)` and did exactly that
        # while its own comment said it was avoiding it.
        self._sequence += 1
        report = self._report()
        # Seed the "already told them" record with the WHOLE report while
        # delivering only the temperatures. The printer knows all of this — it
        # simply has not said it — so the diffs that follow stay sparse and keep
        # `gcode_state` out of them, which is what the real machine does. Seeding
        # it with only the temperatures would make the very next diff carry
        # everything, and the fake would stop being sparse at all.
        self._pushed = dict(report)
        first = {"nozzle_temper": report["nozzle_temper"],
                 "bed_temper": report["bed_temper"]}
        self.snapshot.take({"print": dict(first, command="push_status", msg=1,
                                          sequence_id=str(self._sequence))},
                           now=self._now())
        self._last_push = self._now()

    def ask_for_everything(self) -> bool:
        """Request a full report. On the fake this is what makes it READY.

        Exists here as well as on `A1Printer` because it lived only on the real
        one: a screen built against the fake and then pointed at the machine
        crashed on `AttributeError`, and one that never called it left the real
        printer blank for ever. Neither failure was reachable from the fake.
        """
        self._tick()
        self._push(full=True)
        return self.snapshot.complete

    def close(self) -> None:
        self._connected = False

    def status(self) -> Status:
        if not self._connected:
            return Status(state=PrinterState.OFFLINE, detail="not connected")
        self._tick()
        return self.snapshot.status(now=self._now())

    def send(self, gcode_3mf: Path, on_progress: Optional[Callable] = None) -> str:
        path = Path(gcode_3mf)
        if not path.is_file():
            raise PrinterError(f"there is no file at {path}")
        if on_progress:
            # Real FTPS calls back per block; a screen with an upload bar has to
            # be exercised against more than one callback.
            size = path.stat().st_size
            done = 0
            while done < size:
                step = min(32768, size - done)
                on_progress(b"\0" * step)
                done += step
        if self.finalise_seconds:
            # The silence lands AFTER the last callback, which is where the real
            # one puts it: every byte is delivered and then the machine says
            # nothing at all while it writes its SD card.
            time.sleep(self.finalise_seconds)
        self.sent.append(path.name)
        self.commands.append(("send", path.name))
        return path.name

    def start(self, remote_name: str, job_name: str = "",
              ams_mapping: list | None = None) -> None:
        self._tick()
        if self._state in PrinterState.BUSY:
            raise PrinterError("it is already printing")
        if not remote_name:
            raise PrinterError("nothing to start: no file name")
        if remote_name not in self.sent:
            # The real machine refuses a file that is not on its card, with a
            # message nobody sees. Better to be loud here than to discover it as
            # a print that silently never starts.
            raise PrinterError(f"{remote_name} has not been sent to the printer")
        if ams_mapping is not None:
            if not self.has_ams:
                raise PrinterError("there is no AMS on this printer")
            self.ams_mappings.append(list(ams_mapping))
        self.commands.append(("start", remote_name))
        self.light_held = False
        self._job = job_name or Path(remote_name).stem
        self._state = PrinterState.PREPARING
        self._started_at = self._now()
        self._paused_total = 0.0
        self._faults = []
        self._push(full=True)

    def pause(self) -> None:
        self._tick()
        if self._state != PrinterState.PRINTING:
            raise PrinterError("it is not printing")
        self.commands.append(("pause", None))
        self._state = PrinterState.PAUSED
        self._paused_at = self._now()
        self._push()

    def resume(self) -> None:
        self._tick()
        if self._state != PrinterState.PAUSED:
            raise PrinterError("it is not paused")
        self.commands.append(("resume", None))
        # Time spent paused does not count towards the print, or the remaining
        # time jumps backwards the moment she reloads the filament.
        if self._paused_at is not None:
            self._paused_total += self._now() - self._paused_at
        self._paused_at = None
        self._faults = []
        self._state = PrinterState.PRINTING
        self._push()

    def gcode(self, text: str) -> None:
        self._tick()
        if self._state in PrinterState.BUSY:
            raise PrinterError("it is printing; the plate stays where it is")
        self.commands.append(("gcode", text))

    def stop(self) -> None:
        self._tick()
        if self._state not in PrinterState.BUSY:
            raise PrinterError("it is not printing")
        self.commands.append(("stop", None))
        self._state = PrinterState.FAILED
        self._faults = ["stopped"]
        self._push(full=True)

    def load(self) -> None:
        """A load from the external spool. Refused while a job runs, as the
        real machine refuses; records the command and marks filament present."""
        self._tick()
        if self._state in PrinterState.BUSY:
            raise PrinterError("it is printing")
        self.commands.append(("load", None))
        self._loaded = True
        self._push(full=True)

    #: What the fake believes is on its external spool — "" until told, as
    #: the real machine reports an all-zero `vt_tray` until configured.
    _tray_type = ""

    def set_light(self, on: bool) -> None:
        self._tick()
        self.commands.append(("light", bool(on)))
        self.light = bool(on)

    def set_filament(self, material: str) -> None:
        """Records the material and reports it back in `vt_tray`, as the
        real machine is expected to. Allowed while printing: it extrudes
        nothing, and the real command is not interlocked either."""
        self._tick()
        import materials
        try:
            chosen = materials.clean(material)
        except ValueError:
            raise PrinterError(f"not a material I know: {material!r}") from None
        self.commands.append(("set_filament", chosen))
        self._tray_type = chosen
        self._push(full=True)

    def set_ams_filament(self, slot: int, material: str, colour: str) -> None:
        """Records the slot setting and reports it back in `print.ams`, as the
        real unit is expected to. Refuses with no AMS attached, as a real A1
        without one would not take it. Not interlocked."""
        self._tick()
        import materials
        if not self.has_ams:
            raise PrinterError("there is no AMS on this printer")
        if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot <= 3:
            raise PrinterError(f"not an AMS slot: {slot!r}")
        try:
            spec = materials.get(material)
        except ValueError:
            raise PrinterError(f"not a material I know: {material!r}") from None
        self.commands.append(("set_ams_filament", (slot, spec.name, str(colour).upper())))
        self._ams_trays[slot].update(tray_color=str(colour).upper() + "FF",
                                     tray_type=spec.name, tray_info_idx=spec.idx)
        self._push(full=True)

    def forget_ams_slot(self, slot: int) -> None:
        """A handle for tests: the unit forgets a slot (a power cycle does
        this to a no-RFID unit), so the saved copy is what is left."""
        self._ams_trays[slot].update(tray_color="00000000", tray_type="",
                                     tray_info_idx="")
        self._push(full=True)

    def pull_ams_spool(self, slot: int, pulled: bool = True) -> None:
        """A handle for tests: the spool in `slot` is taken off (its bit in
        `tray_exist_bits` goes to 0) or put back."""
        (self._ams_pulled.add if pulled else self._ams_pulled.discard)(int(slot))
        self._push(full=True)

    def set_active_slot(self, slot: int) -> None:
        """A handle for tests: the unit feeds from `slot` (255 = none)."""
        self._ams_active = int(slot)
        self._push(full=True)

    def unload(self) -> None:
        self._tick()
        if self._state in PrinterState.BUSY:
            raise PrinterError("it is printing")
        self.commands.append(("unload", None))
        self._loaded = False
        self._push(full=True)

    def confirm_filament(self) -> None:
        """Answer the load question — the real one publishes `ams_control`
        with `param: "done"` (see `A1Printer.confirm_filament`).

        Refuses unless the machine is actually asking, exactly as the real one
        does, so a screen built against this cannot ship a Continue button
        that fires at any other moment.
        """
        self._tick()
        if not (self._state == PrinterState.PAUSED
                and asks_about_the_load(self.status().errors)):
            raise PrinterError("the printer is not asking about the filament "
                               "right now")
        self.commands.append(("confirm_filament", None))
        if self._paused_at is not None:
            self._paused_total += self._now() - self._paused_at
        self._paused_at = None
        self._faults = []
        self._state = PrinterState.PRINTING
        self._push(full=True)

    # -- the handles a test or a demo pulls -------------------------------

    def break_it(self, code: str = "0300_0100_0002_0001") -> None:
        """A real failure: the printer reports a fault and stops on its own.

        The default is the SHAPE of an HMS code, not a claim about what that
        particular code means. See `_faults()`.
        """
        self._tick()
        self._state = PrinterState.FAILED
        self._faults = [code]
        self._push(full=True)

    def run_out_of_filament(self, code: str = "0700_2000_0002_0001") -> None:
        """The recoverable one: the A1 pauses itself and waits for a person.

        SPEC's failure table turns on the difference between this and
        `break_it()`, so the fake can produce both. It does NOT claim these
        specific codes are what the real machine sends — that mapping has never
        been observed here.
        """
        self._tick()
        if self._state not in (PrinterState.PRINTING, PrinterState.PREPARING):
            raise PrinterError("it is not printing")
        self._state = PrinterState.PAUSED
        self._paused_at = self._now()
        self._faults = [code]
        self._push(full=True)

    def ask_if_filament_came_out(self) -> None:
        """The A1's load question at the start of a print (SPEC facts 46, 74):
        paused, carrying `print_error 134184967` (0x07FF8007), waiting for a
        person to tap Continue on the machine's own screen.

        Allowed from an EXISTING pause as well as from a running print,
        because on the real machine the code can land a poll after the state
        does (SPEC fact 3 says the stop code does exactly that)."""
        if self._state == PrinterState.PAUSED:
            self._tick()
            self._faults = ["134184967"]
            self._push(full=True)
            return
        self.run_out_of_filament(code="134184967")

    def camera_frame(self) -> Optional[Frame]:
        """A drawn picture that genuinely changes, so a working camera pane can
        be told from a dead one.

        Deliberately not a photograph and deliberately not static: it shows the
        job's own progress as a rising bar plus a marker that steps every frame,
        so a screen that has frozen looks different from one that is updating.

        **192x135 — the real camera's 1536x1080 aspect exactly**, so anything
        laid out against the fake fits the real picture without reflowing.

        Returns None while offline or disconnected, because that is what the
        real one does and the screen has to handle it.
        """
        if self._offline or not self._connected:
            return None
        self._tick()
        self._frames += 1

        w, h = 192, 135
        s = self.snapshot.status(now=self._now())
        done = (s.percent or 0) / 100.0
        px = bytearray(w * h * 3)
        for y in range(h):
            for x in range(w):
                i = (y * w + x) * 3
                # A dark chamber with a soft gradient, like the real frame.
                px[i] = 10 + (x * 12) // w
                px[i + 1] = 18 + (y * 20) // h
                px[i + 2] = 30 + (x * 30) // w
        # The part growing on the plate: a bar rising from the bottom.
        top = h - 8 - int((h - 30) * done)
        for y in range(top, h - 8):
            for x in range(w // 3, 2 * w // 3):
                i = (y * w + x) * 3
                px[i], px[i + 1], px[i + 2] = 120, 190, 230
        # A marker that steps every frame. If this stops moving, the pane is dead.
        mx = (self._frames * 7) % (w - 6)
        for y in range(2, 6):
            for x in range(mx, mx + 6):
                i = (y * w + x) * 3
                px[i], px[i + 1], px[i + 2] = 240, 240, 240

        return Frame(data=_png(w, h, px), content_type="image/png",
                     width=w, height=h, at=self._now(), number=self._frames)

    def machine(self) -> Machine:
        """The fake's machine facts mirror the real A1 as measured on
        2026-08-21: a 0.4 mm stainless nozzle, no AMS, an SD card, 1080p."""
        return Machine(nozzle_mm=0.4, nozzle_type="stainless_steel",
                       has_ams=self.has_ams, has_sdcard=True,
                       camera_resolution="1080p", wifi="-35dBm",
                       spool_type=self._tray_type,
                       seen_at=time.time(), stale=False)

    @property
    def handles(self) -> dict:
        """The four ways to make the fake misbehave. See `Printer.handles`."""
        return {
            "break_it": self.break_it,
            "run_out_of_filament": self.run_out_of_filament,
            "go_offline": self.go_offline,
            "come_back": self.come_back,
        }

    def go_offline(self) -> None:
        """Stop saying anything, without closing anything.

        The important half of a printer disappearing: the socket stays up and the
        messages stop. A screen tested only against `close()` has never seen the
        case where nothing is obviously wrong and the numbers are simply old.
        """
        self._offline = True

    def come_back(self) -> None:
        self._offline = False
        self._push(full=True)

    # -- the simulation ---------------------------------------------------

    def _now(self) -> float:
        return self.clock()

    def _elapsed_minutes(self) -> float:
        """Print-minutes elapsed, with time spent paused removed."""
        if self._started_at is None:
            return 0.0
        wall = self._now() - self._started_at - self._paused_total
        if self._paused_at is not None:
            wall -= self._now() - self._paused_at
        return max(0.0, wall) * self.speed / 60.0

    def _tick(self) -> None:
        """Advance the job to wherever the clock says it is, then report.

        Driven by reads rather than by a background thread on purpose: a thread
        makes a test wait for real seconds and makes a failure depend on when it
        was looked at. Everything here is a pure function of the clock.
        """
        if self._state == PrinterState.PREPARING:
            # 90 print-seconds of heating and levelling before layer 1.
            if self._elapsed_minutes() >= 1.5:
                self._state = PrinterState.PRINTING
        if self._state == PrinterState.PRINTING:
            if self._elapsed_minutes() >= self.total_minutes:
                self._state = PrinterState.FINISHED
        self._push()

    def _report(self) -> dict:
        """The full `print` object, as the real machine would send it at msg 0."""
        done = min(1.0, self._elapsed_minutes() / self.total_minutes)
        printing = self._state in (PrinterState.PRINTING, PrinterState.PAUSED)
        finished = self._state == PrinterState.FINISHED

        nozzle_target = 220.0 if self._state in PrinterState.BUSY else 0.0
        bed_target = 55.0 if self._state in PrinterState.BUSY else 0.0
        if self._state == PrinterState.PREPARING:
            # Heating: a ramp, not a step. A screen that only ever sees the
            # target has never shown her a printer warming up.
            ramp = min(1.0, self._elapsed_minutes() / 1.5)
            nozzle = 25.0 + ramp * (nozzle_target - 25.0)
            bed = 25.0 + ramp * (bed_target - 25.0)
        elif self._state in (PrinterState.PRINTING, PrinterState.PAUSED):
            nozzle, bed = nozzle_target, bed_target
        else:
            nozzle, bed = 31.0, 32.0    # the real idle numbers, measured

        if self._jitter:
            # The real A1's idle temperatures drift by hundredths every few
            # seconds — that drift is what the diffs are made of. A perfectly
            # still fake never produces a diff at all.
            nozzle += self._random.uniform(-0.4, 0.4)
            bed += self._random.uniform(-0.3, 0.3)

        state_word = {
            PrinterState.IDLE: "IDLE",
            PrinterState.PREPARING: "PREPARE",
            PrinterState.PRINTING: "RUNNING",
            PrinterState.PAUSED: "PAUSE",
            PrinterState.FINISHED: "FINISH",
            PrinterState.FAILED: "FAILED",
        }[self._state]

        report = {
            "command": "push_status",
            "msg": 0,
            "sequence_id": str(self._sequence),
            "gcode_state": state_word,
            "nozzle_temper": round(nozzle, 5),
            "nozzle_target_temper": nozzle_target,
            "bed_temper": round(bed, 5),
            "bed_target_temper": bed_target,
            "hms": [_hms(code) for code in self._faults if "_" in code],
            # A bare decimal fault is a `print_error`, the way the real
            # machine carries its load question (0x07FF8007 = 134184967).
            "print_error": next((int(c) for c in self._faults if c.isdigit()), 0),
        }
        if self._tray_type:
            report["vt_tray"] = {"id": "254", "tray_type": self._tray_type}
        if self.has_ams:
            report["ams"] = {
                "ams_exist_bits": "1",
                "tray_exist_bits": "%x" % (0xF & ~sum(1 << n for n in self._ams_pulled)),
                "tray_now": str(self._ams_active),
                "ams": [{"id": "0", "tray": [dict(t) for t in self._ams_trays]}],
            }
        if self._job:
            report["subtask_name"] = self._job
            report["gcode_file"] = f"{self._job}.gcode.3mf"
            report["total_layer_num"] = self.total_layers
            report["layer_num"] = (self.total_layers if finished
                                   else int(done * self.total_layers))
            report["mc_percent"] = 100 if finished else int(done * 100)
            report["mc_remaining_time"] = (
                0 if finished else int(round(self.total_minutes * (1.0 - done))))
        return report

    def _push(self, full: bool = False) -> None:
        """Deliver a report — as a DIFF unless a full one was asked for.

        This is the method that makes the fake worth having. It keeps its own
        record of what the client has already been told and sends only what
        changed, exactly as the real printer does, so anything built against this
        has already survived the surprise the real machine holds.
        """
        if self._offline or not self._connected:
            return
        self._sequence += 1
        report = self._report()
        if full:
            self._pushed = dict(report)
            payload = dict(report)
            payload["msg"] = 0
        else:
            payload = {k: v for k, v in report.items()
                       if k not in ("command", "msg", "sequence_id")
                       and self._pushed.get(k) != v}
            # The temperatures go in whether or not they changed, because that
            # is what the real machine does — and because leaving them out is a
            # bug a test caught here. A paused printer with nothing moving
            # emitted no diffs at all, so `Snapshot` saw silence and reported it
            # OFFLINE after thirty seconds. A real paused A1 is not silent.
            #
            # Not a guess about the real machine either: in the captured traffic
            # sequence 67 and sequence 69 both carry `bed_temper: 32.8125`. The
            # diff is a periodic SAMPLE, not strictly a change.
            payload["nozzle_temper"] = report["nozzle_temper"]
            payload["bed_temper"] = report["bed_temper"]
            self._pushed.update(payload)
            payload["command"] = "push_status"
            payload["msg"] = 1
            payload["sequence_id"] = str(self._sequence)
        self.snapshot.take({"print": payload}, now=self._now())
        self._last_push = self._now()


def _hms(code: str) -> dict:
    """An HMS record in the printer's own two-halves shape — each half a
    DECIMAL integer, as the real A1 sends it (2026-09-23: attr 50338304 for
    0300_1A00). The fake used to send the hex strings, which is how a lookup
    keyed on hex passed every test and never matched the real machine."""
    parts = code.split("_")
    if len(parts) >= 4:
        try:
            return {"attr": int("".join(parts[:2]), 16), "code": int("".join(parts[2:4]), 16)}
        except ValueError:
            pass
    return {"attr": code, "code": ""}


# --------------------------------------------------------------------------
# Choosing one
# --------------------------------------------------------------------------

def open_printer(config: Optional[Config] = None) -> Printer:
    """The one call the rest of the application makes.

    Returns something that satisfies `Printer` in every case, INCLUDING the case
    where there is no printer. `None` here would push a `if printer is not None`
    into every caller, and the branch nobody writes is the one where the screen
    silently shows nothing.
    """
    cfg = config or load_config()
    if cfg.kind == "fake":
        printer = FakePrinter()
        printer.connect()
        # Ask, here, so a caller gets a printer that knows what it is doing
        # rather than one that has to be prompted. `app/server.py` never
        # prompted it, and against the real machine that meant a screen stuck on
        # "unknown" for ever — the fields it needs never appear in a diff.
        printer.ask_for_everything()
        log.info("printer: using the FAKE printer (kind=fake in printer.json)")
        return printer

    printer = A1Printer(cfg)
    if not cfg.configured:
        # Not an error, and not connected. `status()` says "no printer set up
        # yet" and every command raises `NotConfigured` with that in the message.
        log.info("printer: not configured yet (%r)", cfg)
        return printer
    try:
        printer.connect()
        # The same prompt the fake gets, and for the reason the fake's comment
        # gives: without it the screen waits for fields that only ever arrive in
        # a full report. Its own failure is not this call's failure, though — a
        # printer that could not be asked is still a printer object, and
        # `status()` already reports the silence honestly.
        try:
            printer.ask_for_everything()
        except PrinterError as exc:
            log.warning("printer: connected but could not ask for a full "
                        "report: %s", scrub(str(exc)))
    except PrinterError as exc:
        # Reachability is not this call's job to guarantee. Coming back with a
        # printer that reports itself offline is more useful than raising into a
        # server that then has no printer object at all.
        printer._trouble = str(exc)
        log.warning("printer: %s", scrub(str(exc)))
    return printer


if __name__ == "__main__":
    # `python printer.py`         — watch the REAL printer. Read-only.
    # `python printer.py --fake`  — watch a whole job run on the fake, in a
    #                               couple of minutes, with no hardware.
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if "--fake" in sys.argv:
        f = FakePrinter()                    # 600x: the real 19h45m planter in ~2 min
        f.connect()
        f.send(__file__)                     # any file will do; it is not read
        f.start(Path(__file__).name, job_name="planter")
        while True:
            time.sleep(1.0)
            s = f.status()
            print(f"  {s.state:<10} {s.spoken()}")
            if s.state in (PrinterState.FINISHED, PrinterState.FAILED):
                break
        raise SystemExit(0)

    cfg = load_config()
    print(f"  config      {cfg}")
    print(f"  file        {CONFIG_PATH}")
    if not cfg.configured:
        print("\n  Not configured. Fill in printer.json and run this again.\n")
        raise SystemExit(0)
    p = A1Printer(cfg)
    p.connect()
    for _ in range(20):
        time.sleep(1.0)
        s = p.status()
        print(f"  {s.state:<12} complete={s.complete}  "
              f"nozzle={s.nozzle}  bed={s.bed}  {s.spoken()}")
    p.close()
