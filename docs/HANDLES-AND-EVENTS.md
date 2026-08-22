# Handles and events: how Python talks about stack objects

This explains the machinery in `src/_native/shim.c` (handle table, event trampoline, JSON
payloads) and its Python side (`src/baresip/events.py`, `Runtime.subscribe`). It exists because
the design only makes sense once you see the three problems it is solving at the same time.

## The three problems

baresip reports things asynchronously — an incoming call, a registration result, a hangup —
through a callback that hands us raw C pointers: a `struct ua *`, a `struct call *`, a
`struct sip_msg *`. Those pointers are unusable from Python for three independent reasons:

1. **Lifetime.** The objects can be freed the moment the callback returns. On a `CALL_CLOSED`
   event the call is already on its way out, and the `sip_msg` (where From, To, Call-ID and
   header values live) points into a transport receive buffer that is recycled immediately.
2. **Threads.** baresip has no internal locking; every object is owned and mutated by the
   dedicated re thread. Python code runs on the asyncio thread. Even a pointer to a live,
   reference-counted object is unsafe to read from there — that is a data race, not a lifetime
   bug, and no reference count fixes it.
3. **Meaning.** Memory staying allocated does not mean the value stays put. A call mutates as it
   progresses; an event is a statement about the instant it fired.

The answer has two halves: **snapshots** for reading and **handles** for commanding.

## Reading: events are snapshots

At the instant a stack event fires, still inside the callback and on the re thread — the only
moment the pointers are guaranteed valid — `emit_stack_event()` copies everything a listener
could want into a single JSON string: the event name, the peer URI, From/To, the Call-ID, and
any headers the application allowlisted via `Config.expose_headers`. When that function returns,
the JSON is self-contained; no pointer into any baresip object survives it.

The JSON writer treats every value as hostile network input. It validates UTF-8 byte by byte
(including the overlong and surrogate encodings), preserves invalid bytes as `\u00XX` escapes,
caps each value at 1 KB cut only on a code-point boundary (setting `"truncated"`), and falls
back to a minimal valid payload if the whole event would overflow the buffer. The invariant is
that Python's `json.loads` **never** fails on what we emit — one raw hostile byte passed through
would otherwise poison the entire event.

On the Python side, `Runtime` parses the JSON and hands every subscribed listener one frozen
`StackEvent` — a typed, immutable snapshot that stays valid forever, no matter what C frees.

## Commanding: handles instead of pointers

Reading is only half the job: Python must also be able to act on an object later — "answer
*this* call", "hang up *this* call" — and for that it needs a durable name. That name is a
**handle**: a 32-bit integer, never a pointer, packed as

```
 31        24 23                     0
+------------+------------------------+
| generation |       slot index       |
+------------+------------------------+
```

Handles index a 1024-entry table (`g_slots`; slot 0 stays empty so handle 0 means "none"). Each
occupied slot stores three things:

- **`ptr`** — the object, held via `mem_ref()`. libre memory is reference-counted and the
  destructor only runs when the count hits zero, so the table's reference guarantees the stored
  pointer is never dangling — even if baresip drops its own references first. Without it, a
  far-end hangup racing an `answer` command would leave the slot pointing at freed memory, and
  even *validating* such a pointer is undefined behavior.
- **`type`** — UA, call, … so a call handle cannot be replayed against a UA operation.
- **`gen`** — an 8-bit counter of how many times this slot has been freed.

A command executes on the re thread: `handle_lookup()` turns the handle back into a pointer
there, validates it, and calls baresip. Python itself never dereferences anything.

## The generation: expiring old handles

Slots get reused — call A ends, call B lands in the same slot. If the handle were just the slot
index, a stale handle for A (still sitting in application state or a queued event) would now
silently name B, and "hang up A" would disconnect a stranger's live call.

The generation closes that hole. It changes in exactly one place: `slot_clear()` increments it
on every free. Creating an entry does not touch it — `handle_create()` just stamps the slot's
*current* generation into the handle it returns. A lookup then compares the handle's stamped
generation against the slot's live one; any mismatch fails. One free therefore expires every
handle ever issued for that slot, and the stale handle degrades to a typed `STALE_HANDLE` error
instead of acting on the wrong object.

Only two places may free a slot: `CALL_CLOSED` processing in the event handler, and the teardown
drain (which releases every table reference before `ua_close()` so baresip can actually free its
objects). Concentrating frees in two sites is what keeps "who invalidates whom" answerable.

**The one honest limitation:** the counter is 8 bits, so after exactly 256 frees of one slot it
wraps, and a handle held across all 256 reuses validates falsely — *if* a live object occupies
the slot at that moment (an empty slot never validates, whatever its generation). Accepted by
design and pinned by `test_generation_wraparound`, so any change to the packing shows up as a
failing test rather than a surprise.

## One channel, two id spaces

Everything C tells Python arrives through a single callback, `bp_event_h(ev, handle, payload)`,
but it carries two unrelated kinds of traffic, and the `handle` argument means something
different in each — so the event id alone must do the routing:

| range | meaning | `handle` is | routed to |
|---|---|---|---|
| `ev < BP_EV_BASE` (1000) | a command you issued completed (`DONE`, `STALE_HANDLE`, …) | that command's sequence number | the pending future for that command |
| `ev >= BP_EV_BASE` | a stack event fired (`BP_EV_BASE + bevent value`) | the object handle | every `Runtime.subscribe` listener, as a `StackEvent` |

Raw bevent values (0–37) and completion codes (1–3) overlap, so stack events are shifted by
`BP_EV_BASE` into their own disjoint range. One comparison routes unambiguously, and object
handles can never be mistaken for command sequence numbers.

The Python `Event` enum mirrors the bevent numbering, which upstream treats as bare enum
positions and has inserted into before. `test_event_numbering_matches_the_compiled_stack`
cross-checks the enum against the compiled stack's own `bevent_str` table on every run, so a
submodule bump that shifts the numbering fails a test instead of silently relabeling every
event.
