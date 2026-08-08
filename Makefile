# Build ActivityCapture.exe from source.
# Windows-only (pynput's Windows backend + PyInstaller EXE target).
# Run with `make` (Git Bash / WSL / MSYS2) or just call build.bat directly on plain cmd.exe.

.PHONY: all install build clean

all: build

install:
	python -m pip install --upgrade pip
	python -m pip install -r requirements.txt

build: install
	python -m PyInstaller --clean --noconfirm ActivityCapture.spec

clean:
	rm -rf build dist __pycache__
