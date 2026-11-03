import QtQuick
import Quickshell
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
  property string connection: "connecting"
  readonly property bool connected: connection === "connected"
  property string engineVersion: ""

  signal notice(var payload)

  // ---- derived -----------------------------------------------------------
  readonly property bool hasSong: !!nowPlaying
  readonly property bool playing: hasSong && player && !player.paused
  readonly property string title: nowPlaying ? String(nowPlaying.title || "") : ""
  readonly property string artist: nowPlaying ? String(nowPlaying.artist || "") : ""
  readonly property string album: nowPlaying ? String(nowPlaying.album || "") : ""
  readonly property string artwork: nowPlaying ? String(nowPlaying.artwork || "") : ""
  readonly property real position: player ? Number(player.pos) || 0 : 0
  readonly property real duration: player ? Number(player.duration) || 0 : 0
  readonly property var queueItems: queue && Array.isArray(queue.items) ? queue.items : []
  readonly property int queueIndex: queue ? Number(queue.index) : -1
  readonly property bool signedIn: auth ? auth.signedIn === true : false

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

  function request(cmd, params, callback) {
    var message = { cmd: cmd, params: params || {} }
    if (callback) {
      message.id = root._nextId++
      var pending = root._pending
      pending[message.id] = callback
      root._pending = pending
    }
    root._send(message)
  }

  // A second Socket.write in the same event-loop turn can be dropped, so
  // requests queue up and go out as one write on the next tick.
  function _send(message) {
    root._outbox.push(JSON.stringify(message))
    if (!root._flushScheduled) {
      root._flushScheduled = true
      Qt.callLater(root._flush)
    }
  }

  function _flush() {
    root._flushScheduled = false
    if (root._outbox.length === 0) return
    if (!root.connected || !root._socket) return
    var data = root._outbox.join("\n") + "\n"
    root._outbox = []
    root._socket.write(data)
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
    if (message.event) root._applyEvent(String(message.event), message.data)
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
          socket.write(JSON.stringify({ id: 0, cmd: "hello", params: { client: "service", protocol: 1 } }) + "\n")
          socket.flush()
          var handshake = root._pending
          handshake[0] = function(ok, result) {
            handshakeTimer.stop()
            if (!ok) { console.warn("apple-music: handshake refused:", JSON.stringify(result)); if (root._socket) root._socket.connected = false; return }
            root.engineVersion = result && result.engineVersion ? String(result.engineVersion) : ""
            var snapshot = result && result.snapshot ? result.snapshot : null
            if (snapshot) for (var key in snapshot) root._applySlice(key, snapshot[key])
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
    root.connection = "connecting"
    var pending = root._pending
    root._pending = ({})
    root._outbox = []
    for (var id in pending) {
      if (String(id) === "0") continue
      try { pending[id](false, { code: "disconnected", message: "The player service is not connected" }) }
      catch (e) { console.warn("apple-music: callback failed:", e) }
    }
    // Spawn the daemon on the first failed attempt of a cycle; a second copy
    // exits when it finds the socket taken.
    var now = Date.now()
    if (now - root._lastSpawn > 3000) {
      root._lastSpawn = now
      root.connection = "starting"
      Quickshell.execDetached(["python3", root.launcherPath, "serve"])
    }
    reconnectTimer.interval = root._retryMs
    reconnectTimer.restart()
    root._retryMs = Math.min(4000, root._retryMs * 2)
  }

  function _connect() {
    if (root.socketPath === "") {
      root.connection = "unavailable"
      return
    }
    if (root._socket && root._socket.connected) return
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

  // ---- commands ----------------------------------------------------------
  function search(term, callback) { root.request("search", { term: term }, callback) }
  function home(callback) { root.request("home", {}, callback) }
  function library(kind, callback) { root.request("library", { kind: kind }, callback) }
  // Named *Detail because `album` and `artist` are also the now-playing
  // strings the bar reads; a function of the same name would shadow them.
  function albumDetail(id, isLibrary, callback) { root.request("album", { id: id, library: !!isLibrary }, callback) }
  function playlistDetail(id, isLibrary, callback) { root.request("playlist", { id: id, library: !!isLibrary }, callback) }
  function artistDetail(id, isLibrary, callback) { root.request("artist", { id: id, library: !!isLibrary }, callback) }

  function play(tracks, index, context) {
    root.request("play", { tracks: tracks || [], index: index || 0, context: context || {} })
  }
  function playSong(song) { root.play([song], 0, { title: song.title, artwork: song.artwork }) }
  function toggle() { root.request("toggle", {}) }
  function resume() { root.request("resume", {}) }
  function pause() { root.request("pause", {}) }
  function next() { root.request("next", {}) }
  function previous() { root.request("previous", {}) }
  function seek(pos) { root.request("seek", { pos: pos }) }
  function setVolume(value) { root.request("volume", { value: value }) }
  function setShuffle(on) { root.request("shuffle", { on: on }) }
  function cycleRepeat() { root.request("repeat", {}) }

  function signIn(callback) { root.request("signin", {}, callback) }
  function signOut(callback) { root.request("signout", {}, callback) }
  function account(callback) { root.request("account", {}, callback) }
  function cacheStats(callback) { root.request("cache", {}, callback) }
  function clearCache(callback) { root.request("clearCache", {}, callback) }

  function browse(view, extra) {
    if (!root.shell || typeof root.shell.summon !== "function") return false
    var payload = extra ? extra : {}
    if (view) payload.view = view
    return root.shell.summon(root.pluginId, JSON.stringify(payload))
  }

  function status() {
    return {
      connection: root.connection,
      signedIn: root.signedIn,
      playing: root.playing,
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
    function browse(): void { root.browse("") }
    function openView(view: string, argsJson: string): void {
      var extra = {}
      try { extra = JSON.parse(argsJson || "{}") } catch (error) { extra = {} }
      root.browse(view, extra)
    }
    function status(): string { return JSON.stringify(root.status()) }
  }
}
