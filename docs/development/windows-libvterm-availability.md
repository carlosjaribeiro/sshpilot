# Why "libvterm (unavailable)" on a fresh Windows setup

Preferences → Terminal → Backend shows "libvterm (unavailable)" when
`load_libvterm()` (`vterm_terminal_model.py`) finds no usable DLL. The
built-in backend then falls back to pyte (`create_terminal_model`), even
though libvterm is the Windows default (`default_builtin_model`).

## Cause (from reading the code; not reproduced on Windows here)

`scripts/setup-windows.ps1` fetches GTK and ConPTY as prebuilt binaries, but
it does not download libvterm. It *builds* it, and only when a compiler is
already on `PATH`:

```powershell
if (Get-Command cl.exe ...) { build.py --compiler msvc }
elseif (Get-Command gcc.exe ...) { build.py --compiler gcc }
else { Write-Warning "No C compiler found: skipping libvterm ..." }
```

On a default install neither is on `PATH`:

- Visual Studio / Build Tools put `cl.exe` on `PATH` only in a
  "Developer PowerShell / Command Prompt" (`vcvars64.bat`), not in a normal
  PowerShell, even when installed.
- MSYS2's `gcc.exe` lives in `C:\msys64\ucrt64\bin`, which is not on
  `PATH` by default.

The script then prints only a warning and still ends with "Done", so the
missing `vendor\libvterm\<arch>\libvterm-0.dll` goes unnoticed. The model
looks for that file first in a source checkout (`_candidate_paths`). Without
it, and without `SSHPILOT_LIBVTERM`, no candidate loads.

The build also needs `git` on `PATH`, because `build.py` clones
`github.com/neovim/libvterm` at a pinned commit, plus network access. If a
distro `libvterm-0.dll` happens to be on the loader path, it loads, but
it lacks the CSI bound that the patched build adds.

## What makes it available

Any one of these:

1. Run `python packaging\windows\libvterm\build.py --compiler msvc` from a
   *Developer PowerShell for VS* (C++ build tools installed). Then restart
   the app.
2. With MSYS2:
   `python packaging\windows\libvterm\build.py --compiler gcc --cc C:\msys64\ucrt64\bin\gcc.exe`.
3. Point `SSHPILOT_LIBVTERM` at an existing patched `libvterm-0.dll`.

Possible fixes in the script (not done here):

- find Visual Studio with `vswhere.exe` and run the build inside
  `vcvars64.bat`;
- or fetch a CI-built patched DLL the way `fetch-gtk.py` and
  `fetch-conpty.py` fetch theirs;
- and fail loudly, or print the exact fix, instead of only warning.

The frozen installer is not affected if its build ships the DLL next to the
executable (`_MEIPASS` / executable directory).
