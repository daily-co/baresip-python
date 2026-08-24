# Building with extra modules, including GPL codecs (H.264)

The published wheels contain exactly the default module set, and never any GPL code. But
the build system is not in the business of restricting what you build for yourself: any
baresip module can be compiled in from source — your machine, your build, your license
terms. This page covers the mechanics, and the licensing nuance for the module most people
want this for: H.264 via ffmpeg's `avcodec`.

## The mechanics

Two knobs drive an extra-modules build from a source checkout:

- `BARESIP_EXTRA_MODULES` (or `--extra-modules`) tells `scripts/build_native.py` which
  additional modules to compile into the static libraries. The module-completeness guard
  applies to them too: a module that silently fails to build — usually a missing system
  dependency — fails the whole build instead of quietly disappearing.
- `BP_EXTRA_LIBS` hands the extension link step whatever external libraries those modules
  need, since their code sits in `libbaresip.a` and its symbols must resolve when the
  extension links.

On Linux, with the ffmpeg development headers installed
(`apt install libavcodec-dev libavutil-dev`):

```
BARESIP_EXTRA_MODULES=avcodec uv run python scripts/build_native.py
BP_EXTRA_LIBS="-lavcodec -lavutil" make ext
make test
uv run python scripts/check_linkage.py --allow-gpl
```

The last command shows what the built extension actually links — with `avcodec` in the
build you will see the ffmpeg libraries listed, which is the point.

On macOS the same steps apply with `brew install ffmpeg`, plus the brew lib directory on
the link line, e.g. `BP_EXTRA_LIBS="-L$(brew --prefix ffmpeg)/lib -lavcodec -lavutil"`.

The supported non-GPL opt-ins (`sndfile` for compressed-audio file drivers,
`in_band_dtmf` for in-band tone detection) use exactly the same mechanics:
`BARESIP_EXTRA_MODULES="sndfile;in_band_dtmf"` and, for sndfile, `BP_EXTRA_LIBS=-lsndfile`.

## When is this GPL, actually?

The nuance is worth knowing, because it decides what you may do with the result:

- **Decoding H.264** uses ffmpeg's own native decoder, which is LGPL. An ffmpeg built
  without GPL components keeps the whole combination LGPL-clean.
- **Encoding H.264** in practice means [x264](https://www.videolan.org/developers/x264.html),
  which is GPL — and an ffmpeg built with `--enable-gpl`/`--enable-libx264` (which is what
  distro and Homebrew ffmpeg packages ship) makes the *combined work* GPL, decode or not.

So: build against a distro ffmpeg and the binary you produced is, in practice, a GPL
combined work. That is entirely fine to build, run, and test on your own systems. Do not
redistribute it unless you are prepared to meet the GPL's obligations for the whole
combination.

## Support level

Honestly stated: the `avcodec` build is **compile-verified** — CI builds it against ffmpeg
weekly and runs the unit suite, and nothing it produces is ever distributed — while its
**runtime behavior is community-supported**. If you rely on it, expect to get your hands
dirty; issues and patches are welcome.
