"""PlatformIO pre-build patch for M5Unified audio-task stack bugs on the Cardputer ADV.

M5Unified sizes its audio FreeRTOS task stacks as `<base> + dma_buf_len * <sample size>`,
but each task then `alloca`s a `dma_buf_len * <sample size>` work buffer on that same
stack — so the real headroom is only the fixed base, regardless of buffer size:

  - mic_task (Mic_Class.cpp):     base 2048, then IDF i2s_channel_read underneath needs
    more than that on the ADV (ESP32-S3 + ES8311) and the task overflows the instant
    recording starts: "Stack canary watchpoint triggered (mic_task)".
  - spk_task (Speaker_Class.cpp): base 1280 — even less headroom for the same IDF
    i2s_channel_write path, plus float math in the mixer.

There is no config knob for these stack sizes, so we bump the hardcoded bases to 8192
in the vendored library source before compiling. Idempotent; runs on every build.
"""
Import("env")  # noqa: F821
import glob
import os

PATCHES = [
    ("Mic_Class.cpp",
     "2048 + (_cfg.dma_buf_len * sizeof(uint16_t))",
     "8192 + (_cfg.dma_buf_len * sizeof(uint16_t))"),
    ("Speaker_Class.cpp",
     "1280 + (_cfg.dma_buf_len * sizeof(uint32_t))",
     "8192 + (_cfg.dma_buf_len * sizeof(uint32_t))"),
]

libdeps = env.subst("$PROJECT_LIBDEPS_DIR")  # noqa: F821
pioenv = env.subst("$PIOENV")  # noqa: F821

for filename, needle, replacement in PATCHES:
    pattern = os.path.join(libdeps, pioenv, "**", filename)
    patched = False
    for path in glob.glob(pattern, recursive=True):
        with open(path, "r") as fh:
            src = fh.read()
        if needle in src:
            with open(path, "w") as fh:
                fh.write(src.replace(needle, replacement))
            print("[patch_m5unified] task stack -> 8192 :", path)
            patched = True
        elif replacement not in src:
            # The library is installed but upstream changed this line: without the patch the
            # device crashes (stack overflow) on the first recording, so stop the build here.
            print("[patch_m5unified] ERROR: %s no longer contains the expected stack size;"
                  " M5Unified changed upstream — update PATCHES." % path)
            env.Exit(1)  # noqa: F821
    if not patched:
        print("[patch_m5unified] %s: no change (already patched, or not installed yet)" % filename)
