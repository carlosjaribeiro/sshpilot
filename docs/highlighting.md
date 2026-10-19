# Terminal output highlighting

Output highlighting is opt-in under **Settings → Terminal → Highlighting**. It
injects foreground SGR sequences without changing visible bytes. Highlighting
is suspended while the remote application owns foreground/background colour,
reverse/conceal, or an alternate screen, and its parser state resets when a
terminal reconnects.

Profiles:

- **Generic**, **Cisco IOS**, **Junos**, and **Huawei VRP** retain their original
  terminal-palette colours.
- **Cisco (SecureCRT-style)** runs all 51 non-comment rules of the supplied
  SecureCRT V3 `Cisco Words` list **verbatim**, in file order, on each complete
  line; later rules win on overlap. As in the source list
  (`D:"Match Case"=00000000`), every rule is case-insensitive. See
  [SecureCRT line engine](#securecrt-line-engine).

## Colour themes

`securecrt` is the default and emits the source COLORREF values as truecolour
`38;2;R;G;B` after converting SecureCRT's `00BBGGRR` order to RGB: `00d26900`
is RGB (0,105,210) blue and `0000ae00` is (0,174,0) green; a test checks every
colour in the file. (Gold interfaces and cyan addresses are the *Cisco IOS*
profile's palette colours 33 and 36, not a swapped SecureCRT colour.) `dark` is a
brighter black-background variant, `light` is a darker white-background
variant (its accents keep at least 4.5:1 contrast on white), and
`high-contrast` maximises separation. `terminal-palette` emits ANSI palette
indices 31–36 and 90–97 (grey roles use 90, never 37, which is invisible on a
white background) so the terminal theme owns the actual colours. Themes apply
only to this profile; the setting is greyed out for the others.

## Custom rules

Put one file named `highlight_rules.*` in sshPilot's configuration directory
(`~/.config/sshpilot` on standard Linux installations, `%APPDATA%\sshpilot` on
Windows). The lexicographically first matching file is appended to the
`cisco-securecrt` profile, so its later rules override built-ins. Restart
sshPilot after changing the file.

Simple format:

```text
"\bmaintenance\b",important
"\bsite-[0-9]{1,3}\b",#ff8800,bold
```

The second field is a documented role or `#RRGGBB`; the optional third field is
`bold`. A SecureCRT keyword-list export can be used directly: `00BBGGRR`
COLORREF values are converted on load and `[*]` section rows are skipped.
Rules are case-insensitive unless the file contains `D:"Match Case"=00000001`.

Custom patterns (other than the 51 built-in source expressions, which are
recognised and run verbatim) must fit a small allowlist, checked token by
token:

- literals, escaped characters, `\xHH`, character classes, `.`, `\d \w \s`
  (and their negations), `\b \B`, groups `(…)`/`(?:…)` and `|`;
- `^` and `$` (rules always run on one line, so they mean line start/end);
- quantifiers `?`, `*`, `+`, `{n}`, `{n,m}` only directly after a single
  character or class, never after a group, at most two per rule, and at most
  32 repetitions: `*` and `+` are capped at 32;
- no lookarounds, backreferences, inline flags or other `(?` syntax, and at
  most 512 ASCII characters.

Anything else skips that rule with a warning, so `((a+))+$` or `a{1,100000}`
cannot stall the terminal. The file is ignored with a warning if it is larger
than 256 KiB or is not UTF-8, unparsable rows are skipped, and only the first
500 rules are used. A bad file never stops highlighting: the built-in rules
still apply.

## SecureCRT line engine

The profile holds each line of output until it ends, then applies every rule
to the line, exactly as written in the file (only `re.IGNORECASE`), in file
order, each rule painting all its matches over the earlier ones. Output is
therefore the same however the stream is split into chunks. Every line of up
to 4096 bytes is matched whole (the two exceptions are under **Line cap**), so
`.*thernet…`, `.*down.?`, `.*-more-.*`, `snmp.*`, the `^` prompt rules and `$`
behave as in SecureCRT. Visible bytes never change.

Rule file format: each row is `"regex",COLORREF` (`00BBGGRR`); there is no
enable/disable field. One row also carries a trailing `# V3: …` note, which is
ignored. Rows whose regex starts with `[*]` are the list's 17 section labels
(`[*]Interfaces`, …). They are not loaded: as regexes they only match a
literal `*` followed by the label, which device output never contains. The
other 51 rows are all loaded.

- **Line end:** CR, LF or backspace (BS). Backspace counts because Cisco erases
  `--More--` with backspaces and prints the next line in its place.
- **Unfinished line** (prompt, typed echo, `--More--`, `Password:`): shown
  after 50 ms without output, or at once when the line so far looks like a
  prompt (`\w[^>]*#` or `\w[^#]*>` up to its last byte). What follows on that
  line is coloured with the shown part as context; the shown part itself is
  never repainted, so a line-wide rule triggered later (`SW#copy x.bin`) only
  colours the new text. An unfinished UTF-8 character is held back.
- **Line cap:** typical lines cost about 0.2 ms, but several source rules are
  superlinear, so two fallbacks bound hostile input:
  - a line longer than 4096 bytes is cut at 4096 (at a UTF-8 boundary) and
    the pieces are matched as separate lines, so a rule cannot span the cut;
  - a line containing a run of more than 256 digits is matched in 256-byte
    pieces, because `\d*\d*\.\d*\(` is cubic in the run length (21 s for 4096
    digits whole, about 7 ms per piece).

  The worst case left is a hostile 4096-byte line: about 0.25 s for the
  quadratic `.*X` rules, and about 0.7 s for `%.+-[0-9]-.+:` on `%-1-%-1-…`
  with no `:`.
- **Remote styling:** escape sequences keep their position in the line. Text
  the remote coloured (foreground/background, reverse, conceal) still counts
  for matching but is never painted; alternate-screen programs (vim, less,
  top) are passed through untouched, and a held line is shown first.

Deliberate deviations from the file:

| Source rule | Behaviour | Reason |
|---|---|---|
| Final `.*\|setasregextosetdefaultcolor` (`00808080`) | Loaded as the last rule but emits no SGR: the terminal's default colour is the baseline | The file has no enable flag to turn it off. Applied literally as the last, highest-priority rule, it would grey over every earlier rule; the file names it the default-colour rule. |
| — | No word guards on short words | The earlier "no alphanumeric neighbour" rule for `no`, `on`, `des` … contradicts the file, so it was dropped. As in the file, `no` matches in `snooping`, `on` in `authentication`, and the single letters `H`, `V`, `Q` colour every `h`, `v`, `q`. |
| V3 progress rule `\[#+(?:\x20\|$)\|^\](?:\x20\|$)` | Bold only for its span, then foreground/bold/faint restored | Prevents bold leaking into the rest of the line. |
