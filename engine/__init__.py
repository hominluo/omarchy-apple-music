"""The Apple Music engine for the Omarchy shell.

Modules:

    config   paths, settings, artwork URL templates
    auth     the media-user-token: Chrome cookie database and the sign-in window
    apple    Apple's web API (dev token, catalog, library, playback, licenses)
    stream   HLS -> segments -> Widevine key -> decrypted m4a
    player   the mpv process and its IPC socket
    daemon   the queue, command dispatch and the unix socket
    cli      `serve` plus the terminal verbs
"""

__version__ = "0.2.2"
