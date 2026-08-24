# Logging

baresip-python follows the standard library convention for libraries: it logs into the
`baresip.*` logger hierarchy (`baresip.runtime`, `baresip.events`, ...), attaches nothing
but a `NullHandler`, and never configures output. Handlers, formatting, and destinations
are the application's decision.

## Correlation fields

Records carry structured context in `extra` where it is known:

| Field | Meaning |
|---|---|
| `seq` | Command sequence number (one async API call → one command) |
| `cmd` | Native command id the record relates to |
| `event` | Stack event name the record relates to |
| `call` | The call handle, stable for the call's lifetime |
| `sip_call_id` | The SIP Call-ID header value |
| `peer` | The remote party's URI |

`sip_call_id` is the correlation spine: it appears in Python-side call records, in the
native stack's own lines, and in every SIP trace message, and it is the same value the far
end's SIP server logs — the one key that ties a call's story together across systems. A
JSON formatter or log aggregator can lift these fields directly off the record.

## The native stack's logs

The C stack's own logging is captured and re-emitted under `baresip.native` (nothing is
ever written to stdout/stderr by the library). `Config(native_log_level=...)` sets the
lowest severity captured, from the stack's very first line; it can be changed while running
with `await runtime.set_native_log_level(...)`.

A full SIP message trace — every message sent and received, verbatim — is available under
`baresip.native.sip` at DEBUG level: enable it with `Config(sip_trace=True)` or toggle it
live with `await runtime.set_sip_trace(True)`. Treat trace output as sensitive: it contains
call metadata for everyone who calls, and the digest material from authentication
exchanges.

## Per-call DEBUG: `PerCallFilter`

Process-wide DEBUG on a busy server is unusable, but the one misbehaving call is exactly
what needs it. That is a *filter* problem, not a level problem, and `PerCallFilter` solves
it: records at INFO and above always pass; DEBUG records pass only for calls you are
tracing.

```python
import logging
from baresip import PerCallFilter

handler = logging.StreamHandler()
trace = PerCallFilter()
handler.addFilter(trace)
logging.getLogger("baresip").addHandler(handler)
logging.getLogger("baresip").setLevel(logging.DEBUG)

trace.trace("a84b4c76e66710@10.0.0.1")   # this one call goes verbose
trace.untrace("a84b4c76e66710@10.0.0.1")  # and back
```

`trace()` accepts either the SIP Call-ID (`call.call_id`) or the call handle
(`call.handle`).

## Bridging into loguru

Applications that log through [loguru](https://github.com/Delgan/loguru) — pipecat among
them — can route the `baresip` hierarchy into it with the standard intercept handler:

```python
import inspect
import logging

from loguru import logger


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        # Standard-library `extra` fields do not cross into loguru on their
        # own; re-bind the correlation fields so sinks keep seeing them.
        fields = ("seq", "cmd", "event", "call", "sip_call_id", "peer")
        bound = logger.bind(**{f: getattr(record, f) for f in fields if hasattr(record, f)})
        bound.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


logging.getLogger("baresip").addHandler(InterceptHandler())
logging.getLogger("baresip").setLevel(logging.INFO)
```

With the re-binding above, the correlation fields land in loguru's `record["extra"]`, so
`sip_call_id` and friends stay available to loguru sinks and formatters.
