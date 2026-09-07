---
name: Bug report
about: Something misbehaves — a crash, a wrong result, a call that won't establish
---

**What happened, and what did you expect?**

**Version and platform**

- baresip-python version (`python -c "import baresip; print(baresip.__version__)"`):
- OS and architecture:
- Python version:

**How to reproduce**

The smallest script that shows it. If it needs a SIP peer, the bundled
FreeSWITCH bench (`make bench-up`, see bench/README.md) makes reports
reproducible for everyone.

**Native logs**

Construct the runtime with `Runtime(native_log_level="debug")` (and
`Config(sip_trace=True)` if signaling is involved), attach the output —
the stack's own log usually names the failure long before the Python
surface does.
