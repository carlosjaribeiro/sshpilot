# Terminal output highlighting

Output highlighting is opt-in under **Settings → Terminal → Highlighting**. It
injects foreground SGR sequences without changing visible bytes. Highlighting
is suspended while the remote application owns foreground/background colour,
reverse/conceal, or an alternate screen, and its parser state resets when a
terminal reconnects.

Profiles:

- **Generic**, **Cisco IOS**, **Junos**, and **Huawei VRP** retain their original
  terminal-palette colours.
- **Cisco (SecureCRT-style)** preserves all 51 non-comment rules of the supplied
  SecureCRT V3 `Cisco Words` list as source text and in file order. Built-ins
  compile with a whole-word guard, and each `.*` is bounded to one
  non-whitespace word; later rules win on overlap. As in the source list
  (`D:"Match Case"=00000000`), every rule is case-insensitive. See
  [SecureCRT line engine](#securecrt-line-engine).
- **Custom profiles** are your own copies of any of the above, listed after the
  built-ins. See [Editing profiles and themes](#editing-profiles-and-themes).

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
to this profile and to custom profiles; the setting is greyed out for the
others. Custom themes (copies with your own colours) are listed after these.

`securecrt` is designed for a light background (SecureCRT's own `Cisco Words`;
VanDyke ships a separate `DkBg` list for dark ones). `securecrt-dark` is a
regular, explicitly selectable theme: every `securecrt` colour with its
lightness raised to at least 0.62, hue and saturation kept. It is chosen
automatically only when the saved theme is `securecrt` and the terminal theme's
background is dark (`#RGB` or `#RRGGBB`, luminance below 50%); the saved setting
stays `securecrt`. Choosing `securecrt-dark` yourself, or any other theme
(`dark`, `light`, a custom theme, …), uses exactly that theme: an explicit choice
is never auto-switched.

*Bold Highlighted Text* (`terminal.highlight_bold`, default on) paints every
highlighted run of the *Cisco (SecureCRT-style)* profile bold, as SecureCRT does
with its session-wide bold option (the file only flags the progress rule). Bold
the remote has set is kept, not restarted or cancelled. Other profiles ignore it.

## Editing profiles and themes

Built-in profiles and themes are read-only. To make your own, open
**Settings → Terminal → Highlighting**, select the one to start from and use
the buttons on its row:

- **Edit** (pencil): for a built-in, opens a copy named "… (copy)"; for a custom
  one, edits it. **Save** stores it; when editing a custom entry, **Save as New**
  keeps the original and stores a copy once the *Name* field holds a new name.
  Closing with unsaved changes asks first.
- **Duplicate** (two pages): always opens a copy, also of a custom entry.
- **Delete** (bin): custom entries only, after a confirmation. Terminals using it
  switch to *Generic* (profile) or *SecureCRT* (theme).

Whatever you save becomes the selected profile or theme, and open terminals use
it for new output at once; no restart is needed.

The profile editor lists the rules in order. **Where two rules match the same
text, the later one wins**, so put general rules first and exceptions after
them. Expand a rule to change its regular expression, an optional description,
its colour (a *role* such as *IP address*, which follows the colour theme, or a
fixed *custom colour* picked or typed as `#RRGGBB`), bold, case matching, and to
move it up or down or remove it. The switch on each row turns a rule off without
deleting it. **+** adds a rule; **Import…** appends the rules of a SecureCRT
keyword-list export or a simple rules file (see [Custom rules](#custom-rules)).

The **Live Preview** paints sample `show ip interface brief`, `show running-config`
and syslog lines with the rules being edited (the theme editor: with the
selected profile), on the current terminal colours. Paste a line of your own
into **Test text** to see it on top. A copy of *Cisco (SecureCRT-style)* paints
exactly like the built-in, including its adapted rules that colour parts of
words (`h` in `GigabitEthernet`, `as` in `unassigned`; see the
deviations table below).

Validation happens while you type and before anything is written: a rule must
pass the same safety check as the rules file (see [Custom rules](#custom-rules)),
and a name must be non-empty, at most 64 characters, and not used by another
custom profile (or theme) or a built-in one (`generic`, *Generic*, …), ignoring
case. Descriptions are at most 200 characters. Problems are shown in a banner and on
the rule's row, and **Save** stays disabled until they are fixed. A profile has
at most 500 rules; at most 100 custom profiles and 100 custom themes are kept.

Copies of the built-ins behave like the originals:

- *Cisco (SecureCRT-style)*: the 50 enabled source rules (plus those of a
  `highlight_rules.*` file), in order; the no-op default-colour rule is left out.
- *Generic*, *Cisco IOS*, *Junos*, *Huawei VRP*: their rules, in order and
  case-sensitive (*Match case* on), each described by its token (`ipv4`,
  `vlan`, …). Their range checks come along (an address after `Version` is not
  an IP, `vlan 5000` is out of range) as long as the expression is left
  unchanged. Colours become roles (interfaces *Interface*, VLAN/MPLS
  *Miscellaneous*, errors *Bad*, …), so the colour theme now applies to them.

Any built-in expression, from any profile, runs unchanged. An edited or new
expression must fit the allowlist under [Custom rules](#custom-rules).

The theme editor shows every role with its colour in the base theme. Pick a
colour or type `#RRGGBB` to change it; the undo button returns a role to the
base colour. A custom theme stores only the changed roles, on top of its base.

### Where they are stored

Custom profiles and themes are saved together in `highlight_profiles.json` in
sshPilot's configuration directory:

| System | File |
|---|---|
| Linux | `~/.config/sshpilot/highlight_profiles.json` (`$XDG_CONFIG_HOME/sshpilot/…` if set) |
| Linux, Flatpak | `~/.var/app/io.github.mfat.sshpilot/config/sshpilot/highlight_profiles.json` |
| Windows | `%APPDATA%\sshpilot\highlight_profiles.json` |

The file is versioned JSON and may be copied between machines:

```json
{
  "version": 1,
  "profiles": [
    {"name": "Lab", "rules": [
      {"pattern": "\\bcore\\b", "colour": "important", "bold": true,
       "enabled": true, "match_case": false, "description": "Core links"}
    ]}
  ],
  "themes": [
    {"name": "My dark", "base": "dark", "colours": {"ip": "#00FF00"}}
  ]
}
```

A file that cannot be read (bad JSON, unknown version, duplicate names, unknown
roles, larger than 256 KiB) never stops highlighting: terminals keep the
profile and theme they had (the defaults at start-up) and show a notification,
as they do when a selected custom profile is missing or one of its rules fails
the safety check. Saving from the editor then moves the unreadable file to
`highlight_profiles.json.bad` and writes a new one; a save that would make the
file larger than 256 KiB is refused. Edits made to the file by hand are picked
up on the next highlighting setting change.

Every save re-reads the file and replaces it atomically, so two sshPilot
windows or processes do not undo each other's saved profiles; only two saves
in the very same instant can lose one of them (there is no lock file).

## Custom rules

To change the *Cisco (SecureCRT-style)* profile itself rather than a copy,
put one file named `highlight_rules.*` in sshPilot's configuration directory
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

Custom patterns identical to one of the 51 built-in source expressions use the
same whole-word and bounded-`.*` adaptations as built-ins. Other custom patterns
are run as written and must fit a small allowlist, checked token by token:

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

The profile holds each line of output until it ends, then applies the built-in
rules in file order, each painting all its matches over earlier ones. Output
is therefore the same however the stream is split into chunks. Built-ins use
`re.IGNORECASE`, a whole-word guard, and `\S*` in place of each `.*`, keeping
those wildcards inside one non-whitespace word. Every line of up to 4096 bytes
is matched whole (the two exceptions are under **Line cap**). Visible bytes
never change.

**Whole words (a deliberate adaptation, confidence medium).** SecureCRT's
rules are not matched as raw substrings: without the whole-word guard, `H`, `V`,
`des` or `on` would colour letters inside `dhcp`, `server` or `authorization`,
which SecureCRT does not do. So the 51 built-in rules (and exact, unedited
copies of them in a custom profile, so *Duplicate* equals the original) are
matched on whole words: when a match's first (last) character is a letter,
digit or `_`, the character before (after) it must not be one. Symbol edges
(`#`, `.`, `,`) are exempt, so the prompt `SW#show` and `bits/sec,` still match.
It is a lookaround added when compiling (`whole_word`), not a post-filter, so a
rule can still backtrack to an alternative that fits. In built-in patterns,
each `.*` is also changed to `\S*` before compilation so it cannot cross
whitespace. This is a known adaptation: a pattern like `.*'.*` colours only a
single quoted token, not a multi-word quoted phrase. `_` counts as a word
character, so `ISE_RADIUS` stays one word. Evidence: the list's "gray keywords
work around symbol delimited words" comments and side-by-side screenshots of
SecureCRT; VanDyke does not document how `Regex Line Mode=0` delimits words.
Rules you write or edit yourself (custom profiles, `highlight_rules.*` files)
match exactly as written: `err` still matches inside `error`; add `\b` yourself.
The one exception is a rule whose pattern text is identical to one of the 51
built-in SecureCRT patterns (an unedited duplicate, or a rule you typed or put in
a `highlight_rules.*` file with exactly that text): it is treated as that
built-in and also matches whole words only. Change the pattern even slightly,
for example by adding `(?:)`, to get the as-written behaviour.

**`.*` stays inside one word (confidence medium-high).** In the same built-in
rules (and their exact copies) every `.*` is compiled as `\S*`, so it never
crosses a space: `snmp.*` colours `snmp-server`, not the rest of the line;
`.*down.?` colours `down` or `%LINK-3-UPDOWN:`, not the timestamp before it;
`.*fail.*`, `shut.*`, `password.*`, `.*-more-.*` and the others colour their
own word. This reproduces side-by-side SecureCRT screenshots of
`show running-config` and `show logging`; VanDyke does not document it. `.+`
and `.?` are unchanged.

Rule file format. The reference is the user's original SecureCRT export
(`tests/data/securecrt-cisco-words.original.ini`), which holds two lists of 71
rows: legacy `Keyword List V2` (`"regex",COLORREF,flag`) and `Keyword List V3`
(`"regex",COLORREF,F1,F2`). **V3 is the source of truth**; the importer
(`load_rules`) reads only the V3 section when a file has one. `COLORREF` is
`00BBGGRR`. Decoded fields:

| Field | Meaning | Confidence |
|---|---|---|
| F1 bit 0 (`0x1`) | Rule enabled; `0` = disabled. Only the final default-colour row has it clear (also `0` in V2). | High (same flag as V2) |
| F1 bit 3 (`0x8`) | Bold for the rule's matches. | Medium-low: inferred, VanDyke does not document V3 |
| F2 | Not interpreted. `1` on every row except the progress row (`0xf`, i.e. four bits set), which also has F1 `0x9`. Probably an attribute mask. | Low |

VanDyke documents bold/reverse/colour only as *session-wide* options
(`Highlight Color`, `Highlight Bold`, `Highlight Reverse Video`). Whether the
bold seen on other keywords in SecureCRT comes from `Highlight Bold` cannot be
told from the file: its only per-rule bold row is the progress bar, so every
other keyword's bold, if any, is the global option and not stored per rule.

V2 vs V3 in this file: only the progress rule changes (colour `0000d9d9` ->
`00ff0000` and F1 `0x1` -> `0x9`); the final row is disabled in both. The
profile follows V3. Rows whose regex starts with `[*]` are the 20 section
labels (`[*]Interfaces`, …). They are not loaded: as regexes they only match a
literal `*` followed by the label, which device output never contains. Of the
other 51 rows, 50 are enabled and loaded; the disabled final
`.*|setasregextosetdefaultcolor` is kept as a no-op (see below).

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

  The worst case left is a hostile 4096-byte line: about 0.7 s for
  `%.+-[0-9]-.+:` on `%-1-%-1-…` with no `:`. The `.*` wildcards are bounded to
  one non-whitespace word.
- **Remote styling:** escape sequences keep their position in the line. Text
  the remote coloured (foreground/background, reverse, conceal) still counts
  for matching but is never painted; alternate-screen programs (vim, less,
  top) are passed through untouched, and a held line is shown first.

Deliberate deviations from the file:

| Source rule | Behaviour | Reason |
|---|---|---|
| Final `.*\|setasregextosetdefaultcolor` (`00808080`) | Kept in the built-in profile as the last rule but emits no SGR; the importer skips it (F1 = 0, disabled) | The terminal's default colour is the baseline. Applied literally as the last, highest-priority rule, it would grey over every earlier rule; the file names it the default-colour rule. |
| — | No word guards on short words | The earlier "no alphanumeric neighbour" rule for `no`, `on`, `des` … contradicts the file, so it was dropped. As in the file, `no` matches in `snooping`, `on` in `authentication`, and the single letters `H`, `V`, `Q` colour every `h`, `v`, `q`. |
| V3 progress rule `\[#+(?:\x20\|$)\|^\](?:\x20\|$)` (F1 `0x9`) | Bold (SGR 1, closed with `22;39`) only for its span, then foreground/bold/faint restored | Prevents bold leaking into the rest of the line. |
