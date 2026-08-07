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
