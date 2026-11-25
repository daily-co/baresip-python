# Hold and call transfer

Transfers are the one part of SIP where the *protocol's* idea of success is
surprising, and where an API that hides the surprises produces bots that
misbehave on real switches. This guide explains the model: hold, blind
transfer, attended transfer, and what to do when the far end asks *you* to
transfer.

## Hold and resume

`await call.hold()` pauses the media and tells the far end with a re-INVITE;
`await call.resume()` reverses it. Both return once the re-INVITE is on the
wire — the peer's answer arrives asynchronously as events. `call.is_on_hold`
tracks your own intent; the far end putting *you* on hold is a different fact,
tracked in `call.remote_on_hold` and delivered as `CALL_HOLD` / `CALL_RESUME`
events.

Facts worth knowing before relying on it:

- Hold works on **established** calls only.
- Peer-hold detection is **audio-based**: a far end that holds only its video
  is invisible to `remote_on_hold`.
- If another session refresh is already in flight (the far end re-INVITEd at
  the same moment — "glare"), `hold()`/`resume()` raises a `BaresipError`
  saying so; the state is left unchanged and a short retry succeeds.
- A hold/resume renegotiation can rebuild the call's audio streams. Code
  reading or writing `call.audio` concurrently must be ready for
  `AudioRestarted` — the next operation rebinds to the new streams. This is
  the same contract as any mid-call renegotiation.

## Blind transfer

`await call.transfer(uri)` asks the far end to call `uri` instead of talking
to us (an in-dialog REFER). The part every integrator trips over:

**On success, your call closes.** That is not an accident of the binding — it
is how the protocol works. The far end reports "my new call was answered"
through the REFER subscription, and the correct next step is to leave the old
call, so the stack ends it. `transfer()` returning normally *means* the call
is over. On failure the call survives, still established, and `transfer()`
raises `TransferFailed` carrying the SIP status the far end reported.

Two practical rules:

- **Hold first.** `transfer()` does not hold the call for you; until the far
  end's new call connects, it keeps hearing your media. Holding before
  transferring is the conventional courtesy, and what example 07 does.
- **One at a time.** A call carries at most one outstanding transfer;
  starting a second while one is pending raises.

A switch in the path adds its own rule: most refuse to transfer a leg that is
not part of a bridge (FreeSWITCH answers the REFER with a 403 for a one-legged
call). A bot that answers a call and immediately tries to transfer it will hit
this; the caller must be bridged through the switch to the bot first.

### Passing context

A bot that hands a caller to a human agent or a queue usually knows things the
agent would otherwise ask again. `transfer(uri, headers={...})` sends them
along:

```python
await call.transfer(
    "sip:support@pbx.example.com",
    headers={"User-to-User": "56a390f3d2b7310023a2;encoding=hex", "X-Case": "1234"},
)
```

The headers are attached to the address the far end is told to call
(`Refer-To: <sip:support@pbx.example.com?User-to-User=...>`), which is where
SIP puts context meant for the party being called. The far end sets them on
the call it places, and that party reads them as ordinary headers. The library
does the escaping the address form needs.

`User-to-User` is the standard header for this. Contact-center platforms read
it from the transfer target, usually as hex data ending in `;encoding=hex`;
check the platform's own format. A custom header works the same way.

**Delivery is the far end's choice.** Nothing obliges the party being
transferred to copy the headers, and practice varies:

- Our own agents carry them (see "Receiving a transfer" below); the test suite
  follows the context to the called party, directly and through a proxy.
- FreeSWITCH places the new call without them. It keeps the whole target in
  the channel variable `sip_refer_to`, so a dialplan can forward them.
- Other switches, border controllers and carriers each have their own rule,
  often a setting. When context does not arrive, look at what the party being
  transferred sent on the new call.

Headers that define the call itself (From, To, Call-ID, CSeq, Via, Route,
Record-Route, Contact, Max-Forwards and the content headers) are refused.

## Attended transfer

`await call.attended_transfer(consult_call)` splices the two far ends of two
established calls together and takes you out of the path: the REFER carries a
`Replaces` header naming the consultation dialog. The method holds both legs
first (already-held legs are left alone), verifies the peer advertises
`Replaces` support, and raises `TransferFailed` up front when it does not.
Success ends **both** of your legs — the two other parties now talk directly.

The receptionist pattern — answer, consult, optionally bridge the audio in
Python while both calls are up, then splice and drop out — is
[example 07](../examples/07_warm_transfer.py). The bridge half needs no SIP at
all (`call.audio` reads and writes compose into a two-way pump), which is also
the fallback when a peer does not support `Replaces`: keep bridging; the
parties still hear each other, you just stay in the path.

## Receiving a transfer

When the far end REFERs *you*, the stack has already accepted the REFER
transaction before your code hears about it (SIP obliges an immediate answer),
and the call parks until someone decides. The decision is governed by
`transfer_policy` on `UserAgent.create`:

- `"manual"` (default): the parsed request is delivered as a
  `TransferRequest` (target URI, raw Refer-To, `Replaces` presence, method,
  and the headers attached to the target) via `call.on_transfer_request`. The application calls
  `await call.accept_transfer()` — which dials the target and returns the new
  `Call`; the original leg closes once the new call establishes and the
  transferor is notified — or `await call.reject_transfer()`, which sends the
  failing outcome and leaves the call established.
- `"auto"`: every transfer is executed immediately.
- `"reject"`: every transfer is refused immediately.

Manual is the default for a reason: **a REFER is the far end instructing your
agent to place a call.** An agent that blindly complies can be steered into
dialing premium-rate or arbitrary destinations by anyone who can get a call
established with it. Auto-accept is for closed systems where every peer is
trusted.

The new call carries the context the transferor attached to the target.
`TransferRequest.headers` holds every header attached; `accept_transfer()` and
the `"auto"` policy set the `User-to-User` and `X-` headers among them on the
call they place. The target comes from the far end, which your trunk does not
vouch for, so nothing else is taken from it: an identity header
(`P-Asserted-Identity`), a `Diversion` or a `Require` attached to the target
is left out, with a warning in the log.

`accept_transfer(headers={...})` adds headers of the application's own, which
may be any header that does not define the call itself: a name the request
also has takes the value given, and a value of `None` leaves that header out.
To read context on a call you *receive*, list the header names in
`Config(expose_headers=[...])`.

Not deciding is also a decision, and a bad one: an ignored transfer times out
on the transferor's side after about a minute and the parked call stays
parked. Policy `"manual"` obliges the application to answer.

Scope note: this applies to the in-dialog REFER of a real transfer. An
out-of-dialog REFER (no call attached) is surfaced as an informational event
and never executed, regardless of policy.
