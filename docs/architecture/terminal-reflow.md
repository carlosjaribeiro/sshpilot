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

| Area | Raw stream (POSIX local shell, POSIX daemon) | ConPTY-rendered stream (Windows) |
|---|---|---|
| Scrollback | reflowed | reflowed |
| Primary screen | reflowed together with the scrollback | **not** reflowed (old behaviour) |
| Alternate screen (vim, less, htop) | never | never |
| Main screen saved behind the alternate screen | reflowed when the alternate screen exits | cut at the new width (old behaviour); the next row is unlinked |

"Raw stream" versus "ConPTY" is the existing `pull_history_on_grow` switch
(`prepare_pty_less_emulation`, `spawn_async`). It already means "the model
owns the screen rows" versus "conhost repaints its viewport after a resize".

On the ConPTY path a resize that changes both size and width first pushes
the rows it scrolls off into the scrollback, then rewraps the scrollback. The
pushed rows therefore end up at the new width too. Screen rows that narrowing
cuts lose their tail, as before. The row after a cut row no longer continues
it.

On the ConPTY path, `draw` starting at column 0 also unlinks the row. ConPTY
moves to the start of a row only to repaint it, with whatever its own buffer
holds there, so the row no longer follows the one above. On raw streams,
overwriting in place keeps the link (readline redraws).

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
  repainted rows arrive as cursor moves rather than autowraps. They carry no
  `continued` flag (a repaint at column 0 clears it), so they are not joined
  after they scroll into history. Only output that ConPTY passed through as a
  soft wrap is.
- DECSC saved cursors (`ESC 7`) are not remapped.
- Under ConPTY, narrowing still cuts the screen rows and the main screen
  saved behind the alternate screen, which loses the cut text. ConPTY
  repaints its own buffer over them; nothing is spliced.
- On a raw stream, the main screen saved behind the alternate screen is
  reflowed only when the alternate screen is left. Until then, rows kept
  past the new width stay hidden.
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

That is plan step 2.6 and still open. The ConPTY rule above applies to it
too.
