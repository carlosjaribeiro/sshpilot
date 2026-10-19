# Terminal output highlighting

Output highlighting is opt-in under **Settings → Terminal → Highlighting**. It
injects foreground SGR sequences without changing visible bytes. Highlighting
is suspended while the remote application owns foreground/background colour,
reverse/conceal, or an alternate screen, and its parser state resets when a
terminal reconnects.

Profiles:

- **Generic**, **Cisco IOS**, **Junos**, and **Huawei VRP** retain their original
  terminal-palette colours.
- **Cisco (SecureCRT-style)** ports all 51 non-comment rules from the supplied
  SecureCRT V3 `Cisco Words` list. Later rules win on overlap. The final `.*`
  rule is represented as the unstyled terminal-default baseline. As in the
  source list (`D:"Match Case"=00000000`), every rule is case-insensitive:
  `Gi0/10`, `YES`, `--More--` and `EC:8E:77:17:60:C9` all match.

## Colour themes

`securecrt` is the default and emits the source COLORREF values as truecolour
`38;2;R;G;B` after converting SecureCRT's `00BBGGRR` order to RGB. `dark` is a
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
recognised and replaced by their vetted adaptations) must fit a small
allowlist, checked token by token:

- literals, escaped characters, `\xHH`, character classes, `.`, `\d \w \s`
  (and their negations), `\b \B`, groups `(…)`/`(?:…)` and `|`;
- `^` and `$` mean start and end of the line (CR or LF), not of the buffer;
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

## SecureCRT compatibility adaptations

No source rule is omitted. The built-in table keeps each original expression
beside its adapted expression, and a fixture test checks all 51 in original
priority order. The changes below are deliberate:

| Source rule(s) | Streaming-safe behaviour | Reason |
|---|---|---|
| `.*'.*` | At most 256 non-newline bytes on either side of `'` | Prevent unbounded line scans and never cross a line. |
| `on-fail.*` | Match whole-word `on-fail`, then colour arriving text to newline | Prompts/output are emitted immediately; no newline buffering. |
| `^\w[^>]*#`, `^\w[^#]*>` | Host/prompt body is capped at 127 bytes and excludes CR/LF | Preserve line anchoring without an unbounded negated class. |
| `hostname` and every bare-word alternative | ASCII-alphanumeric guards: `(?<![A-Za-z0-9])…(?![A-Za-z0-9])` | `_`, `-`, `/`, `.`, `:`, and `,` remain separators; embedded prose/hostnames do not match. |
| Every rule | Compiled case-insensitively | The source list has `Match Case` off. |
| `$` in `(?:\x20\|$)` and `@+$` | End of line: space, CR, LF or end of the received text | Python's `$` is end of buffer; a line's last `up` must still match. |
| `^` in prompt and `^\]` rules | Start of a real line, tracked across chunks and escape sequences | The highlighter only sees a window of the stream, which is not a line start. |
| Both leading-`.*thernet…` rules | Start at the interface token (`…thernet`) rather than line start | Colour the meaningful token, not unrelated line text. |
| Interface numeric `+` repetitions | 1–5 components/digits as appropriate | Cisco interface identifiers are finite; bounds prevent pathological scans. |
| `[0-9|\/|:|,]+` | `[0-9/:,]{1,32}` | Correct the source character-class `|` typo and bound it. |
| `con`, `vty`, `line`, `aux`, `console` | Alphanumeric whole-word guards | Retain all noisy short words without matching inside prose. |
| `wwn`, `pwwn` | Alphanumeric whole-word guards; WWN remains eight octets | Avoid embedded matches. |
| IPv4/IP:port rule | Each octet remains 1–3 digits; prefix/port/list widths are bounded; alphanumeric/dot guards added | Preserve source acceptance while preventing partial embedded matches. |
| Netmask rule | Remove empty `(?:)` and add alphanumeric/dot guards | Empty group has no semantic effect. |
| MAC rules | Add hexadecimal-character guards | Prevent partial MAC matches. |
| IOS version rule | `*` fields capped at 16/32/128 characters; never crosses CR/LF | Remove unbounded repetitions while retaining both version forms. |
| Serial rule | `.` becomes one non-newline byte and outer guards are added | Never consume across lines. |
| `yes`, `permit`, `[OK]`, `on`, `enabled` | Alphanumeric guards; optional `.` is one non-newline byte | Retain punctuation behaviour without matching inside words. |
| `.*SUCCESS.*`, `.*success.*`, `passed.*` | Begin at the trigger and colour the rest of visible line as chunks arrive | Leading unrelated text is left unchanged; no newline wait. |
| `.*_ERR:` | Colour from the start of the line (as far as not yet shown) to `_ERR:` | Prefix-only rule: the rest of the line is not coloured. |
| `.*fail.*`, `.*reload.*` | Begin at `fail` or `reload`, then colour to newline | Same streaming rule; `invalid` remains token-only. |
| `shut.*` | Trigger is bounded/guarded; matching line remainder is streamed | Never crosses CR/LF. |
| `.*down.?` | Colour from the start of the line to `down` plus one character | Prefix-only: in `… is down, line protocol is up` the `up` stays green. |
| `not`, `Off`, `1024`, `768`, `des`, `des56.*` | Exact ASCII-alphanumeric guards; only guarded `des56` streams a remainder | `3des` and `des56x` do not match. |
| `.?` suffixes (`initializing`, `half-duplex`, maps/policies, description, delete) | One optional non-newline byte | Preserve the source wildcard without consuming a newline. |
| Misc table headings (`H`, `V`, `Q`, `AS`, `ID`, etc.) | Exact ASCII-alphanumeric guards | Keep every short heading active without mangling longer words. |
| `State/Pf.*` | `State/Pf` triggers streamed remainder colouring | Avoid an unbounded regex. |
| `vlan\d*` | At most four digits | Cisco VLAN range is finite and matching stays bounded. |
| Confirmation and `-more-` rules | Trigger tokens are matched immediately; greedy alternatives stream to newline; `-more-` includes up to 8 leading dashes | Pager prompts often have no newline; `--More--` is coloured whole. |
| `username.*`, `password.*` | Guarded trigger; stream remainder | Avoid leaking colour to the next line. |
| `.*-key` | Colour from the start of the line to `-key` | Prefix-only: `pre-shared-key abc 10.0.0.1` keeps the address green. |
| `(hitcnt=[1-9][0-9]*)` | At most ten digits | Counters remain practical and bounded. |
| `\*+`, `!+`, `###+`, `@+$`, progress `#+` | Runs capped at 128 characters | Prevent arbitrarily long repetition while preserving display use. |
| V3 progress rule | Emits bold only for its span and restores foreground/bold/faint safely | Prevent bold leakage and preserve an already-bold or faint remote stream. |
| `snmp.*` | Guarded `snmp` trigger streams to newline | Preserve the greedy service rule without crossing lines. |
| `%…-[0-9]-…:` | Non-newline fields capped at 128 bytes; lazy scan to severity/tag delimiter | Match Cisco syslog tags without nested/unbounded wildcards. |
| `\b.*\.(bin|tar)` | Match a non-space filename of at most 128 bytes | Do not colour unrelated text before a filename. |
| Final `.*|setasregextosetdefaultcolor` | Stored as the last rule but emits no SGR | It represents terminal-default grey; allowing it to overwrite with injected grey would defeat terminal themes. |

Because visible text is never held, a token split inside the word is coloured
from the second chunk onward (the already displayed prefix cannot be repainted).
The carried prefix still participates in both look-behind and look-ahead, so
`n` + `o shutdown` matches `no`, while `Not` + `ification` does not match
`not`. A complete keyword delivered exactly at a chunk end is necessarily
decided with the bytes currently available (`Not` + `ification` colours `Not`);
waiting for a future separator would violate immediate prompt display.

A rule that streams to the end of the line keeps its own priority in the next
chunk, so later rules still override it exactly as if the line had arrived in
one piece. Each rule paints each byte at most once, so the work per chunk is
linear in its size.
