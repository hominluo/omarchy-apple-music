# Apple Music for Omarchy

A native Apple Music player for the [Omarchy](https://omarchy.org) shell — catalog
and library browsing in the bar and a full-window browser, with playback through
mpv. No Electron, no browser at runtime.

> Status: early. The repository is scaffolding while the playback path is
> validated. Nothing here is installable yet.

## Shape

- **Plugin** (`io.github.hominluo.apple-music`), kinds `service`, `bar-widget`
  and `overlay` — the same contract the Omarchy Podcast plugin uses.
- **UI** in QML on `qs.Ui` / `qs.Commons`, so themes, motion, keyboard focus,
  translations and bar behaviour come from the shell rather than being faked.
- **Daemon** in Python behind the service: Apple Music API for metadata, one
  cached credential, local Widevine decryption, and mpv over its JSON IPC.
- **Transport** published through MPRIS by `mpv-mpris`, so media keys,
  `omarchy.media` and the bar work without extra plumbing.

## Sign-in

Signing in is a real window, not a token field: the plugin opens the Apple
Music web player, the user logs in there once, and the daemon keeps the
`media-user-token` (mode 0600) for later API and license calls. The window
closes itself when it has the token.

## Why not the web player

The web player (Cider and friends) is a browser wrapped around MusicKit: it works,
but it costs half a gigabyte of memory and a second Chrome to play a song. Apple
serves the same 256 kbps AAC stream to Chrome as Widevine; decrypting it locally
lets mpv do the playing, which is smaller, seekable, cacheable and already speaks
MPRIS.

## License

MIT
