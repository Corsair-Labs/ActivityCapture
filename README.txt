This is the original, human-readable source code that ActivityCapture.exe was built from.

ActivityCapture.exe is this exact Python script, packaged with PyInstaller so end users
don't need Python installed. Nothing in the exe does anything beyond what you see in
activity_capture.py:

  - Tracks mouse clicks/movement and key presses using the pynput library
  - Writes the live stats to a local JSON file on the user's own PC
  - Runs small local-only HTTP servers (ports 7070 and, optionally, 8080) used only by
    the widget itself for reset/pause and live preview
  - Makes no outbound network requests and sends nothing off the user's PC

Anyone with Python installed can run this file directly (`python activity_capture.py`)
and confirm it behaves identically to the .exe.

This folder also includes everything needed to rebuild ActivityCapture.exe from scratch
and verify it matches:

  - requirements.txt        pinned dependency versions
  - ActivityCapture.spec    PyInstaller build spec (exact build config used)
  - Makefile                 build script for `make` users (Git Bash / WSL)
  - .github/workflows/       GitHub Actions pipeline that builds the exe automatically
                              when a new GitHub Release is created

To build it yourself: run `make build` from this folder (requires Git Bash or WSL).
The result lands in `dist/ActivityCapture.exe`.

Download pre-built releases from:
  https://github.com/Corsair-Labs/ActivityCapture/releases
