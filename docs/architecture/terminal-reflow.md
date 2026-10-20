# Reflow on resize (built-in backend, pyte model)

When the window gets wider, VTE and xterm-style terminals re-join lines that
were only wrapped because the terminal was narrow ("soft wraps"). The built-in
backend did not. On Windows, where it is the only backend, text printed before
a resize kept its old line breaks, and narrowing cut the visible rows. This
note says what the pyte model now reflows, what it leaves alone, and why.

## What is tracked

Each row has a `continued` flag: "this row continues the row above".

- It is set only when pyte **autowraps**, that is when `draw()` reaches the
  last column with DECAWM on and moves to the next row. `EmulatorScreen.draw`
  marks the span, and `linefeed()` flags the new row only inside it.
- An explicit CR/LF never sets it, even after a line that fills the row
  exactly.
- Editing breaks the link at the edited edge. If an erase or shift (EL, ED,
  ECH, DCH, ICH) changes the first column of a row, that row stops
  continuing the row above. If it changes the last column, the next row
  stops continuing it. Writing over cells in place keeps the link (a
  readline redraw), and so does `draw` writing into the row it just
  wrapped to.
- When rows move relative to each other, the moved rows lose their link:
  IL/DL, and IND/RI scrolling inside a DECSTBM region. That covers the moved
  rows and the row below the region. A full-screen scroll keeps the link,
  because the top row goes to the scrollback.
- It moves with the row: scrolling, the scrollback (`_Row`, a `dict` with a
  `continued` slot), shrinking into history and pulling back on grow.

## What is reflowed

Reflow runs only when the **column count** changes (`EmulatorScreen.reflow`,
default on).

| Area | Raw stream (POSIX local shell, POSIX daemon) | Bundled conpty.dll (Windows default) | Inbox console (kernel32, pywinpty) |
|---|---|---|---|
| Scrollback | reflowed | reflowed | reflowed |
| Primary screen | reflowed with the scrollback, bottom-anchored | reflowed with the scrollback, top-anchored | **not** reflowed by the model; conhost repaints it reflowed |
| Alternate screen (vim, less, htop) | never | never | never |
| Main screen saved behind the alternate screen | rebuilt when the alternate screen exits | rebuilt when the alternate screen exits | cut at the new width; the next row is unlinked |

The model has two switches. `pull_history_on_grow` is off for any stream
that went through ConPTY. `passthrough` (only meaningful with the first one
off) says the console relays the stream and sends nothing after a resize, so
the model still owns its screen. `PyteTerminalBackend._follow_conpty` sets
both for daemon sessions and local shells on Windows; `passthrough` is on when
`conpty_passes_through()` finds the bundled conpty.dll (the same lookup the
local daemon makes) and `terminal.reflow_visible_screen` is not false.

### Why the bundled conpty.dll can be reflowed

Measured on Windows 11 with Microsoft.Windows.Console.ConPTY 1.25 (the DLL
the Windows build bundles), driving `CtypesPty` from WSL interop and feeding
its output into this model. Clients: a fake Cisco CLI (raw input, writes VT,
echoes itself, like `ssh.exe` relaying IOS), `wsl.exe` running bash, and
`cmd.exe`.

- A VT client's bytes come through unchanged. A 90-character line arrives
  as 90 characters, not pre-wrapped.
- A resize (narrower, wider, taller, shorter, both, on the alternate
  screen) produces **no output at all** for the fake CLI. Nothing is
  repainted.
- When the client asks the console for its cursor (wsl.exe and cmd.exe do
  around a resize or a prompt), ConPTY sends `ESC[6n` and, after the reply,
  a CUP to exactly the reported position. The model answers from its own,
  reflowed cursor, so that CUP moves nothing.
- `cmd.exe` writes through console APIs, so conhost positions its output
  with absolute CUPs into its own buffer. conhost reflows that buffer on a
  width change but does not pull history down when rows are freed. That is
  why the model's passthrough reflow is top-anchored: with bottom-anchoring,
  `echo y` after a widen landed one prompt too high. Top-anchored, the
  recorded cmd.exe stream lands on the right rows in both directions
  (`test_passthrough_widening_keeps_history_where_conhost_keeps_it`).

The inbox console behaves differently: it reflows its own buffer and
repaints the viewport from `ESC[H` after every resize, and keeps positioning
later output absolutely against that viewport. There the model keeps
following conhost (the old rule below), and the visible text still comes
out reflowed, because conhost's repaint is.

On the inbox-console path a resize that changes both size and width first pushes
the rows it scrolls off into the scrollback, then rewraps the scrollback. The
pushed rows therefore end up at the new width too. Screen rows that narrowing
cuts lose their tail, as before. The row after a cut row no longer continues
it.

On the inbox-console path, text that does not start exactly where the previous text
ended is treated as a repaint. The row it lands on loses both links, to the
row above and to the row below. ConPTY moves the cursor and draws (CUP, CUF,
CR plus text) only to repaint a row with whatever its own buffer holds there.
A wrap that ConPTY passes through is continuous text, so its link survives,
even when the output is split across feeds or broken by SGR. Cursor moves
that draw nothing leave the rows unchanged, so their links stay valid. On raw
streams and under conpty.dll, overwriting in place keeps the link (readline
redraws).

Leaving the alternate screen on a raw stream or under conpty.dll rebuilds
the main screen whenever the size changed meanwhile. That reflows it and
anchors it as above,
so rows that no longer fit go to the scrollback. Before this change, a
height shrink on the alternate screen dropped the main screen rows below the
new height (`tests/test_pyte_terminal_model.py` had marked those cases as
expected failures for pyte).

Reflow joins each logical line (a row plus the rows that continue it), drops
trailing unwritten cells of soft-wrapped rows and trailing default blanks of
the last row, and wraps again at the new width:

- cells are copied whole, so colour and attributes are kept (this includes
  colours added by an output-highlighting filter in front of the model, which
  arrive as ordinary SGR);
- a wide character never straddles two rows. If it does not fit, the row ends
  one cell short and the character starts the next row;
- combining characters are already merged into their base cell by pyte;
- a short row that is not wrapped is reused as is;
- the scrollback keeps its `maxlen`. A narrower width drops the oldest rows
  first, as new output would.

On a screen reflow the cursor stays on the same character (or the same
blank after the text). Blank rows below the cursor are dropped. Then the
screen is bottom-anchored like xterm and VTE: rows freed by widening pull
history down, and rows added by narrowing push the top into the scrollback.
The cursor row always stays on screen.

## Risks

Under the inbox console, conhost repaints its viewport from the top after
every resize and positions later output against it. If the model reflowed
the visible rows there, the repaint would land on different rows
(corruption) and redraw rows the model had pushed into the scrollback
(duplicates). So on that path the model only rewraps the scrollback.

Under the bundled conpty.dll there is no repaint to collide with. The
remaining risks are the same as on a raw stream, plus one:

- a console-API client (cmd.exe, PowerShell) relies on conhost's reflow
  matching the model's. Both keep cells, drop trailing blanks of the last
  row and keep the screen top; the recorded cmd.exe case matches. A wide
  character at a row edge, or a line straddling the top of the screen, may
  still differ by a row.
- an older or newer conpty.dll that repaints after a resize would bring the
  inbox-console problem back. `terminal.reflow_visible_screen = false`
  restores the old behaviour.

On a raw stream the remote sees only SIGWINCH and the new size, exactly as
under VTE, which reflows the same way. Line editors (bash readline, zsh ZLE)
redraw a multi-row command line from their own idea of the old layout. That
can leave a stray row while editing a wrapped command, the same as in VTE.

Full-screen applications are unaffected because the alternate screen is never
reflowed (Neovim #38812 makes the same rule).

## Default

On by default. The scrollback rewrap cannot desynchronise ConPTY or the
remote, because neither addresses those rows. The screen rewrap runs on raw
streams, where VTE does the same thing, and under the bundled conpty.dll,
which was measured not to repaint. `EmulatorScreen.reflow = False` turns all
of it off. The setting `terminal.reflow_visible_screen` (config only, no
Preferences row; default true) set to false keeps the visible screen under
ConPTY as before.

## Known limits

- **Not verified in a real Windows window.** The ConPTY byte streams are
  real (recorded through conpty.dll 1.25 and kernel32 on Windows 11) and the
  model was fed them live, but the GTK view and a real `ssh.exe` to a Cisco
  device were not driven. Under the inbox console, repainted rows arrive as
  cursor moves rather than autowraps. They carry no `continued` flag (a
  repaint clears it), so they are not joined after they scroll into history.
- A line editor that redraws a multi-row command line after a resize (bash
  readline) moves up by its own idea of the row count, so narrowing can
  leave one stale copy of the command line above the redraw. VTE and xterm
  do the same. Cisco IOS scrolls a long command line sideways instead of
  wrapping it, so it is not affected.
- DECSC saved cursors (`ESC 7`) are not remapped.
- Under the inbox console, narrowing still cuts the screen rows and the main
  screen saved behind the alternate screen, which loses the cut text.
  conhost repaints its own buffer over them; nothing is spliced.
- Under the inbox console, a wrapped line is not rejoined in two cases. The first is
  when ConPTY writes part of a continuation row by moving the cursor, for
  example a CUF over a run of spaces. The second is any width or height
  change in between, because the end position of the last text is then
  stale. Either way the line just stays split.
- With the inbox console or with reflow off, a height shrink on the alternate screen
  still drops the saved main screen rows below the new height, as upstream
  does (`windows-port.md` §3a).
- Performance has not been measured in the GTK view. Each width change walks
  every row of the scrollback in Python.
- pyte's `insert_characters` keeps one pushed-off cell just past the last
  column. The model drops it, so reflow cannot bring it back.

## libvterm model

Not done. `reflow_rows` works on any rows of cells that have `.data` and
`_replace` and that carry the `continued` attribute. The libvterm model,
however, keeps its scrollback as raw C cell bytes and its screen inside
libvterm. It would need:

- the patched mirror's `sb_pushline4` to receive the continuation flag;
- libvterm's own `vterm_screen_enable_reflow` for the screen;
- the push/pop accounting emacs-libvterm does (`windows-decisions.md`
  finding 4).

That is plan step 2.6 and still open. The inbox-console rule above applies
to it too. Until then it ignores `passthrough`, so under conpty.dll it keeps
its visible screen as before.
