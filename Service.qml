import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io

// The one object in the shell that talks to `apple-musicd`.
//
// Bar widgets exist once per monitor and the browser window is a separate
// component, so none of them may own a socket or a process. They read the
// state slices below (player, queue, catalog slices) and call the command
// functions; this service keeps a single connection to the daemon and starts
// it when it is not running.
//
// Wire format is newline-delimited JSON: requests are correlated by id and
// answered through a callback, state arrives as events named after the slice
// they replace.
Item {
  id: root

  // Injected by the host.
  property string omarchyPath: ""
  property var shell: null
  property var manifest: null
  property var barWidgetRegistry: null
  property var pluginRegistry: null

  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "io.github.hominluo.apple-music"
  readonly property string pluginVersion: manifest && manifest.version ? String(manifest.version) : ""

  // Mirrors engine/config.py: the socket lives in the private per-user
  // runtime directory and nowhere else.
  readonly property string runtimeDir: {
    var dir = Quickshell.env("XDG_RUNTIME_DIR")
    return dir && dir !== "" ? dir.replace(/\/+$/, "") : ""
  }
  readonly property string socketPath: runtimeDir !== "" ? runtimeDir + "/omarchy-apple-music/daemon.sock" : ""
  // Written by apple-musicd.py / engine/cli.py when the daemon fails to come
  // up, removed once it listens (see cli.py's startup errors).
  readonly property string startupErrorPath: runtimeDir !== "" ? runtimeDir + "/omarchy-apple-music/startup-error" : ""
  // Written by the daemon that holds the lock; the only handle on an engine
  // too old to know `quit`, and how a retire tells the old daemon from its
  // replacement (see _retireEngine).
  readonly property string pidPath: runtimeDir !== "" ? runtimeDir + "/omarchy-apple-music/daemon.pid" : ""
  readonly property string launcherPath: decodeURIComponent(String(Qt.resolvedUrl("apple-musicd.py")).replace(/^file:\/\//, ""))

  // ---- state mirrored from the daemon ------------------------------------
  property var player: ({ songId: null, pos: 0, duration: 0, paused: true, idle: true, volume: 100, loading: false })
  property var nowPlaying: null
  property var queue: ({ index: -1, context: {}, shuffle: false, repeat: "off", items: [] })
  property var auth: ({ signedIn: false, error: "", name: "", storefront: "us", status: "unknown", endsAt: "", source: "" })
  property var build: ({})
  property var settings: ({})
  property var engine: ({ version: "", protocol: 0, mpv: "stopped" })

  // Connection: "connected" | "connecting" | "starting" | "unavailable".
  // "unavailable" carries the reason in startupError.
  property string connection: "connecting"
  readonly property bool connected: connection === "connected"
  property string engineVersion: ""
  property string startupError: ""
  // The reason as every surface shows it: only once the connection state
  // has decided it belongs to this run (see _updateConnectionState); a
  // leftover from an earlier run must not sit under "starting".
  readonly property string currentError: connection === "unavailable" ? startupError : ""
  // Non-empty while the shell runs on with an engine that does not match
  // this plugin because replacing it did not work (see _retireEngine).
  property string engineMismatch: ""

  // Must equal PROTOCOL in engine/daemon.py. The daemon is detached and
  // outlives plugin updates, so the handshake checks this and the engine
  // version against manifest.json and replaces an engine from another
  // release instead of driving it with commands it does not have.
  readonly property int protocol: 3

  signal notice(var payload)

  // ---- derived -----------------------------------------------------------
  readonly property bool hasSong: !!nowPlaying
  readonly property string title: nowPlaying ? String(nowPlaying.title || "") : ""
  readonly property string artist: nowPlaying ? String(nowPlaying.artist || "") : ""
  readonly property string album: nowPlaying ? String(nowPlaying.album || "") : ""
  readonly property string artwork: nowPlaying ? String(nowPlaying.artwork || "") : ""
  // A radio station is playing rather than a song: nowPlaying then names
  // the station, and `artist` is its current show or provider.
  readonly property bool isStation: hasSong && String(nowPlaying.type || "") === "station"
  // A live stream (Apple Music 1, a broadcaster): no length and no seeking,
  // only "now". Episodes and track stations are not live.
  readonly property bool isLive: hasSong && nowPlaying.live === true

  // nowPlaying moves the moment a track is picked, but mpv keeps the old file
  // until the new one has been fetched and decrypted. player.songId is the
  // file mpv really has, so until it matches (or while the daemon says it is
  // loading) every surface shows the new title as "Preparing…" rather than
  // pairing it with the old track's clock and seek bar.
  readonly property bool trackLoaded: hasSong && !!player && player.songId !== null
                                      && player.songId !== undefined
                                      && String(player.songId) === String(nowPlaying.id)
  // The build map as surfaces read it. `build` is the last snapshot of a
  // daemon that may be gone, and a build it was running died with it, so
  // nothing shows a step (or a failure) of one while disconnected.
  readonly property var buildStages: connected && build ? build : ({})
  readonly property string nowBuildStage: {
    if (!nowPlaying) return ""
    var entry = buildStages[String(nowPlaying.id)]
    return entry && entry.stage ? String(entry.stage) : ""
  }
  // An idle player with a stale nowPlaying is the end of the queue, not a
  // build: only count it as preparing when the daemon or a build says so.
  // Both need the connection: the slices are the last snapshot of a daemon
  // that may be gone, and a build it was running died with it.
  readonly property bool preparing: connected && hasSong && !!player
    && (player.loading === true
        || (!trackLoaded && nowBuildStage !== "failed"
            && (player.idle !== true || nowBuildStage !== "")))
  readonly property bool prepareFailed: connected && hasSong && !trackLoaded && nowBuildStage === "failed"
  readonly property string prepareMessage: {
    if (prepareFailed) {
      var entry = buildStages[String(nowPlaying.id)]
      return entry && entry.message ? "Unavailable: " + String(entry.message) : "This track is unavailable"
    }
    if (!preparing) return ""
    // A station has no file to fetch and decrypt in the listener's terms:
    // it is being tuned in, whatever step the daemon is on.
    if (isStation) return "Tuning in…"
    switch (nowBuildStage) {
      case "resolving": return "Preparing… finding the stream"
      case "downloading": return "Preparing… downloading"
      case "decrypting": return "Preparing… decrypting"
      default: return "Preparing…"
    }
  }

  // Playing means sound from the track on screen: not while the daemon is
  // gone (mpv dies with it), not on an idle player at the end of the queue
  // even if a media key unpaused it, and not while the next track is built.
  readonly property bool playing: connected && hasSong && !!player && !player.paused
                                  && player.idle !== true && !preparing
  readonly property real position: trackLoaded && !preparing ? Number(player.pos) || 0 : 0
  readonly property real duration: {
    if (trackLoaded && !preparing && Number(player.duration) > 0) return Number(player.duration)
    if (nowPlaying) return (Number(nowPlaying.durationMs) || 0) / 1000
    return player ? Number(player.duration) || 0 : 0
  }
  // An idle player (mpv crashed or was released, or the queue ran out) has
  // no file to seek in, even though songId and duration are still set. A
  // live stream has nothing behind "now" to seek to.
  readonly property bool canSeek: connected && trackLoaded && !preparing && player.idle !== true
                                  && duration > 0 && !isLive
  readonly property int volume: player ? Math.max(0, Math.min(100, Math.round(Number(player.volume) || 0))) : 0

  readonly property var queueItems: queue && Array.isArray(queue.items) ? queue.items : []
  readonly property int queueIndex: queue ? Number(queue.index) : -1
  readonly property bool signedIn: auth ? auth.signedIn === true : false
  // Apple turned the stored token down: the daemon then reports signedIn
  // false and expired true, and nothing will play until the user signs in
  // again.
  readonly property bool authExpired: auth ? auth.expired === true : false
  readonly property bool needsSignIn: !signedIn || authExpired
  // A token is stored, usable or not. An expired one stays on disk and is
  // re-checked at every start, so Sign out must stay reachable for it.
  readonly property bool hasToken: auth ? (auth.hasToken === true || signedIn || authExpired) : false
  readonly property string storefront: auth && auth.storefront ? String(auth.storefront) : ""

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  // ---- request plumbing --------------------------------------------------
  property int _nextId: 1
  property var _pending: ({})
  property var _outbox: []
  property bool _flushScheduled: false
  property double _lastSpawn: 0
  property int _retryMs: 250
  // Spawns since the last successful handshake. The first few are quick;
  // after that a daemon that keeps failing is retried once a minute instead
  // of every few seconds, until the user does something (wake()).
  property int _spawnsWithoutConnect: 0
  readonly property int _quickSpawns: 3
  readonly property int _quickSpawnGapMs: 3000
  readonly property int _slowSpawnGapMs: 60000
  property double _startupErrorAt: 0
  // When the current run of failed spawns began (the first spawn after a
  // connect, or after the user woke it).
  property double _streakStart: 0

  // A press made while the daemon is (re)starting is delivered if it comes up
  // within this long; an older one is dropped with a notice rather than
  // replayed out of the blue.
  readonly property int _outboxMaxAgeMs: 5000
  readonly property int _outboxCap: 20

  function request(cmd, params, callback) {
    var message = { cmd: cmd, params: params || {} }
    if (callback) {
      message.id = root._nextId++
      var pending = root._pending
      pending[message.id] = callback
      root._pending = pending
    }
    if (!root.connected) root.wake()
    root._send(message, !!callback)
  }

  // A second Socket.write in the same event-loop turn can be dropped, so
  // requests queue up and go out as one write on the next tick.
  function _send(message, expectsReply) {
    root._outbox.push({ text: JSON.stringify(message), at: Date.now(), reply: expectsReply === true })
    if (root._outbox.length > root._outboxCap) root._outbox.splice(0, root._outbox.length - root._outboxCap)
    if (!root._flushScheduled) {
      root._flushScheduled = true
      Qt.callLater(root._flush)
    }
  }

  // Drops fire-and-forget commands too old to replay. Requests with a
  // callback are failed and dropped by _onDisconnected instead.
  function _pruneOutbox() {
    var now = Date.now()
    var kept = []
    var dropped = 0
    for (var i = 0; i < root._outbox.length; i++) {
      var entry = root._outbox[i]
      if (entry.reply || now - entry.at <= root._outboxMaxAgeMs) kept.push(entry)
      else dropped++
    }
    root._outbox = kept
    if (dropped > 0)
      root.notice({ level: "error", message: "The Apple Music player is not running, so that was not sent" })
  }

  function _flush() {
    root._flushScheduled = false
    if (root._outbox.length === 0) return
    if (!root.connected || !root._socket) return
    root._pruneOutbox()
    if (root._outbox.length === 0) return
    var lines = []
    for (var i = 0; i < root._outbox.length; i++) lines.push(root._outbox[i].text)
    root._outbox = []
    root._socket.write(lines.join("\n") + "\n")
    root._socket.flush()
  }

  function _ingest(line) {
    var text = String(line || "").trim()
    if (text === "") return
    var message
    try { message = JSON.parse(text) } catch (e) { console.warn("apple-music: bad line:", text.substring(0, 200)); return }
    if (message.id !== undefined && message.id !== null) {
      var callback = root._pending[message.id]
      if (callback) {
        var pending = root._pending
        delete pending[message.id]
        root._pending = pending
        try { callback(message.ok === true, message.ok === true ? message.result : message.error) }
        catch (e) { console.warn("apple-music: callback failed:", e) }
      } else if (message.ok === false && message.error) {
        console.warn("apple-music: daemon error:", JSON.stringify(message.error))
      }
      return
    }
    if (message.event) { root._applyEvent(String(message.event), message.data); return }
    // A fire-and-forget command (play/pause, next, an IPC call) that failed:
    // it was a user action, so say why instead of doing nothing.
    if (message.ok === false && message.error) {
      console.warn("apple-music: daemon error:", JSON.stringify(message.error))
      if (message.error.message) root.notice({ level: "error", message: String(message.error.message) })
    }
  }

  function _applyEvent(name, data) {
    switch (name) {
      case "snapshot":
        if (!data) return
        for (var key in data) root._applySlice(key, data[key])
        return
      case "player": case "nowPlaying": case "queue": case "auth": case "build": case "settings": case "engine":
        root._applySlice(name, data)
        return
      case "notice":
        root.notice(data)
        return
      default:
        return
    }
  }

  function _applySlice(key, value) {
    switch (key) {
      case "player": root.player = value || root.player; break
      case "nowPlaying": root.nowPlaying = value || null; break
      case "queue": root.queue = value || { index: -1, context: {}, shuffle: false, repeat: "off", items: [] }; break
      case "auth": root.auth = value || root.auth; break
      // The daemon sends the whole build map with every change, snapshot
      // or event alike, so a plain replace is right for both.
      case "build": root.build = value || ({}); break
      case "settings": root.settings = value || {}; break
      case "engine": root.engine = value || root.engine; break
    }
  }

  // ---- connection lifecycle ----------------------------------------------
  // Quickshell 0.3.1 keeps the QLocalSocket of a failed attempt inside the
  // Socket, so a Socket that failed once can never connect again: every
  // attempt gets a fresh one.
  property var _socket: null
  Component {
    id: socketComponent
    Socket {
      id: socket
      path: root.socketPath
      connected: false
      parser: SplitParser {
        onRead: function(line) { if (root._socket === socket) root._ingest(line) }
      }
      onConnectedChanged: {
        if (root._socket !== socket) return
        attemptTimer.stop()
        if (socket.connected) {
          root._retryMs = 250
          root.connection = "connecting"
          socket.write(JSON.stringify({ id: 0, cmd: "hello", params: { client: "service", protocol: root.protocol } }) + "\n")
          socket.flush()
          var handshake = root._pending
          handshake[0] = function(ok, result) {
            handshakeTimer.stop()
            if (!ok) { console.warn("apple-music: handshake refused:", JSON.stringify(result)); if (root._socket) root._socket.connected = false; return }
            var engineProtocol = result ? Number(result.protocol) || 0 : 0
            root.engineVersion = result && result.engineVersion ? String(result.engineVersion) : ""
            // An engine from another release would get commands it does not
            // know and send slices this QML does not read: replace it before
            // its snapshot reaches any surface.
            var stale = engineProtocol !== root.protocol
                        || (root.pluginVersion !== "" && root.engineVersion !== root.pluginVersion)
            // Engines from this release name their process; for older ones
            // the pid file is the next best answer.
            var answeringPid = result && Number(result.pid) > 0 ? Math.floor(Number(result.pid)) : 0
            if (stale && answeringPid === 0) answeringPid = root._readDaemonPid()
            if (stale && root._retireEngine(engineProtocol, root.engineVersion, answeringPid)) return
            if (!stale) root.engineMismatch = ""
            root._engineProtocol = engineProtocol
            var snapshot = result && result.snapshot ? result.snapshot : null
            if (snapshot) for (var key in snapshot) root._applySlice(key, snapshot[key])
            root._spawnsWithoutConnect = 0
            root.startupError = ""
            root._startupErrorAt = 0
            root.connection = "connected"
            root._flush()
          }
          root._pending = handshake
          handshakeTimer.restart()
        } else {
          root._onDisconnected()
        }
      }
      onError: function(error) { if (root._socket === socket && !socket.connected) root._onDisconnected() }
    }
  }

  function _disposeSocket() {
    var old = root._socket
    if (!old) return
    root._socket = null
    old.connected = false
    old.destroy()
  }

  function _onDisconnected() {
    attemptTimer.stop()
    handshakeTimer.stop()
    var pending = root._pending
    root._pending = ({})
    // Requests with a callback are answered "disconnected" below, so their
    // lines must not go out later as well; recent presses stay queued.
    root._outbox = root._outbox.filter(function(entry) { return !entry.reply })
    root._pruneOutbox()
    for (var id in pending) {
      if (String(id) === "0") continue
      try { pending[id](false, { code: "disconnected", message: "The player service is not connected" }) }
      catch (e) { console.warn("apple-music: callback failed:", e) }
    }
    root._readStartupError()
    // Spawn the daemon on the first failed attempt of a cycle; a second copy
    // exits when it finds the socket taken. A few quick tries cover a slow
    // first start, then once a minute: a daemon that dies on every start
    // should not be relaunched (and re-run pip) every four seconds forever.
    var now = Date.now()
    var gap = root._spawnsWithoutConnect < root._quickSpawns ? root._quickSpawnGapMs : root._slowSpawnGapMs
    if (now - root._lastSpawn > gap) root._spawn()
    root._updateConnectionState()
    reconnectTimer.interval = root._retryMs
    reconnectTimer.restart()
    root._retryMs = Math.min(4000, root._retryMs * 2)
  }

  // ---- replacing an engine from another release ---------------------------
  // What was done about an engine of one version and protocol, kept in the
  // runtime directory rather than in this object: a hot-reload builds a new
  // Service, and one that had forgotten the retire would stop the
  // replacement all over again. {state: "retired" | "accepted", pid, at}:
  // "retired" once a stop was sent (pid is the process it was sent to),
  // "accepted" once that engine came back anyway and was let run. The
  // directory is a tmpfs that goes with the login session.
  function _retirePath(engineProtocol, version) {
    if (root.runtimeDir === "") return ""
    var safe = function(text) { return String(text || "unknown").replace(/[^A-Za-z0-9._+-]/g, "_") }
    return root.runtimeDir + "/omarchy-apple-music/retired-" + safe(version) + "-p" + (Number(engineProtocol) || 0)
      + "-for-" + safe(root.pluginVersion)
  }

  function _readRetire(path) {
    if (path === "") return null
    var text = root._readFile(retireFile, path).trim()
    if (text === "") return null
    try {
      var record = JSON.parse(text)
      return record && typeof record === "object" ? record : null
    } catch (e) {
      return null
    }
  }

  function _writeRetire(path, record) {
    if (path === "") return
    retireFile.path = path
    retireFile.setText(JSON.stringify(record) + "\n")
  }

  function _readDaemonPid() {
    if (root.pidPath === "") return 0
    var text = root._readFile(pidFile, root.pidPath).trim()
    return /^[0-9]+$/.test(text) ? Number(text) : 0
  }

  FileView {
    id: retireFile
    blockLoading: true
    blockWrites: true
    atomicWrites: true
    printErrors: false
  }

  FileView {
    id: pidFile
    blockLoading: true
    printErrors: false
  }

  // No reconnect before this: a fresh attempt could reach the old daemon
  // before it has stopped listening and be taken for its replacement.
  property double _retireUntil: 0
  readonly property int _retireGraceMs: 1500
  // How long the daemon a stop was sent to may keep answering before it is
  // taken as one that will not go.
  readonly property int _retireCapMs: 15000

  function _setMismatch(engineProtocol, version, why, announce) {
    root.engineMismatch = "The Apple Music player is engine " + (version || "unknown")
      + " (protocol " + engineProtocol + ") but the plugin is " + (root.pluginVersion || "unknown")
      + " (protocol " + root.protocol + "), and " + why + "."
      + " Some controls may not work until it is replaced (log out, or pkill -f apple-musicd)."
    if (!announce) return
    console.warn("apple-music:", root.engineMismatch)
    root.notice({ level: "error", message: root.engineMismatch })
  }

  // Stops the engine on the other end of the socket so the normal respawn
  // brings up the one shipped with this plugin, and returns true while the
  // caller must not use this connection. Returns false (and the caller
  // carries on with the old engine) once that was tried for this engine
  // version and a different process answered just as old: killing it again
  // would only loop. The same process answering again is the old daemon
  // still closing, not its replacement, so that only waits, for a while.
  function _retireEngine(engineProtocol, version, answeringPid) {
    var path = root._retirePath(engineProtocol, version)
    var record = root._readRetire(path)
    if (record && record.state === "accepted") {
      // The reason the first Service concluded, so a hot-reload does not
      // turn "it did not stop when asked" into a different story.
      var known = typeof record.why === "string" && record.why !== "" ? record.why : "restarting it did not help"
      root._setMismatch(engineProtocol, version, known, false)
      return false
    }
    var now = Date.now()
    if (record && record.state === "retired") {
      var retiredPid = Number(record.pid) || 0
      var age = now - (Number(record.at) || 0)
      var stillClosing = retiredPid > 0 && answeringPid === retiredPid
      if (stillClosing && age >= 0 && age < root._retireCapMs) {
        // Spawns while it holds the lock just exit, so they must not use
        // up the quick ones: the replacement comes a quick gap from now.
        root._spawnsWithoutConnect = 0
        root._lastSpawn = now
        root._retireUntil = now + root._retireGraceMs
        root.connection = "starting"
        if (root._socket) root._socket.connected = false
        return true
      }
      var why = stillClosing ? "it did not stop when asked" : "restarting it did not help"
      root._writeRetire(path, { state: "accepted", pid: answeringPid, at: now, why: why })
      root._setMismatch(engineProtocol, version, why, true)
      return false
    }
    // Read now, before the stop: the pid file goes with the old daemon.
    root._writeRetire(path, { state: "retired", pid: root._readDaemonPid(), at: now })
    console.warn("apple-music: engine", version || "unknown", "protocol", engineProtocol,
                 "does not match plugin", root.pluginVersion || "unknown", "protocol", root.protocol,
                 "- restarting it")
    // Set before the socket goes, since _onDisconnected reads them: a fresh
    // run of spawns, the first one a quick-spawn gap from now so the old
    // daemon has let go of its lock by then.
    root._spawnsWithoutConnect = 0
    root._lastSpawn = now
    root._retireUntil = now + root._retireGraceMs
    root.connection = "starting"
    if (engineProtocol >= 2 && root._socket) {
      // It knows quit and closes every connection itself once the answer
      // is out; dropping ours first could lose the line. handshakeTimer
      // drops it if the daemon never does.
      root._socket.write(JSON.stringify({ cmd: "quit", params: {} }) + "\n")
      root._socket.flush()
      handshakeTimer.restart()
    } else {
      // Too old for quit. Its SIGTERM handler shuts down cleanly (socket,
      // pid file, mpv), but it waits for open connections before exiting,
      // so ours goes now.
      root._terminateOldEngine()
      if (root._socket) root._socket.connected = false
    }
    return true
  }

  // SIGTERM for an engine too old to know `quit`. The pid comes from the pid
  // file and is signalled only if its cmdline is this plugin's
  // apple-musicd.py running serve (an old cli also takes no verb as serve),
  // so a reused pid or a `call` from a terminal is never hit. Detached, so it
  // also runs from Component.onDestruction.
  function _terminateOldEngine() {
    if (root.pidPath === "") return
    Quickshell.execDetached(["sh", "-c",
      "pid=$(cat \"$1\" 2>/dev/null) || exit 0\n"
      + "case \"$pid\" in ''|*[!0-9]*) exit 0 ;; esac\n"
      + "want=$(readlink -f -- \"$2\") || exit 0\n"
      + "args=$(tr '\\0' '\\n' 2>/dev/null < \"/proc/$pid/cmdline\") || exit 0\n"
      + "found=0 skip=0 verb=\n"
      + "while IFS= read -r arg; do\n"
      + "  if [ \"$found\" = 0 ]; then\n"
      + "    case \"$arg\" in\n"
      + "      apple-musicd.py|*/apple-musicd.py)\n"
      + "        case \"$arg\" in /*) path=$arg ;; *) path=/proc/$pid/cwd/$arg ;; esac\n"
      + "        [ \"$(readlink -f -- \"$path\")\" = \"$want\" ] && found=1 ;;\n"
      + "    esac\n"
      + "  elif [ \"$skip\" = 1 ]; then skip=0\n"
      + "  else\n"
      + "    case \"$arg\" in --timeout) skip=1 ;; -*) ;; *) verb=$arg; break ;; esac\n"
      + "  fi\n"
      + "done <<EOF\n"
      + "$args\n"
      + "EOF\n"
      + "[ \"$found\" = 1 ] || exit 0\n"
      + "case \"$verb\" in ''|serve) kill -TERM \"$pid\" ;; esac\n",
      "apple-music-retire", root.pidPath, root.launcherPath])
  }

  function _spawn() {
    root._lastSpawn = Date.now()
    if (root._spawnsWithoutConnect === 0) root._streakStart = root._lastSpawn
    root._spawnsWithoutConnect++
    Quickshell.execDetached(["python3", root.launcherPath, "serve"])
  }

  // "unavailable" once a spawn of the current run has said why it failed.
  // An older message (a previous session's, or one from before the user
  // asked for a retry) only counts once the new spawn has had time to boot.
  function _updateConnectionState() {
    if (root.connected) return
    if (root.socketPath === "") { root.connection = "unavailable"; return }
    var now = Date.now()
    var booting = now - root._lastSpawn < 15000
    // `at` is whole seconds, hence the second of slack.
    var failedThisRun = root._startupErrorAt >= root._streakStart - 1000
    if (root.startupError !== "" && (failedThisRun || !booting)) root.connection = "unavailable"
    else root.connection = booting ? "starting" : "connecting"
  }

  function _readStartupError() {
    if (root.startupErrorPath === "") return
    var text = root._readFile(startupErrorFile, root.startupErrorPath).trim()
    if (text === "") { root.startupError = ""; root._startupErrorAt = 0; return }
    var parsed = null
    try { parsed = JSON.parse(text) } catch (e) { parsed = null }
    var message = parsed && parsed.message ? String(parsed.message) : ""
    root.startupError = message !== "" ? message : "The Apple Music player failed to start"
    root._startupErrorAt = parsed && Number(parsed.at) > 0 ? Number(parsed.at) * 1000 : Date.now()
  }

  // The user did something that needs the daemon (opened the window, pressed
  // play): forget the back-off and try now.
  function wake() {
    if (root.connected || root.socketPath === "") return
    root._spawnsWithoutConnect = 0
    root._retryMs = 250
    var attempting = attemptTimer.running || (root._socket && root._socket.connected)
    if (!attempting && reconnectTimer.running) {
      reconnectTimer.stop()
      Qt.callLater(root._connect)
    }
  }

  FileView {
    id: startupErrorFile
    blockLoading: true
    printErrors: false
  }

  // The file as it is now. FileView.reload() loads in the background and
  // text() hands back the previous contents meanwhile, even with
  // blockLoading (a pid file read right after the daemon replaced it still
  // said the old pid), whereas a new path is loaded before text() returns.
  function _readFile(view, path) {
    view.path = ""
    view.path = path
    return String(view.text() || "")
  }

  function _connect() {
    if (root.socketPath === "") {
      root.startupError = "XDG_RUNTIME_DIR is not set, so the player has nowhere private to listen"
      root.connection = "unavailable"
      return
    }
    if (root._socket && root._socket.connected) return
    var wait = root._retireUntil - Date.now()
    if (wait > 0) {
      reconnectTimer.interval = wait
      reconnectTimer.restart()
      return
    }
    root._disposeSocket()
    attemptTimer.restart()
    root._socket = socketComponent.createObject(root)
    root._socket.connected = true
  }

  Timer {
    id: reconnectTimer
    interval: 250
    repeat: false
    onTriggered: root._connect()
  }

  Timer {
    id: attemptTimer
    interval: 5000
    repeat: false
    onTriggered: {
      if (!root._socket || !root._socket.connected) root._onDisconnected()
      else if (!root.connected) root._socket.connected = false
    }
  }

  Timer {
    id: handshakeTimer
    interval: 5000
    repeat: false
    onTriggered: { if (!root.connected && root._socket) root._socket.connected = false }
  }

  Component.onCompleted: root._connect()

  // ---- teardown ----------------------------------------------------------
  // The daemon is detached and owns its own lifetime: it runs only while the
  // shell's config has this plugin enabled, and re-reads that file when it
  // changes, so a disable or removal stops it (and mpv) within a few seconds,
  // also one still on its way up when this object went. A shell restart or
  // a plugin hot-reload leaves the config alone, and the next Service
  // reconnects while playback carries on.
  //
  // What is left here is the fast path. The host keeps
  // `pluginRegistry.enabled` current while the plugin stays enabled and
  // destroys the registry object when it stops being so, before this
  // service. On a shell quit Quickshell tears everything down without
  // aboutToQuit, and the registry object is still alive and enabled when
  // this runs; a hot-reload leaves it alive and enabled too.
  property bool _registrySeen: false
  onPluginRegistryChanged: if (pluginRegistry) _registrySeen = true
  // Protocol of the engine on the current connection, set by the handshake.
  property int _engineProtocol: 0

  function _beingRemoved() {
    var registry = root.pluginRegistry
    if (registry && registry.enabled === true) return false
    if (registry && registry.enabled === false) return true
    return root._registrySeen
  }

  FileView {
    id: shellConfigFile
    blockLoading: true
    printErrors: false
  }

  // Whether the shell's own config file is one the shell actually uses: a
  // non-empty JSON object with version 1 (shell.qml applyShellConfig). When
  // it is not - a hand edit saved non-atomically, a half-written file - the
  // shell falls back to its defaults, where no third-party plugin is
  // enabled, and drops this service although nobody disabled anything.
  function _shellConfigUsable() {
    var text = root._readFile(shellConfigFile, Quickshell.env("HOME") + "/.config/omarchy/shell.json").trim()
    if (text === "") return false
    try {
      var parsed = JSON.parse(text)
      return !!parsed && typeof parsed === "object" && !Array.isArray(parsed) && parsed.version === 1
    } catch (e) {
      return false
    }
  }

  // Not connected: nothing to do here. A daemon still starting checks the
  // config before it starts mpv and exits on its own.
  function _quitDaemon() {
    if (!root.connected || !root._socket) return
    if (root._engineProtocol >= 2) {
      // An unusable config is no answer: leave it to the daemon's own check,
      // which reads it the same way and re-checks once the file is whole.
      if (!root._shellConfigUsable()) return
      root._socket.write(JSON.stringify({ cmd: "quit", params: {} }) + "\n")
      root._socket.flush()
      return
    }
    // An older engine let run after a failed replace: it knows neither quit
    // nor the config rule, so it would play on with no shell to stop it.
    root._terminateOldEngine()
  }

  Component.onDestruction: {
    if (root._beingRemoved()) root._quitDaemon()
  }

  // ---- commands ----------------------------------------------------------
  function search(term, callback) { root.request("search", { term: term }, callback) }
  // The catalogue's pages, all answered as {title, sections}: "home"
  // (personal, needs sign-in), "new" (editorial) and "radio".
  function browse(page, callback) { root.request("browse", { page: String(page || "home") }, callback) }
  function home(callback) { root.browse("home", callback) }
  // A genre page, opened from a link on New or Radio.
  function grouping(id, callback) { root.request("grouping", { id: String(id || "") }, callback) }
  // A radio show or an editor: {info, sections}.
  function curator(id, kind, callback) {
    root.request("curator", { id: String(id || ""), kind: String(kind || "") }, callback)
  }
  function charts(callback) { root.request("charts", {}, callback) }
  // A "See All" page, a page at a time like the library: {title, items, next}.
  function room(id, offset, callback) {
    if (typeof offset === "function") { callback = offset; offset = 0 }
    root.request("room", { id: String(id || ""), offset: Math.max(0, Number(offset) || 0) }, callback)
  }
  // `offset` pages through the library: the answer carries `next`, the
  // offset of the following page, or null after the last.
  function library(kind, offset, callback) {
    if (typeof offset === "function") { callback = offset; offset = 0 }
    root.request("library", { kind: kind, offset: Math.max(0, Number(offset) || 0) }, callback)
  }
  // Named *Detail because `album` and `artist` are also the now-playing
  // strings the bar reads; a function of the same name would shadow them.
  function albumDetail(id, isLibrary, callback) { root.request("album", { id: id, library: !!isLibrary }, callback) }
  function playlistDetail(id, isLibrary, callback) { root.request("playlist", { id: id, library: !!isLibrary }, callback) }
  function artistDetail(id, isLibrary, callback) { root.request("artist", { id: id, library: !!isLibrary }, callback) }

  function play(tracks, index, context) {
    root.request("play", { tracks: tracks || [], index: index || 0, context: context || {} })
  }
  function playSong(song) { root.play([song], 0, { title: song.title, artwork: song.artwork }) }
  // A station is not a track list: the daemon tunes in (a live stream) or
  // fills the queue from it (a track station) and keeps it topped up.
  function playStation(station, callback) { root.request("playStation", { station: station }, callback) }
  // A station seeded from a song or an artist, which starts playing.
  function createStation(id, type, callback) {
    root.request("createStation", { id: String(id || ""), type: type === "artist" ? "artist" : "song" }, callback)
  }
  // Play Next / Play Later: into the queue after the current track, or at
  // its end. Answers {count}.
  function enqueue(tracks, mode, callback) {
    root.request("enqueue", { tracks: tracks || [], mode: mode === "next" ? "next" : "later" }, callback)
  }
  // A live station's schedule: {info, schedule}.
  function stationInfo(id, callback) { root.request("station", { id: String(id || "") }, callback) }
  function toggle() { root.request("toggle", {}) }
  function resume() { root.request("resume", {}) }
  function pause() { root.request("pause", {}) }
  function next() { root.request("next", {}) }
  function previous() { root.request("previous", {}) }
  function jump(index) { root.request("jump", { index: index }) }
  // Seeking a track that is not loaded yet would seek the previous file.
  function seek(pos) {
    if (!root.canSeek) return
    root.request("seek", { pos: Math.max(0, Math.min(root.duration, Number(pos) || 0)) })
  }
  function setVolume(value) {
    root.request("volume", { value: Math.max(0, Math.min(100, Math.round(Number(value) || 0))) })
  }
  // The daemon remembers the level to come back to, so every surface (and a
  // restart) agrees on what unmute means.
  function mute() { root.request("mute", {}) }
  function setShuffle(on) { root.request("shuffle", { on: on }) }
  function cycleRepeat() { root.request("repeat", {}) }

  function signIn(callback) { root.request("signin", {}, callback) }
  // "signOut" also clears the sign-in profile's session; an engine from
  // before that command only knows "signout", which is still better than
  // a button that does nothing.
  function signOut(callback) {
    root.request("signOut", {}, function(ok, result) {
      if (!ok && result && result.code === "bad-request") { root.request("signout", {}, callback); return }
      if (callback) callback(ok, result)
    })
  }
  function account(callback) { root.request("account", {}, callback) }
  // {lines: [{begin, end, text}], synced, writers}; error code "not-found"
  // is a song without lyrics.
  function lyrics(id, callback) { root.request("lyrics", { id: String(id || "") }, callback) }
  // Favourites: value 1 is a favourite, -1 "suggest less", 0 neither.
  function rating(type, id, callback) { root.request("rating", { type: type, id: String(id || "") }, callback) }
  function rate(type, id, value, callback) {
    var v = Number(value) > 0 ? 1 : (Number(value) < 0 ? -1 : 0)
    root.request("rate", { type: type, id: String(id || ""), value: v }, callback)
  }
  function addToLibrary(type, id, callback) { root.request("addToLibrary", { type: type, id: String(id || "") }, callback) }
  function cacheStats(callback) { root.request("cache", {}, callback) }
  function clearCache(callback) { root.request("clearCache", {}, callback) }

  // Summons the window, optionally on a view. Not `browse`: that is the
  // daemon's page command above.
  function openWindow(view, extra) {
    if (!root.shell || typeof root.shell.summon !== "function") return false
    var payload = extra ? extra : {}
    if (view) payload.view = view
    return root.shell.summon(root.pluginId, JSON.stringify(payload))
  }

  function status() {
    return {
      connection: root.connection,
      error: root.currentError,
      engine: root.engineVersion,
      engineMismatch: root.engineMismatch,
      signedIn: root.signedIn,
      authExpired: root.authExpired,
      playing: root.playing,
      preparing: root.preparing,
      live: root.isLive,
      title: root.title,
      artist: root.artist,
      position: root.position,
      duration: root.duration,
      queue: root.queueItems.length,
    }
  }

  // ---- IPC: `omarchy-shell io.github.hominluo.apple-music <method>` ------
  IpcHandler {
    target: root.pluginId

    function ping(): string { return "pong" }
    function playPause(): void { root.toggle() }
    function play(): void { root.resume() }
    function pause(): void { root.pause() }
    function next(): void { root.next() }
    function previous(): void { root.previous() }
    function seek(position: string): void { root.seek(Number(position) || 0) }
    function volume(value: string): void { root.setVolume(Number(value) || 0) }
    function mute(): void { root.mute() }
    function browse(): void { root.openWindow("") }
    function openView(view: string, argsJson: string): void {
      var extra = {}
      try { extra = JSON.parse(argsJson || "{}") } catch (error) { extra = {} }
      root.openWindow(view, extra)
    }
    function status(): string { return JSON.stringify(root.status()) }
  }

  // ---- the dropdown, from a keybinding -----------------------------------
  // One BarWidget exists per monitor, and a fixed IPC target in each of them
  // only ever reaches whichever copy claimed it. The host routes panel
  // hotkeys through Bar.findPanelWidget instead, but not for this plugin:
  // it also declares an overlay, so shell.toggle(<id>) means the window. So
  // the widgets register here and this one handler picks the copy the way
  // BarModel.pickPanelSlot does: an open one first, then the focused output.
  property var _barWidgets: []

  function registerBarWidget(widget) {
    if (!widget || root._barWidgets.indexOf(widget) >= 0) return
    root._barWidgets = root._barWidgets.concat([widget])
  }

  function unregisterBarWidget(widget) {
    root._barWidgets = root._barWidgets.filter(function(item) { return item && item !== widget })
  }

  function panelWidget() {
    var pool = root._barWidgets.filter(function(item) { return !!item })
    var open = pool.filter(function(item) { return item.opened === true })
    if (open.length > 0) pool = open
    var monitor = Hyprland.focusedMonitor
    var focused = monitor ? String(monitor.name || "") : ""
    if (focused !== "") {
      var here = pool.filter(function(item) { return String(item.screenName || "") === focused })
      if (here.length > 0) pool = here
    }
    return pool.length > 0 ? pool[0] : null
  }

  IpcHandler {
    target: root.pluginId + ".panel"

    function open(): void { var w = root.panelWidget(); if (w) w.open() }
    function close(): void { var w = root.panelWidget(); if (w) w.close() }
    function show(): void { var w = root.panelWidget(); if (w) w.open() }
    function hide(): void { var w = root.panelWidget(); if (w) w.close() }
    function toggle(): void { var w = root.panelWidget(); if (w) w.toggle() }
  }
}
