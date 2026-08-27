# SIP trunks: register-less dialing and separate auth credentials

The examples register a user agent against a switch, softphone-style. Real
deployments often connect to a **trunk** instead — a provider endpoint that
does not want a registration at all, and that may authenticate with a
username that is not the one in your SIP URI. Two `Account` fields cover
this.

## Dialing without registering: `reg_interval=0`

```python
account = Account(
    user="+15551230000",
    domain="my-trunk.example.com",
    password="...",
    reg_interval=0,
)
```

`reg_interval=0` declares the account register-less: `dial()` works
immediately, no REGISTER is ever sent, and `register()` /`unregister()` fail
fast with `RegistrationError` instead of silently doing nothing and timing
out. Outbound authentication still happens per-request — the INVITE is
digest-challenged and answered like any other.

The flip side of not registering: the provider cannot *find* you through a
registration. Inbound calls on a trunk arrive because the provider is
configured with your address (an origination URI, an IP ACL) — your process
must be reachable and listening where the provider expects it.

## When the login is not the URI user: `auth_user`

Digest authentication is a username and password, and nothing in SIP requires
that username to equal the user part of your URI. Credential-list trunks
commonly key the credential store by an account identifier while your URI
carries a phone number:

```python
account = Account(
    user="+15551230000",          # what your From/Contact URI says
    domain="my-trunk.example.com",
    password="...",
    auth_user="acct_7f3a",        # what the provider's credential list expects
    reg_interval=0,
)
```

Unset, `auth_user` falls back to `user` — the softphone case, where they are
the same thing.

## The two shapes, concretely (Twilio's split as the example)

Most large providers offer both shapes; Twilio's product split names them
cleanly:

- **SIP Domains** (`yourname.sip.twilio.com`): endpoint-style. Your agent
  registers (or authenticates per-call against a credential list) and dials
  through the domain — the ordinary `Account` with a registration, exactly
  like the examples.
- **Elastic SIP Trunking**: trunk-style. No registration exists in the
  product at all — termination (your outbound calls) authenticates against a
  credential list or your IP; origination (inbound to you) is Twilio sending
  INVITEs to the address you configured. This is `reg_interval=0`, usually
  with `auth_user` naming the credential-list username.

If a provider document says "registration not supported" or asks for an
"origination URI", you are in the second shape.

## A loopback footnote

Dialing an address-literal target that no local interface can reach raises
`NoLocalAddressError` up front, naming the target. The classic way to hit it
is a loopback target (`sip:...@127.0.0.1`) from a runtime whose config does
not pin `net_interface 127.0.0.1` — the bench examples pin it for exactly
this reason.
