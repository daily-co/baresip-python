# Video: frames in, frames out

Video in this library follows the audio design: a call carries it, and
Python exchanges it as plain bytes through `call.video` — no rendering,
no camera abstraction beyond the drivers, no framework assumptions.
This guide is the contract and the reasoning behind its edges.

## Enabling video on a call

Nothing about a runtime is video until a call asks: `dial(uri,
video=True)` offers it, `answer(video=True)` accepts it, and VP8 is the
codec. A peer that declines (an audio-only switch, a phone) leaves a
perfectly good audio call — `call.video` operations then raise
`VideoNotActive`, which is the *typed answer* to "did video happen?",
not a failure of the call.

## Adding and removing video mid-call

`call.set_video_direction("sendrecv" | "sendonly" | "recvonly" |
"inactive")` renegotiates the call's video with a re-INVITE;
`add_video()` and `remove_video()` are the sendrecv/inactive shorthands.
The one requirement is that the call carries a video stream to
redirect: dial with `video="inactive"` to negotiate the stream without
activating it — the call is audio-only on the wire until someone brings
video up — or start from any `video=True` call. A call dialed with
`video=False` has no video stream and can never add one.

The peer side needs no special handling: an incoming direction change
arrives as a normal renegotiation, media starts or stops, and a bound
`CallVideo` sees `VideoRestarted` once and rebinds — the same epoch
contract hold/resume exercises. Removing video leaves a working audio
call, with `call.video` back to raising `VideoNotActive`.

Right after establishment the ACK may still be in flight; a direction
change then raises with "retry shortly" — wait briefly and call it
again.

`examples/09_midcall_video.py` runs the whole story as a two-terminal
demo: caller adds and removes video mid-call, callee accepts with
nothing but `answer(video=True)`.

## The frames contract

Frames are **packed I420**: a full-resolution luma plane, then two
quarter-resolution chroma planes, all tightly packed — for width `w`
and height `h`, exactly `w*h*3/2` bytes. Geometry is fixed per runtime
by `Config.video_size` and applies to **both directions**: a written
frame must be exactly one configured-size frame (anything else raises
`ValueError` with the expected byte count), and received frames larger
than the configured size are dropped and counted in
`VideoInfo.rx_oversize`. Timestamps are microseconds.

Neither direction blocks:

- `write_frame(data)` queues one frame for the paced encoder and
  returns `True`. It returns `False` when the frame was *refused* —
  the ring is full, or the transmit direction is momentarily down
  (a renegotiation gap). Keep pacing; delivery resumes by itself.
  The pacer transmits the newest queued frame at `Config.video_fps`;
  writing faster just skips stale frames (`tx_skipped`). Writing
  nothing transmits nothing — that is video's silence.
- `read_frame()` returns the next decoded `VideoFrame`, or `None`
  until something new arrives. A reader that falls behind is skipped
  forward to the newest frame: live video stays live, and a slow
  consumer sees fresh frames, never a growing backlog.

Readiness is asymmetric, and `info()` reports it: `tx_ready` flips when
the transmit machinery binds at call establishment; `rx_ready` flips
only once the first decoded frame has actually arrived — it cannot be a
precondition for starting to send.

Renegotiation (a hold/resume cycle, a re-INVITE) may replace the video
streams mid-call. The epoch contract is audio's: the next operation
raises `VideoRestarted` once — frames buffered across the swap are lost
by design — and the operation after that binds to the new streams
automatically.

## Keyframes

Damaged incoming video heals itself: a decode error triggers a picture
update request (RTCP PLI) to the far end without application help.
`await call.video.request_keyframe()` exists for the other direction of
the same need — a consumer that joins mid-stream and wants a decodable
starting point sooner than the next natural keyframe.

## Cameras

`Config(video_source=...)` selects the capture driver: `"avcapture"`
on macOS (device: `"front"`, `"back"`, or anything else for the system
default camera; the OS asks for camera permission on first use) and
`"v4l2"` on Linux (device: a `/dev/videoN` path). The camera's native
pixel format is converted before the encoder — no format knowledge is
needed. Under a camera source, `write_frame()` has no effect (the
camera owns transmit) while `read_frame()` still taps received video;
the default source, `vidmem`, is the programmatic one where
`write_frame()` is the camera.

## Displaying video is the application's job

This library deliberately ships no display. The reason is structural,
not laziness: macOS confines window creation and event handling to the
process **main thread** (AppKit's rule, inherited by every GUI toolkit
— Tk, Qt, SDL, OpenCV's windows alike), and in an embedded library the
main thread belongs to the application. A display driver inside the
library would either break that rule or demand control of a thread it
does not own. Every Python library in this space lands on the same
answer: frames go to the application, the application renders.

Rendering is small. An asyncio task runs on the main thread, which is
exactly where Tk must live:

```python
import tkinter as tk

root = tk.Tk()
label = tk.Label(root)
label.pack()

async def render(call):
    while True:
        frame = call.video.read_frame()
        if frame is not None:
            img = tk.PhotoImage(
                data=f"P5 {frame.width} {frame.height} 255 ".encode()
                + frame.data[: frame.width * frame.height]
            )
            label.configure(image=img)
        root.update()
        await asyncio.sleep(1 / 30)
```

That is a grayscale preview — the luma plane is a ready-made PGM image.
Color is one numpy conversion away (see
[examples/08_video_call.py](../examples/08_video_call.py), which does
both, self-contained). For headless machines, write frames to a Y4M
file — a two-line format every video tool plays — as the example's
`VIDEO_RECORD` does.
