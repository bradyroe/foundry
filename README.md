# Foundry

**A safety-first Python client for driving a Bambu Lab A1 3D printer over the
local network: send a job, start it, watch it, pause it, stop it. No cloud
account, no vendor app.**

Built with AI tools. I planned it, directed the build, and tested it.

Foundry talks to the printer the way the official slicer does on a LAN: MQTT
over TLS for commands and status, implicit FTPS for uploading the job file.
It was built against a real A1, by reading what the machine actually sends.
Several of those messages don't match the published examples, and the source
comments say exactly where and how.

This repository is the printer layer of a larger personal app that designs
parts, slices them and prints them. That layer is the part with the
interesting engineering, so that's what's here.

---

## Why "safety-first"

Software that can start a 3D printer can waste a spool of filament, print over
a part someone left on the bed, or run a hot nozzle with nobody watching. So:

- **Printing and uploading are off by default.** `allow_real_prints` and
  `allow_uploads` must be switched on in the config before any command that
  makes the machine act will go out. A test proves the default really is off.
- **Stopping is never blocked.** Pause and stop deliberately skip that
  interlock. A print can be started from the printer's own screen while
  Foundry is watching, so the brake has to work on a job Foundry didn't start.
- **"Unknown" is not "idle".** After connecting, the printer knows things the
  client doesn't. Until a full report arrives the state is `UNKNOWN`, never a
  guessed `IDLE`. Guessing is how a screen says "ready to print" about a
  machine that's halfway through a job.
- **The access code never leaks.** It's never logged, never put in an
  exception, never shown. `scrub()` also runs over every log line and error
  this module produces, because the leaks that matter are the ones nobody
  wrote: a library quoting a URL with credentials in it, a traceback printing
  a local variable.

## What the real printer taught me

These are the protocol facts that broke the obvious implementation. Each is
pinned by a test.

1. **It sends sparse diffs, not status objects.** Every published example
   shows a full status message. A real idle A1 sent fifteen messages in 45
   seconds, and every one looked like
   `{"print":{"bed_temper":32.9,"command":"push_status","msg":1}}`. A client
   that reads `payload["print"]["gcode_state"]` crashes on nearly every real
   message. Foundry accumulates state in a `Snapshot` that merges each diff,
   and a field nobody has mentioned stays `None`, which is not the same as 0.
2. **A wildcard subscription silently gets nothing.** `device/+/report` is
   accepted and granted by the printer's broker, then delivers no messages.
   That looks exactly like a printer that went quiet. Foundry subscribes to
   the exact serial number.
3. **Delivery is not agreement.** MQTT confirms the start command arrived,
   never that the printer accepted it. The only honest confirmation that a
   print began is the status changing, so the caller is made to watch for it.
4. **Errors latch.** A failed job leaves its error code set, and while it's
   set the printer refuses every remote start. Foundry clears a latched error
   before starting, but never while a print is paused, because a paused job's
   error is a question for the owner, not leftover debris.
5. **Long-lived connections stop being heard.** Starts sent on a connection
   that had been open for hours were ignored, while the first start after a
   fresh connection worked every time. Foundry reconnects right before each
   start.
6. **Resume needs the error cleared first.** A job paused by a fault stays
   paused through any number of `resume` commands until the error is cleared.

## Testing without a printer

`FakePrinter` speaks the same interface with a simulated clock, so a monitoring
screen can be built and every failure state exercised (paused, failed, offline,
out of filament) with no hardware and no filament.

```
pip install -r requirements.txt
pytest
```

145 tests, all offline. They run on every push.

## Using it

```python
from pathlib import Path

import printer

cfg = printer.Config(host="192.168.1.50", serial="YOUR_SERIAL",
                     access_code="FROM_THE_PRINTER_SCREEN",
                     allow_real_prints=True, allow_uploads=True)
a1 = printer.A1Printer(cfg)
a1.connect()
a1.ask_for_everything()            # ask for a full report, not just diffs

remote = a1.send(Path("part.gcode.3mf"))   # FTPS upload
a1.start(remote, job_name="part")
print(a1.status())                 # watch for PREPARING / PRINTING
```

The host, serial and access code come from the printer's own screen
(Settings, WLAN, and LAN-only mode).

## A note on TLS

The printer presents a self-signed certificate for a hostname it doesn't
have, so there is nothing to verify it against and certificate verification
is off. The connection is still encrypted. That's a real limit of the device,
and it's said plainly in the code rather than hidden.
