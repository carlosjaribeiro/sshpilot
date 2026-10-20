# Terminal session logging

Session logging is opt-in under **Settings → Terminal → Session Logging**.
It writes one plain-text transcript for each newly opened terminal session.

Choose a log folder and file-name pattern. An empty folder uses the sshPilot
data directory under `logs`. The default pattern creates a dated subdirectory
and includes the session name and host.

Supported placeholders are `%S` (session name), `%H` (host), `%Y` (year), `%M`
(month), `%D` (day), `%h` (hour), `%m` (minute), `%s` (second), and `%%`
(literal percent sign). Session and host values are sanitised before becoming
path components, and relative patterns cannot escape the selected folder.

Terminal control sequences are removed from the transcript. Logging failures
stop logging for that session and show a notification; they do not interrupt
the terminal session. Replayed output after reattachment is not logged twice.
