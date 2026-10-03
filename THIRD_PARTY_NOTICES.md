# Third-party notices

## comma.ai panda and opendbc

The firmware PandaCapture builds is comma.ai's panda firmware (`firmware/panda` and, for the
STM32F4 Black Panda, `firmware/panda-f4`: git submodules) with PandaCapture's patches
(`firmware/patches`), built against comma.ai's opendbc (`firmware/opendbc`, `firmware/opendbc-f4`). The flashing and USB protocol code in `pandacapture/` (`protocol.py`,
`panda.py`, `dfu.py`, `flasher.py`) follows comma.ai's panda Python library. Both are under the
MIT licence:

```
Copyright (c) 2016, Comma.ai, Inc. (panda)
Copyright (c) 2020, Comma.ai, Inc. (opendbc)

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

PandaCapture isn't made or endorsed by comma.ai. "panda", "Red Panda" and "Black Panda" are
comma.ai's product names.

## Bundled in release programs

- python-libusb1 (LGPL-2.1+) and libusb (LGPL-2.1+), via libusb1 and libusb-package
- Python and PyInstaller's bootloader (PSF licence; GPL with the bootloader exception)
