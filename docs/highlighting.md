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
  rule is represented as the unstyled terminal-default baseline.

## Colour themes

`securecrt` is the default and emits the source COLORREF values as truecolour
`38;2;R;G;B` after converting SecureCRT's `00BBGGRR` order to RGB. `dark` is a
brighter black-background variant, `light` is a darker white-background
variant, and `high-contrast` maximises separation. `terminal-palette` emits
ANSI palette indices 31–36/90–97 so the terminal theme owns the actual colours.

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
Malformed/unsafe rules are skipped with a warning. The safe subset limits a
pattern to 512 ASCII characters, converts `.*` to a bounded single-line match,
and rejects lookarounds, backreferences, and nested repetition. This is a
safe-compile complexity guard, not a general-purpose regex sandbox.

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
| `invalid` | Also accepts `Invalid` | SecureCRT's visual match is case-insensitive for the captured Cisco error form. |
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
| `.*_ERR:`, `.*fail.*`, `.*reload.*` | Begin at `_ERR:`, `fail`, or `reload`, then colour to newline | Same streaming rule; `invalid` remains token-only. |
| `shut.*`, `.*down.?` | Trigger is bounded/guarded; matching line remainder is streamed | Never crosses CR/LF. |
| `not`, `Off`, `1024`, `768`, `des`, `des56.*` | Exact ASCII-alphanumeric guards; only guarded `des56` streams a remainder | `3des` and `des56x` do not match. |
| `.?` suffixes (`initializing`, `half-duplex`, maps/policies, description, delete) | One optional non-newline byte | Preserve the source wildcard without consuming a newline. |
| Misc table headings (`H`, `V`, `Q`, `AS`, `ID`, etc.) | Exact ASCII-alphanumeric guards | Keep every short heading active without mangling longer words. |
| `State/Pf.*` | `State/Pf` triggers streamed remainder colouring | Avoid an unbounded regex. |
| `vlan\d*` | At most four digits | Cisco VLAN range is finite and matching stays bounded. |
| Confirmation and `-more-` rules | Trigger tokens are matched immediately; greedy alternatives stream to newline | Pager prompts often have no newline. |
| `username.*`, `password.*`, `.*-key` | Guarded trigger; key prefix capped at 64 characters; stream remainder | Avoid leaking colour to the next line or scanning without bound. |
| `(hitcnt=[1-9][0-9]*)` | At most ten digits | Counters remain practical and bounded. |
| `\*+`, `!+`, `###+`, `@+$`, progress `#+` | Runs capped at 128 characters | Prevent arbitrarily long repetition while preserving display use. |
| V3 progress rule | Emits bold only for its span and restores foreground/bold safely | Prevent bold leakage and preserve an already-bold remote stream. |
| `snmp.*` | Guarded `snmp` trigger streams to newline | Preserve the greedy service rule without crossing lines. |
| `%…-[0-9]-…:` | Non-newline fields capped at 128 bytes; lazy scan to severity/tag delimiter | Match Cisco syslog tags without nested/unbounded wildcards. |
| `\b.*\.(bin|tar)` | Match a non-space filename of at most 128 bytes | Do not colour unrelated text before a filename. |
| Final `.*|setasregextosetdefaultcolor` | Stored as the last rule but emits no SGR | It represents terminal-default grey; allowing it to overwrite with injected grey would defeat terminal themes. |

Because visible text is never held, a token split inside the word is coloured
from the second chunk onward (the already displayed prefix cannot be repainted).
The carried prefix still participates in both look-behind and look-ahead, so
`n` + `o shutdown` matches `no`, while `Not` + `ification` does not match
`not`. A complete keyword delivered exactly at a chunk end is necessarily
decided with the bytes currently available; waiting for a future separator
would violate immediate prompt display.
