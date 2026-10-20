# Reflow on resize (built-in backend, pyte model)

When the window gets wider, VTE and xterm-style terminals re-join lines that
were only wrapped because the terminal was narrow ("soft wraps"). The built-in
backend did not. On Windows, where it is the only backend, text printed before
a resize kept its old line breaks. This note says what the pyte model now
reflows, what it leaves alone, and why.

## What is tracked

Each row has a `continued` flag: "this row continues the row above".

- It is set only when pyte **autowraps**, that is when `draw()` reaches the
  last column with DECAWM on and moves to the next row. `EmulatorScreen.draw`
  marks the span, and `linefeed()` flags the new row only inside it.
- An explicit CR/LF never sets it, even after a line that fills the row
  exactly.
- Erasing clears it at the erased junction (EL 0/1/2, ED 0/1/2), so a screen
  that is cleared and redrawn does not join stale rows.
- It moves with the row: scrolling, the scrollback (`_Row`, a `dict` with a
  `continued` slot), shrinking into history and pulling back on grow.

## What is reflowed

Reflow runs only when the **column count** changes (`EmulatorScreen.reflow`,
default on).

| Area | Raw stream (POSIX local shell, POSIX daemon) | ConPTY-rendered stream (Windows) |
|---|---|---|
| Scrollback | reflowed | reflowed |
| Primary screen | reflowed together with the scrollback | **not** reflowed (old behaviour) |
| Alternate screen (vim, less, htop) | never | never |
| Main screen saved behind the alternate screen | not reflowed (old behaviour) | not reflowed |

"Raw stream" versus "ConPTY" is the existing `pull_history_on_grow` switch
(`prepare_pty_less_emulation`, `spawn_async`). It already means "the model
owns the screen rows" versus "conhost repaints its viewport after a resize".

Reflow joins each logical line (a row plus the rows that continue it), drops
trailing unwritten cells of soft-wrapped rows and trailing default blanks of
the last row, and wraps again at the new width:

- cells are copied whole, so colour and attributes are kept (this includes
  colours added by an output-highlighting filter in front of the model, which
  arrive as ordinary SGR);
- a wide character never straddles two rows. If it does not fit, the row ends
  one cell short and the character starts the next row;
- combining characters are already merged into their base cell by pyte;
- a short row that is not wrapped is reused as is, so the cost is
  proportional to the wrapped text, not to the scrollback size;
- the scrollback keeps its `maxlen`. A narrower width drops the oldest rows
  first, as new output would.

On a screen reflow the cursor stays on the same character (or the same
blank after the text). Blank rows below the cursor are dropped. Then the
screen is bottom-anchored like xterm and VTE: rows freed by widening pull
history down, and rows added by narrowing push the top into the scrollback.
The cursor row always stays on screen.

## Risks and why ConPTY sessions keep their screen

`windows-port.md` measured that the bundled ConPTY does not reflow its own
buffer. A shell under it redraws with absolute cursor positions computed
against that unreflowed buffer, and conhost repaints its viewport from the
top after every resize. If the model reflowed the visible rows, the repaint
would land on different rows (corruption). It would also push rows into the
scrollback that the repaint then draws again (duplicates). So on Windows the
model only rewraps the scrollback. Neither ConPTY nor the remote can address
those rows, and they are what the user scrolls back to read. The visible
screen keeps ConPTY's line breaks until it scrolls away.

On a raw stream the remote sees only SIGWINCH and the new size, exactly as
under VTE, which reflows the same way. Line editors (bash readline, zsh ZLE)
redraw a multi-row command line from their own idea of the old layout. That
can leave a stray row while editing a wrapped command, the same as in VTE.

Full-screen applications are unaffected because the alternate screen is never
reflowed (Neovim #38812 makes the same rule).

## Default

On by default. The scrollback rewrap cannot desynchronise ConPTY or the
remote, because neither addresses those rows. The screen rewrap only runs
where VTE, the default backend on that platform, already does the same thing.
`EmulatorScreen.reflow = False` turns it off; there is no preference yet.

## Known limits

- **Not verified in a real Windows window.** Unit tests only. Under ConPTY,
  repainted rows arrive as cursor moves rather than autowraps. Such rows carry
  no `continued` flag, so they are not joined after they scroll into history.
  Only output that ConPTY passed through as a soft wrap is.
- DECSC saved cursors (`ESC 7`) are not remapped. Neither is the main screen
  saved behind the alternate screen.
- IL/DL (insert/delete line) move rows together with their flags. A blank row
  inserted inside a wrapped line can join it to the row above.
- Cost: about 0.1 s per width change with a full 10 000-row scrollback in
  which a fifth of the lines are 200 characters long. Typical scrollbacks are
  far below that.

## libvterm model

Not done. `reflow_rows` works on any rows of cells that have `.data` and
`_replace` and that carry the `continued` attribute. The libvterm model,
however, keeps its scrollback as raw C cell bytes and its screen inside
libvterm. It would need:

- the patched mirror's `sb_pushline4` to receive the continuation flag;
- libvterm's own `vterm_screen_enable_reflow` for the screen;
- the push/pop accounting emacs-libvterm does (`windows-decisions.md`
  finding 4).

That is plan step 2.6 and still open. The ConPTY rule above applies to it
too.
