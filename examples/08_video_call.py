#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""A video call: your camera out, the far end's video in.

The platform's camera driver is picked automatically — avcapture on
macOS (the OS will ask for camera permission on first run), v4l2 on
Linux. Received video shows live in a window — in color when numpy
is installed (the dev environment has it), otherwise grayscale (the
luma plane drawn directly, dependency-free) — and can be recorded in
full color as Y4M with ``VIDEO_RECORD``.

Two instances make a self-contained video call, talking directly to
each other (a switch in the middle would need video support of its own
— the bundled FreeSWITCH bench has none and declines video):

    SIP_LISTEN=127.0.0.1:5070 python 08_video_call.py
    SIP_LISTEN=127.0.0.1:5072 SIP_DIAL=sip:alice@127.0.0.1:5070 python 08_video_call.py

(127.x binds mark the local demo; bind 0.0.0.0 to take calls from other
machines — the loopback pin then stays off so real interfaces work.)

Each window then shows the other side's camera. Against a real,
video-capable SIP service, register instead:

    SIP_USER=1001 SIP_PASS=secret SIP_DOMAIN=sip.example.com python 08_video_call.py
    SIP_DIAL=sip:someone@sip.example.com python 08_video_call.py   # or dial out

Environment:

    SIP_LISTEN    direct mode: bind here, skip registration, take calls directly
    SIP_TRANSPORT "udp" (default), "tcp", or "tls" — registrar mode only
    VIDEO_SOURCE  capture override, "module,device" — e.g. "v4l2,/dev/video1",
                  or "vidmem" to send synthetic moving bars instead of a camera
    VIDEO_RECORD  path to write received video as Y4M (plays in mpv/ffplay)
    VIDEO_SIZE    "WxH" (default 640x480)

Rendering happens HERE, in the application, on its main thread — that
is deliberate. macOS confines window and event handling to the process
main thread (AppKit's rule, which every GUI toolkit inherits), and in
this library the main thread belongs to you, not to the SIP stack. An
asyncio task calling ``tk.update()`` runs on exactly that thread, so a
stdlib Tk window works; a rendering thread inside the library could
not. Headless machines: set VIDEO_RECORD and close with Ctrl-C.
"""

import asyncio
import contextlib
import logging
import os
import platform
import signal

from baresip import Account, Config, Event, Runtime, UserAgent, VideoNotActive, VideoRestarted

DOMAIN = os.environ.get("SIP_DOMAIN", "127.0.0.1:15060")
USER = os.environ.get("SIP_USER", "alice")
PASSWORD = os.environ.get("SIP_PASS", "")
DIAL = os.environ.get("SIP_DIAL")
LISTEN = os.environ.get("SIP_LISTEN")
TRANSPORT = os.environ.get("SIP_TRANSPORT", "udp")
RECORD = os.environ.get("VIDEO_RECORD")
CAMERA = "avcapture" if platform.system() == "Darwin" else "v4l2"
SOURCE = os.environ.get("VIDEO_SOURCE", CAMERA)

# The mode matrix — every combination is meaningful:
#   (neither)            wait for a call, registered at SIP_DOMAIN
#   SIP_DIAL             place a call, registered at SIP_DOMAIN
#   SIP_LISTEN           wait for a call directly, no registrar
#   SIP_LISTEN+SIP_DIAL  place a call directly, no registrar (the caller
#                        needs its own bind too — ports must differ)
if TRANSPORT not in ("udp", "tcp", "tls"):
    raise SystemExit(f"SIP_TRANSPORT must be udp, tcp or tls, got {TRANSPORT!r}")
if DIAL and not DIAL.startswith("sip:"):
    raise SystemExit(f"SIP_DIAL must be a full URI like sip:alice@host:port, got {DIAL!r}")
try:
    W, H = (int(v) for v in os.environ.get("VIDEO_SIZE", "640x480").split("x"))
    if W < 2 or H < 2:
        raise ValueError
except ValueError:
    raise SystemExit(
        f"VIDEO_SIZE must be WxH, e.g. 640x480, got {os.environ.get('VIDEO_SIZE')!r}"
    ) from None


async def synthetic_bars(call):
    """When VIDEO_SOURCE=vidmem: feed moving bars instead of a camera."""
    luma_row = (bytes(range(0, 256, 8)) * (W // 32 + 1))[:W]
    chroma = b"\x80" * (2 * (W // 2) * (H // 2))
    shift = 0
    while True:
        row = luma_row[shift:] + luma_row[:shift]
        try:
            call.video.write_frame(row * H + chroma)
        except (VideoNotActive, VideoRestarted):
            pass
        shift = (shift + 4) % W
        await asyncio.sleep(1 / 15)


try:
    import numpy as _np
except ImportError:
    _np = None


def to_ppm(frame) -> bytes:
    """One frame as a PPM/PGM image for Tk: color via numpy, else the
    luma plane directly — P5 (binary PGM) is width x height single-plane
    bytes, exactly the first W*H of packed I420."""
    w, h = frame.width, frame.height
    if _np is None:
        return f"P5 {w} {h} 255 ".encode() + frame.data[: w * h]
    y = _np.frombuffer(frame.data, _np.uint8, w * h).reshape(h, w).astype(_np.int32)
    u = _np.frombuffer(frame.data, _np.uint8, (w // 2) * (h // 2), w * h)
    v = _np.frombuffer(frame.data, _np.uint8, (w // 2) * (h // 2), w * h + (w // 2) * (h // 2))
    u = u.reshape(h // 2, w // 2).repeat(2, 0).repeat(2, 1).astype(_np.int32) - 128
    v = v.reshape(h // 2, w // 2).repeat(2, 0).repeat(2, 1).astype(_np.int32) - 128
    r = _np.clip(y + ((1436 * v) >> 10), 0, 255)
    g = _np.clip(y - ((352 * u + 731 * v) >> 10), 0, 255)
    b = _np.clip(y + ((1815 * u) >> 10), 0, 255)
    rgb = _np.dstack((r, g, b)).astype(_np.uint8)
    return f"P6 {w} {h} 255 ".encode() + rgb.tobytes()


async def show_video(call, tk_root, label):
    """Pull received frames; paint the luma plane, append Y4M if asked.

    Runs as an asyncio task — which IS the main thread, where Tk must
    live. ``update()`` pumps the window's events each pass.
    """
    import tkinter as tk

    # A plain blocking file is fine here: Y4M appends are tiny next to
    # the 30 ms frame cadence, and a demo needs no aiofiles.
    y4m = open(RECORD, "wb") if RECORD else None  # noqa: ASYNC230, SIM115
    wrote_header = False
    photo = None
    try:
        while True:
            try:
                frame = call.video.read_frame()
            except (VideoNotActive, VideoRestarted):
                frame = None  # media not up yet, or renegotiating
            if frame is not None:
                if y4m:
                    if not wrote_header:
                        y4m.write(
                            f"YUV4MPEG2 W{frame.width} H{frame.height} "
                            f"F15:1 Ip A1:1 C420\n".encode()
                        )
                        wrote_header = True
                    y4m.write(b"FRAME\n" + frame.data)
                if tk_root is not None:
                    photo = tk.PhotoImage(data=to_ppm(frame))
                    label.configure(image=photo)
            if tk_root is not None:
                tk_root.update()
            await asyncio.sleep(1 / 30)
    finally:
        if y4m:
            y4m.close()


async def main():
    # Surface the stack's warnings (a camera that failed to start, a
    # declined video stream) — without this they are invisible.
    logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    runtime = Runtime()
    conf = Config(video_source=SOURCE, video_size=(W, H), video_fps=15.0).render()

    def loopback(hostport: str) -> bool:
        host = hostport.rsplit(":", 1)[0]
        return host.startswith("127.") or host == "localhost"

    domain = DOMAIN
    if LISTEN:
        # Direct mode: no registrar. The account exists for the URI
        # identity only; peers reach us at SIP_LISTEN.
        domain = LISTEN.replace("0.0.0.0", "127.0.0.1")
        conf = f"sip_listen {LISTEN}\n" + conf
    # The stack skips loopback in interface discovery unless pinned, so
    # loopback-bound traffic needs the pin — but pinning with a real
    # peer would leave no route out at all. Pin only when everything in
    # sight is loopback: the registrar in registrar mode; in direct
    # mode, the bind address (127.x = a local demo; 0.0.0.0 = reachable
    # from outside) and the dial target when there is one.
    if LISTEN:
        pin = loopback(LISTEN) and (DIAL is None or "@127." in DIAL or "127." in DIAL)
    else:
        pin = loopback(domain)
    if pin:
        conf = "net_interface 127.0.0.1\n" + conf

    print(f"******* final config:\n{conf}\n*******\n", flush=True)
    await runtime.start(conf)
    try:
        ua = await UserAgent.create(
            runtime,
            Account(
                user=USER,
                password=PASSWORD,
                domain=domain,
                reg_interval=0 if LISTEN else 600,
                transport=TRANSPORT,
            ),
        )
        if not LISTEN:
            await ua.register()

        if DIAL:
            call = await ua.dial(DIAL, video=True)
            await call.wait_established()
            print(f"connected to {DIAL}")
        else:
            where = f"direct on {LISTEN}" if LISTEN else f"registered as {USER}@{domain}"
            print(f"{where}; waiting for a call ...")
            incoming: asyncio.Queue = asyncio.Queue()
            ua.on_incoming(incoming.put_nowait)
            call = await incoming.get()
            await call.answer(video=True)
            print(f"answered {call.peer}")

        tk_root = label = None
        # uv-managed Pythons ship their own Tcl/Tk but do not always
        # tell Tk where it lives; point it there when unset.
        import sys
        from pathlib import Path

        tcl_dir = Path(sys.base_prefix) / "lib" / "tcl8.6"
        if "TCL_LIBRARY" not in os.environ and tcl_dir.is_dir():
            os.environ["TCL_LIBRARY"] = str(tcl_dir)
            os.environ["TK_LIBRARY"] = str(tcl_dir.with_name("tk8.6"))
        try:
            import tkinter as tk
        except ImportError:
            tk = None
        if tk is not None:
            try:
                tk_root = tk.Tk()
                tk_root.title(f"08_video_call — {USER}")
                # A gray placeholder gives the window its real size at
                # once — an imageless label is a barely-visible sliver.
                header = f"P5 {W} {H} 255 ".encode()
                placeholder = tk.PhotoImage(data=header + b"\x60" * (W * H))
                label = tk.Label(tk_root, image=placeholder)
                label.pack()
                tk_root.lift()
                tk_root.update()
            except tk.TclError as exc:  # headless box, no display
                tk_root = label = None
                print(f"no window ({exc}); set VIDEO_RECORD to capture video")
        else:
            print("tkinter unavailable; set VIDEO_RECORD to capture video")
        if tk_root is not None and _np is None:
            print("grayscale preview (pip install numpy for color)")

        def task_died(t):
            if not t.cancelled() and t.exception() is not None:
                print(f"task failed: {t.exception()!r}")

        async def report():
            # A heartbeat that says what is actually flowing, so a blank
            # window is never a mystery: sent counts the frames handed to
            # the encoder, read the frames this side decoded.
            from baresip import VideoNotActive as _VNA

            while True:
                await asyncio.sleep(3)
                try:
                    i = call.video.info()
                    print(
                        f"video: sent={i.tx_frames} read={i.rx_frames} "
                        f"dropped={i.rx_dropped} {i.width}x{i.height}@{i.fps:g}"
                    )
                except _VNA:
                    print("video: not active (no video negotiated on this call)")

        tasks = [
            asyncio.ensure_future(show_video(call, tk_root, label)),
            asyncio.ensure_future(report()),
        ]
        if SOURCE.split(",")[0] == "vidmem":
            tasks.append(asyncio.ensure_future(synthetic_bars(call)))
        for task in tasks:
            task.add_done_callback(task_died)

        closed = asyncio.get_running_loop().create_future()
        call.on(
            lambda e: (
                closed.set_result(None)
                if e.event is Event.CALL_CLOSED and not closed.done()
                else None
            )
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig, lambda: closed.set_result(None) if not closed.done() else None
            )
        await closed
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
