import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "components"

// The Apple Music window: a launcher-style modal over the desktop.
//
// Declared as the plugin's `overlay`, so the host mounts it once and calls
// open(payloadJson) / close(). Same surface every summoned Omarchy window
// uses — full-screen layer-shell, dimmed scrim, centred card, exclusive
// keyboard focus — painted with the [menu] theme tokens.
//
// Layout: sidebar of views on the left (the web player's: Home, New, Radio,
// Search, the library, Up Next), the current view in the main pane, and the
// player across the bottom. Navigation is a stack so Escape and Backspace
// walk back before closing. Keys: Space play/pause, ←/→ previous and next,
// q the queue, l the lyrics, / search (the footer's tooltips name the same
// keys).
//
// Views only say what was picked (activateItem, songMenu); what a pick means
// (open a page, tune in a station, play a song) and the one song menu every
// list shares live here.
Item {
  id: root

  // Injected by the host.
  property string omarchyPath: ""
  property var shell: null
  property var manifest: null
  property var service: null

  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : "io.github.hominluo.apple-music"

  property bool opened: false
  property string view: "home"
  property var viewArgs: ({})
  property var history: []
  property string libraryKind: "songs"
  property bool editing: false

  // ---- theme -------------------------------------------------------------
  readonly property color background: Color.menu.background
  readonly property color foreground: Color.menu.text
  readonly property color border: Color.menu.border
  readonly property color scrim: Color.menu.scrim
  readonly property color selectedBackground: Color.menu.selectedBackground
  readonly property color selectedText: Color.menu.selectedText
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent
  readonly property color dim: Util.alpha(foreground, 0.6)
  readonly property color faint: Util.alpha(foreground, 0.4)
  readonly property var borderSpec: Border.surfaceSpec("menu", "border", border, Math.max(1, Style.space(2)))
  readonly property int cornerRadius: Style.cornerRadius
  readonly property string fontFamily: Style.font.menuFamily
  readonly property int contentMargin: Style.spacing.panelPadding
  readonly property int cardWidth: Math.min(Style.space(1180), Math.round(panel.width * 0.86))
  readonly property int cardHeight: Math.min(Style.space(780), Math.round(panel.height * 0.86))
  readonly property int sidebarWidth: Style.space(184)
  readonly property int footerHeight: Style.space(96)

  // ---- service shortcuts -------------------------------------------------
  readonly property bool connected: service ? service.connected : false
  readonly property string connection: service ? service.connection : "connecting"
  readonly property bool signedIn: service ? service.signedIn : false
  readonly property bool authExpired: service ? service.authExpired === true : false
  readonly property bool needsSignIn: !signedIn || authExpired
  readonly property bool hasToken: service ? service.hasToken === true : false
  readonly property string storefront: service ? String(service.storefront || "") : ""
  readonly property bool hasSong: service ? service.hasSong : false
  readonly property var player: service ? service.player : null
  readonly property var nowPlaying: service ? service.nowPlaying : null
  readonly property var queue: service ? service.queue : null
  readonly property bool isLive: service ? service.isLive === true : false

  // `header` rows are labels and `gap` rows space, neither is clickable.
  readonly property var sidebarItems: [
    { view: "home", label: "Home", glyph: "󰋜" },
    { view: "new", label: "New", glyph: "󰓒" },
    { view: "radio", label: "Radio", glyph: "󰐹" },
    { view: "search", label: "Search", glyph: "󰍉" },
    { header: "Library" },
    { view: "library", kind: "recently-added", label: "Recently Added", glyph: "󰅐" },
    { view: "library", kind: "artists", label: "Artists", glyph: "󰠃" },
    { view: "library", kind: "albums", label: "Albums", glyph: "󰀥" },
    { view: "library", kind: "songs", label: "Songs", glyph: "󰝚" },
    { view: "library", kind: "playlists", label: "Playlists", glyph: "󰲸" },
    { gap: true },
    { view: "queue", label: "Up Next", glyph: "󰐑" }
  ]

  readonly property var libraryTitles: ({
    "recently-added": "Recently Added",
    "artists": "Artists",
    "albums": "Albums",
    "songs": "Songs",
    "playlists": "Playlists"
  })

  function isSidebarActive(entry) {
    if (!entry.view || entry.view !== root.view) return false
    if (entry.view === "library") return root.libraryKind === entry.kind
    return true
  }

  // The title a page gave itself (a genre's name, a See All room's), for
  // pages opened without one.
  readonly property string pageTitle: viewLoader.item && viewLoader.item.pageTitle !== undefined
                                      ? String(viewLoader.item.pageTitle || "") : ""
  readonly property string argsTitle: root.viewArgs && root.viewArgs.title ? String(root.viewArgs.title) : ""

  readonly property string viewTitle: {
    switch (root.view) {
      case "home": return "Home"
      case "new": return "New"
      case "radio": return "Radio"
      case "charts": return root.pageTitle !== "" ? root.pageTitle : "Charts"
      case "search": return "Search"
      case "library": return root.libraryTitles[root.libraryKind] || "Library"
      case "queue": return "Up Next"
      case "lyrics": return "Lyrics"
      case "album": return root.argsTitle !== "" ? root.argsTitle : "Album"
      case "playlist": return root.argsTitle !== "" ? root.argsTitle : "Playlist"
      case "artist": return root.argsTitle !== "" ? root.argsTitle : "Artist"
      case "grouping": case "curator": case "room":
        return root.argsTitle !== "" ? root.argsTitle : (root.pageTitle !== "" ? root.pageTitle : "Apple Music")
      default: return "Apple Music"
    }
  }

  // Every shelf page (Home, New, Radio, a genre, a show, Charts) is one
  // view; an unknown view lands there too and says it is not available.
  readonly property string viewSource: {
    switch (root.view) {
      case "search": return Qt.resolvedUrl("views/SearchView.qml")
      case "library": return Qt.resolvedUrl("views/LibraryView.qml")
      case "queue": return Qt.resolvedUrl("views/QueueView.qml")
      case "lyrics": return Qt.resolvedUrl("views/LyricsView.qml")
      case "room": return Qt.resolvedUrl("views/RoomView.qml")
      case "album": case "playlist": case "artist": return Qt.resolvedUrl("views/DetailView.qml")
      default: return Qt.resolvedUrl("views/ShelfView.qml")
    }
  }

  // Views that cannot stand without their arguments.
  function needsArgs(view) {
    return ["album", "playlist", "artist", "grouping", "curator", "room"].indexOf(view) >= 0
  }

  // Shelf pages already seen this session, by page: walking back shows one
  // at once and refreshes it underneath.
  property var pageCache: ({})

  // ---- host contract -----------------------------------------------------
  function open(payloadJson) {
    var payload = {}
    try { payload = JSON.parse(String(payloadJson || "{}")) || {} } catch (e) { payload = {} }
    var target = payload.view ? String(payload.view) : ""
    // Opening the window is the user asking for the player: retry a daemon
    // that is backing off now rather than in a minute.
    if (root.service && typeof root.service.wake === "function") root.service.wake()
    // Every opening starts a fresh back stack, so Escape on the first view
    // closes the window instead of walking through an earlier session.
    root.history = []
    root.opened = true
    root.editing = false
    root.closeSongMenu()
    if (target !== "") root.navigate(target, payload, false)
    else if (root.needsArgs(root.view)) {
      // A page from the last session cannot be trusted to still hold its
      // place; start from Home.
      root.navigate("home", {}, false)
    } else {
      var current = viewLoader.item
      if (current && typeof current.activated === "function") Qt.callLater(function() {
        if (root.opened && viewLoader.item === current) current.activated()
      })
    }
    Qt.callLater(root.refocus)
  }

  function close() {
    root.opened = false
    root.editing = false
    root.history = []
    root.closeSongMenu()
  }

  // Closing from inside (scrim, Escape on the first view) also tells the
  // host, so its open state does not go on saying the window is up.
  function dismiss() {
    root.close()
    if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
  }

  function sameView(target, args) {
    if (target !== root.view) return false
    var a = args || {}
    var current = root.viewArgs || {}
    if (target === "library") return String(a.kind || root.libraryKind) === root.libraryKind
    return String(a.id || "") === String(current.id || "")
  }

  function navigate(target, args, push) {
    // `history` is reassigned, never pushed to: an in-place push emits no
    // change, and the back button's visibility binding would never update.
    if (push !== false && root.opened && !root.sameView(target, args)) {
      var stack = root.history.concat([{ view: root.view, args: root.viewArgs, kind: root.libraryKind }])
      root.history = stack.slice(-30)
    }
    // libraryKind first: changing `view` reloads the Loader, and onViewLoaded
    // copies the kind into the new item.
    if (target === "library" && args && args.kind) root.libraryKind = String(args.kind)
    root.viewArgs = args || {}
    root.view = target
    root.editing = false
    root.closeSongMenu()
    Qt.callLater(root.refocus)
  }

  function back() {
    if (root.songMenuOpen) { root.closeSongMenu(); return }
    if (root.history.length === 0) { root.dismiss(); return }
    var stack = root.history.slice()
    var entry = stack.pop()
    root.history = stack
    root.libraryKind = entry.kind || "songs"
    root.viewArgs = entry.args || {}
    root.view = entry.view
    root.editing = false
    Qt.callLater(root.refocus)
  }

  // ---- footer links ------------------------------------------------------
  // The daemon resolves these ids for the current track (see `_lookup`); the
  // footer only offers the link when it has one.
  readonly property string nowAlbumId: root.nowPlaying ? String(root.nowPlaying.albumId || "") : ""
  readonly property string nowArtistId: root.nowPlaying ? String(root.nowPlaying.artistId || "") : ""

  function openNowAlbum() {
    if (root.nowAlbumId === "") return
    root.navigate("album", {
      id: root.nowAlbumId,
      library: root.nowPlaying.albumLibrary === true,
      title: String(root.nowPlaying.album || "")
    })
  }

  function openNowArtist() {
    if (root.nowArtistId === "") return
    root.navigate("artist", {
      id: root.nowArtistId,
      library: root.nowPlaying.artistLibrary === true,
      title: String(root.nowPlaying.artist || "")
    })
  }

  function errorText(result, fallback) {
    return result && result.message ? String(result.message) : fallback
  }

  function showNotice(text, urgent) {
    root.noticeText = String(text || "")
    root.noticeUrgent = urgent === true
    noticeTimer.restart()
  }

  // ---- what a pick means -------------------------------------------------
  // Songs are played by the view that listed them (it knows the queue they
  // belong to); everything else comes here.
  function activate(entry) {
    if (!entry) return
    var type = String(entry.type || "")
    var title = String(entry.title || "")
    switch (type) {
      case "video":
        // Listed by Apple, but this is an audio player.
        return
      case "station":
        // No page of its own: a station starts playing.
        if (!root.service) return
        root.service.playStation(entry, function(ok, result) {
          if (!ok) root.showNotice(root.errorText(result, "Could not tune in to " + (title !== "" ? title : "the station")), true)
        })
        return
      case "curator":
        root.navigate("curator", { id: String(entry.id || ""), title: title, kind: String(entry.kind || "") }, true)
        return
      case "link":
        root.followLink(entry)
        return
      case "song":
        if (root.service) root.service.play([entry], 0, { title: title, artwork: String(entry.artwork || "") })
        return
      case "album": case "playlist": case "artist":
        root.navigate(type, { id: String(entry.id || ""), library: entry.library === true, title: title }, true)
        return
    }
  }

  // A genre, a See All room, Charts: the page the link names, titled by
  // the link until the page says otherwise.
  function followLink(entry) {
    var target = entry && entry.target ? entry.target : {}
    if (!target.view) return
    var args = {}
    var given = target.args || {}
    for (var key in given) args[key] = given[key]
    if (!args.title && entry.title) args.title = String(entry.title)
    root.navigate(String(target.view), args, true)
  }

  // ---- the song menu -----------------------------------------------------
  // One menu for every list of songs, opened by a row's "⋯" or a
  // right-click: Play Next, Play Later, Create Station, Favorite, Add to
  // Library, Go to Album, Go to Artist.
  property var menuSong: null
  readonly property bool songMenuOpen: root.menuSong !== null
  // The song's rating, asked for as the menu opens; null until it answers.
  property var menuRating: null
  property int menuToken: 0
  property int menuCursor: -1
  // Where it was asked for, in the card's coordinates: the point under it,
  // and the top of what was clicked, for opening upwards near the bottom.
  property real menuX: 0
  property real menuY: 0
  property real menuTop: 0

  // A station, a favourite and the library all want the catalogue id; a
  // library song has one only when the daemon matched it to the catalogue.
  function songCatalogId(song) {
    if (!song) return ""
    if (song.catalogId) return String(song.catalogId)
    return song.library === true ? "" : String(song.id || "")
  }

  function songRatingId(song) {
    var id = root.songCatalogId(song)
    return id !== "" ? id : (song ? String(song.id || "") : "")
  }

  readonly property var menuActions: {
    var song = root.menuSong
    if (!song) return []
    var catalogId = root.songCatalogId(song)
    var list = [
      { action: "next", label: "Play Next", glyph: "󰒭" },
      { action: "later", label: "Play Later", glyph: "󰐑" }
    ]
    if (catalogId !== "") list.push({ action: "station", label: "Create Station", glyph: "󰐹" })
    if (root.songRatingId(song) !== "")
      list.push(root.menuRating === 1
                ? { action: "favorite", label: "Remove Favorite", glyph: "󰋑" }
                : { action: "favorite", label: "Favorite", glyph: "󰋕" })
    if (song.library !== true && catalogId !== "") list.push({ action: "add", label: "Add to Library", glyph: "󰐕" })
    if (song.albumId) list.push({ action: "album", label: "Go to Album", glyph: "󰀥" })
    if (song.artistId) list.push({ action: "artist", label: "Go to Artist", glyph: "󰠃" })
    return list
  }

  function openSongMenu(song, source, x, y) {
    if (!song || !source) return
    var point = source.mapToItem(menuLayer, x, y)
    var top = source.mapToItem(menuLayer, x, 0)
    var token = ++root.menuToken
    root.menuSong = song
    root.menuRating = null
    root.menuCursor = -1
    root.menuX = point.x
    root.menuY = point.y
    root.menuTop = top.y
    // The menu's keys (arrows, Enter, Escape) go through the key catcher,
    // even when the search field had the focus.
    keyCatcher.forceActiveFocus()
    var ratingId = root.songRatingId(song)
    if (root.service && ratingId !== "") root.service.rating("song", ratingId, function(ok, result) {
      if (token !== root.menuToken || !ok) return
      root.menuRating = result ? Number(result.value) || 0 : 0
    })
  }

  function closeSongMenu() {
    if (root.menuSong === null) return
    root.menuSong = null
    root.menuToken++
    Qt.callLater(root.refocus)
  }

  function runSongMenu(action) {
    var song = root.menuSong
    var rating = root.menuRating
    root.closeSongMenu()
    if (!song) return
    var title = String(song.title || "")
    switch (action) {
      case "album":
        root.navigate("album", { id: String(song.albumId), library: song.albumLibrary === true, title: String(song.album || "") }, true)
        return
      case "artist":
        root.navigate("artist", { id: String(song.artistId), library: song.artistLibrary === true, title: String(song.artist || "") }, true)
        return
    }
    if (!root.service) return
    var favorite = rating === 1 ? 0 : 1
    switch (action) {
      case "next": case "later":
        root.service.enqueue([song], action, function(ok, result) {
          if (!ok) root.showNotice(root.errorText(result, "Could not add to Up Next"), true)
          else root.showNotice((action === "next" ? "Playing next: " : "Added to Up Next: ") + title, false)
        })
        return
      case "station":
        root.service.createStation(root.songCatalogId(song), "song", function(ok, result) {
          if (!ok) root.showNotice(root.errorText(result, "Could not start the station"), true)
        })
        return
      case "favorite":
        root.service.rate("song", root.songRatingId(song), favorite, function(ok, result) {
          if (!ok) root.showNotice(root.errorText(result, "Could not change the favourite"), true)
          else root.showNotice(favorite === 1 ? "Added to Favorites: " + title : "Removed from Favorites: " + title, false)
        })
        return
      case "add":
        root.service.addToLibrary("song", root.songCatalogId(song), function(ok, result) {
          if (!ok) root.showNotice(root.errorText(result, "Could not add to your library"), true)
          else root.showNotice("Added to your library: " + title, false)
        })
        return
    }
  }

  // Lyrics are a song's: not a station's, and not nothing's.
  readonly property bool canShowLyrics: root.hasSong && !(root.service && root.service.isStation)

  function refocus() {
    if (!root.opened) return
    var item = viewLoader.item
    if (item && item.editing === true) { root.editing = true; return }
    keyCatcher.forceActiveFocus()
  }

  function onViewLoaded() {
    var item = viewLoader.item
    if (!item) return
    item.service = root.service
    // Bound, not copied: the Loader keeps the same item when only the
    // arguments change (album to album), so a copied `args` would go stale.
    item.args = Qt.binding(function() { return root.viewArgs })
    if ("kind" in item) item.kind = Qt.binding(function() {
      if (root.view !== "library") return root.view
      if (root.viewArgs && root.viewArgs.kind) return String(root.viewArgs.kind)
      return root.libraryKind
    })
    if (item.open) item.open.connect(function(target, args) {
      var payload = {}
      for (var key in (args || {})) payload[key] = args[key]
      payload.view = target
      if (target === "album" || target === "playlist" || target === "artist") {
        payload.title = args && args.title ? args.title : ""
      }
      root.navigate(target, payload, true)
    })
    if (item.playTracks) item.playTracks.connect(function(tracks, index, context) {
      if (root.service) root.service.play(tracks, index, context)
    })
    if (item.jump) item.jump.connect(function(index) {
      if (root.service) root.service.request("jump", { index: index })
    })
    if (item.activateItem) item.activateItem.connect(function(entry) { root.activate(entry) })
    if (item.songMenu) item.songMenu.connect(function(song, source, x, y) { root.openSongMenu(song, source, x, y) })
    if (item.notify) item.notify.connect(function(message, urgent) { root.showNotice(message, urgent) })
    if ("pageCache" in item) item.pageCache = root.pageCache
    if (typeof item.activated === "function") item.activated()
    root.editing = item.editing === true
  }

  onServiceChanged: {
    if (!viewLoader.item) return
    var hadService = viewLoader.item.service !== null
    viewLoader.item.service = root.service
    // The overlay can be created before the host has injected the service.
    // The view loaded without one and its first load went nowhere, so ask it
    // to load again now that there is something to talk to.
    if (!hadService && root.service && typeof viewLoader.item.activated === "function")
      Qt.callLater(viewLoader.item.activated)
  }

  Connections {
    target: root.service
    enabled: root.service !== null
    function onNotice(payload) {
      if (!payload) return
      root.showNotice(payload.message, payload.level === "error")
    }
  }

  property string noticeText: ""
  property bool noticeUrgent: false
  Timer { id: noticeTimer; interval: 5000; onTriggered: root.noticeText = "" }

  // ---- window ------------------------------------------------------------
  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-apple-music"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.opened ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: root.cardHeight
      radius: root.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: 0

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        id: keyCatcher
        anchors.fill: parent
        focus: true

        Keys.onEscapePressed: root.back()
        Keys.onBackPressed: root.back()
        Keys.onSpacePressed: if (!root.editing && root.service) root.service.toggle()
        // The search field keeps its own arrows and letters while it edits.
        Keys.onPressed: function(event) {
          // An open song menu takes every key: arrows move in it, Enter
          // picks, Escape closes it (and only it).
          if (root.songMenuOpen) {
            var count = root.menuActions.length
            if (event.key === Qt.Key_Down || event.key === Qt.Key_Tab) {
              root.menuCursor = count > 0 ? (root.menuCursor + 1) % count : -1
            } else if (event.key === Qt.Key_Up || event.key === Qt.Key_Backtab) {
              root.menuCursor = count > 0 ? (root.menuCursor <= 0 ? count - 1 : root.menuCursor - 1) : -1
            } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
              if (root.menuCursor >= 0 && root.menuCursor < count) root.runSongMenu(root.menuActions[root.menuCursor].action)
            } else {
              root.closeSongMenu()
            }
            event.accepted = true
            return
          }
          if (root.editing) return
          if (event.modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)) return
          if (event.key === Qt.Key_Slash) {
            root.navigate("search", {}, true)
            event.accepted = true
          } else if (event.key === Qt.Key_Left) {
            if (root.service) root.service.previous()
            event.accepted = true
          } else if (event.key === Qt.Key_Right) {
            if (root.service) root.service.next()
            event.accepted = true
          } else if (event.key === Qt.Key_Q) {
            root.navigate("queue", {}, true)
            event.accepted = true
          } else if (event.key === Qt.Key_L) {
            if (root.canShowLyrics) root.navigate("lyrics", {}, true)
            event.accepted = true
          }
        }

        Column {
          anchors.fill: parent
          anchors.margins: root.contentMargin
          spacing: Style.spacing.md

          // ---- header ----
          Item {
            width: parent.width
            height: Style.space(34)

            Row {
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.md

              Rectangle {
                visible: root.history.length > 0
                width: Style.space(32)
                height: width
                radius: width / 2
                color: backArea.hovered ? Util.alpha(root.foreground, 0.10) : "transparent"

                Text {
                  anchors.centerIn: parent
                  textFormat: Text.PlainText
                  text: "󰁍"
                  color: root.foreground
                  font.family: Style.font.family
                  font.pixelSize: Style.font.title
                  renderType: Text.NativeRendering
                }

                HoverHandler { id: backArea; cursorShape: Qt.PointingHandCursor }
                TapHandler { onTapped: root.back() }
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: root.viewTitle
                color: root.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.subtitle
                font.weight: Font.DemiBold
                renderType: Text.NativeRendering
                elide: Text.ElideRight
                width: Math.min(implicitWidth, card.width - Style.space(420))
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                visible: !root.connected
                textFormat: Text.PlainText
                text: root.connection === "unavailable"
                      ? "player unavailable" + (root.service && root.service.currentError
                                                ? ": " + String(root.service.currentError) : "")
                      : (root.connection === "starting" ? "starting…" : "connecting…")
                color: root.connection === "unavailable" ? root.urgent : root.faint
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
                width: Math.min(implicitWidth, Style.space(420))
              }
            }

            Row {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.md

              Text {
                anchors.verticalCenter: parent.verticalCenter
                visible: root.noticeText !== ""
                textFormat: Text.PlainText
                text: root.noticeText
                color: root.noticeUrgent ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
                width: Math.min(implicitWidth, Style.space(360))
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                visible: root.authExpired
                textFormat: Text.PlainText
                text: "Apple Music signed you out"
                color: root.urgent
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Rectangle {
                visible: root.needsSignIn
                width: signinLabel.implicitWidth + Style.spacing.lg * 2
                height: Style.space(28)
                radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, Style.spacing.sm) : height / 2
                color: signinArea.hovered ? Util.alpha(root.accent, 0.28) : Util.alpha(root.accent, 0.18)
                border.color: Util.alpha(root.accent, 0.5)
                border.width: Math.max(1, Style.normalBorderWidth)

                Text {
                  id: signinLabel
                  anchors.centerIn: parent
                  textFormat: Text.PlainText
                  text: root.authExpired ? "Sign in again" : "Sign in to Apple Music"
                  color: root.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.weight: Font.DemiBold
                  renderType: Text.NativeRendering
                }

                HoverHandler { id: signinArea; cursorShape: Qt.PointingHandCursor }
                TapHandler {
                  onTapped: {
                    if (!root.service) return
                    root.service.signIn(function(ok, result) {
                      // The daemon's reason (no browser found, a window
                      // already open) is the useful part of a failure.
                      root.noticeText = ok ? "Finish signing in in the window that opened"
                                           : (result && result.message ? String(result.message) : "Could not open a sign-in window")
                      root.noticeUrgent = !ok
                      noticeTimer.restart()
                    })
                  }
                }
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                visible: !root.needsSignIn && root.service && root.service.auth && !!root.service.auth.name
                textFormat: Text.PlainText
                text: root.service && root.service.auth ? String(root.service.auth.name || "") : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              // Sign out asks once more on the same button: it also clears the
              // sign-in profile's session, so a stray click costs a real login.
              // It stays next to "Sign in again" when Apple has turned the
              // token down: that token is still stored and re-checked at every
              // start until it is signed out.
              Rectangle {
                id: signoutButton
                visible: root.hasToken
                property bool confirming: false
                width: signoutLabel.implicitWidth + Style.spacing.md * 2
                height: Style.space(28)
                radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, Style.spacing.sm) : height / 2
                color: signoutArea.hovered || confirming ? Util.alpha(root.foreground, 0.10) : "transparent"
                border.color: confirming ? Util.alpha(root.urgent, 0.6) : Util.alpha(root.foreground, 0.18)
                border.width: Math.max(1, Style.normalBorderWidth)

                Text {
                  id: signoutLabel
                  anchors.centerIn: parent
                  textFormat: Text.PlainText
                  text: signoutButton.confirming ? "Sign out?" : "Sign out"
                  color: signoutButton.confirming ? root.urgent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  renderType: Text.NativeRendering
                }

                Timer { id: signoutConfirmTimer; interval: 3000; onTriggered: signoutButton.confirming = false }

                HoverHandler { id: signoutArea; cursorShape: Qt.PointingHandCursor }
                TapHandler {
                  onTapped: {
                    if (!root.service) return
                    if (!signoutButton.confirming) {
                      signoutButton.confirming = true
                      signoutConfirmTimer.restart()
                      return
                    }
                    signoutButton.confirming = false
                    signoutConfirmTimer.stop()
                    root.service.signOut(function(ok, result) {
                      root.noticeText = ok ? "Signed out of Apple Music"
                                           : (result && result.message ? String(result.message) : "Could not sign out")
                      root.noticeUrgent = !ok
                      noticeTimer.restart()
                    })
                  }
                }
              }
            }
          }

          // ---- body ----
          Row {
            width: parent.width
            height: parent.height - Style.space(34) - root.footerHeight - Style.spacing.md * 2
            spacing: Style.spacing.lg

            // sidebar: scrolls only on a screen too short for it
            Flickable {
              width: root.sidebarWidth
              height: parent.height
              contentHeight: sidebarColumn.implicitHeight
              clip: true
              interactive: contentHeight > height
              boundsBehavior: Flickable.StopAtBounds

              Column {
                id: sidebarColumn
                width: root.sidebarWidth
                spacing: Style.spacing.xxs

                Repeater {
                  model: root.sidebarItems

                  Rectangle {
                    id: sidebarRow
                    readonly property bool isHeader: !!modelData.header
                    readonly property bool isGap: modelData.gap === true
                    readonly property bool isEntry: !isHeader && !isGap
                    width: parent.width
                    height: isHeader ? Style.space(30) : (isGap ? Style.spacing.md : Style.space(36))
                    radius: Math.min(Style.cornerRadius, Style.spacing.sm)
                    color: !isEntry ? "transparent"
                         : root.isSidebarActive(modelData) ? Util.alpha(root.accent, 0.16)
                         : (sidebarRow.hovered ? Util.alpha(root.foreground, 0.06) : "transparent")

                    readonly property bool hovered: sidebarHover.hovered

                    // A section label, as the web player heads its library.
                    Text {
                      visible: sidebarRow.isHeader
                      anchors.left: parent.left
                      anchors.leftMargin: Style.spacing.sm
                      anchors.bottom: parent.bottom
                      anchors.bottomMargin: Style.spacing.xs
                      textFormat: Text.PlainText
                      text: sidebarRow.isHeader ? String(modelData.header) : ""
                      color: root.faint
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.weight: Font.DemiBold
                      renderType: Text.NativeRendering
                    }

                    Row {
                      visible: sidebarRow.isEntry
                      anchors.left: parent.left
                      anchors.leftMargin: Style.spacing.sm
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.spacing.sm

                      Text {
                        width: Style.space(18)
                        horizontalAlignment: Text.AlignHCenter
                        anchors.verticalCenter: parent.verticalCenter
                        textFormat: Text.PlainText
                        text: String(modelData.glyph || "")
                        color: root.isSidebarActive(modelData) ? root.accent : root.dim
                        font.family: Style.font.family
                        font.pixelSize: Style.font.icon
                        renderType: Text.NativeRendering
                      }

                      Text {
                        anchors.verticalCenter: parent.verticalCenter
                        textFormat: Text.PlainText
                        text: String(modelData.label || "")
                        color: root.isSidebarActive(modelData) ? root.accent : root.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.body
                        font.weight: root.isSidebarActive(modelData) ? Font.DemiBold : Font.Normal
                        renderType: Text.NativeRendering
                      }
                    }

                    HoverHandler {
                      id: sidebarHover
                      enabled: sidebarRow.isEntry
                      cursorShape: Qt.PointingHandCursor
                    }
                    TapHandler {
                      enabled: sidebarRow.isEntry
                      // navigate() sets libraryKind itself, after it has pushed
                      // the current kind and compared against it.
                      onTapped: root.navigate(String(modelData.view), modelData.kind ? { kind: modelData.kind } : {}, true)
                    }
                  }
                }

                Item { width: 1; height: Style.spacing.sm }

                // The catalogue follows the account's storefront, not the
                // settings default, so only a signed-in account has one to show.
                Text {
                  width: parent.width
                  visible: root.signedIn && root.storefront !== ""
                  textFormat: Text.PlainText
                  text: "Storefront: " + root.storefront.toUpperCase()
                  color: root.faint
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  renderType: Text.NativeRendering
                }
              }
            }

            // divider
            Rectangle {
              width: Math.max(1, Style.normalBorderWidth)
              height: parent.height
              color: Util.alpha(root.foreground, 0.10)
            }

            // view
            Item {
              width: parent.width - root.sidebarWidth - Style.spacing.lg * 2 - Math.max(1, Style.normalBorderWidth)
              height: parent.height

              Loader {
                id: viewLoader
                anchors.fill: parent
                source: root.viewSource
                onLoaded: root.onViewLoaded()
              }
            }
          }

          // ---- footer ----
          Rectangle {
            width: parent.width
            height: root.footerHeight
            radius: Math.min(Style.cornerRadius, Style.spacing.md)
            color: Util.alpha(root.foreground, 0.04)
            border.color: Util.alpha(root.foreground, 0.08)
            border.width: Math.max(1, Style.normalBorderWidth)

            RowLayout {
              anchors.fill: parent
              anchors.margins: Style.spacing.md
              spacing: Style.spacing.lg

              // Left and right share one width, so the transport in the
              // middle is the centre of the card, not the leftover space.
              RowLayout {
                Layout.preferredWidth: Style.space(236)
                Layout.maximumWidth: Style.space(236)
                Layout.alignment: Qt.AlignVCenter
                spacing: Style.spacing.sm

                Cover {
                  Layout.alignment: Qt.AlignVCenter
                  size: Style.space(52)
                  source: root.hasSong ? String(root.nowPlaying.artwork || "") : ""
                  glyph: root.service && root.service.isStation ? "󰐹" : "󰝚"
                  foreground: root.foreground
                  accent: root.accent

                  // The cover leads to the album it belongs to.
                  MouseArea {
                    id: coverLink
                    anchors.fill: parent
                    enabled: root.nowAlbumId !== ""
                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                    hoverEnabled: enabled

                    Rectangle {
                      anchors.fill: parent
                      radius: coverLink.parent.radius
                      color: root.accent
                      opacity: coverLink.containsMouse ? 0.18 : 0
                      visible: opacity > 0
                      Behavior on opacity { NumberAnimation { duration: 120 } }
                    }

                    onClicked: root.openNowAlbum()
                  }
                }

                Column {
                  Layout.fillWidth: true
                  Layout.alignment: Qt.AlignVCenter
                  spacing: 1

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: root.hasSong ? String(root.nowPlaying.title || "")
                          : (root.connection === "unavailable" ? "Player unavailable" : "Nothing playing")
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.weight: Font.DemiBold
                    renderType: Text.NativeRendering
                    elide: Text.ElideRight
                  }

                  Item {
                    width: parent.width
                    height: artistText.height

                    Text {
                      id: artistText
                      width: parent.width
                      textFormat: Text.PlainText
                      text: root.hasSong ? String(root.nowPlaying.artist || "") : "Pick something from Home, Radio or Search"
                      color: artistLink.containsMouse ? root.accent : root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.underline: artistLink.containsMouse
                      renderType: Text.NativeRendering
                      elide: Text.ElideRight

                      Behavior on color { ColorAnimation { duration: 120 } }
                    }

                    // The artist line leads to the artist.
                    MouseArea {
                      id: artistLink
                      anchors.fill: parent
                      enabled: root.nowArtistId !== ""
                      cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                      hoverEnabled: enabled
                      onClicked: root.openNowArtist()
                    }
                  }
                }
              }

              // transport + progress
              Column {
                Layout.fillWidth: true
                Layout.minimumWidth: Style.space(280)
                Layout.alignment: Qt.AlignVCenter
                spacing: Style.spacing.xs

                TransportRow {
                  anchors.horizontalCenter: parent.horizontalCenter
                  service: root.service
                  foreground: root.foreground
                  accent: root.accent
                  background: root.background
                  fontFamily: root.fontFamily
                  // The window's own keys (keyCatcher above).
                  keyHints: ({ previous: "←", next: "→", playPause: "Space" })
                  large: true
                }

                RowLayout {
                  width: parent.width
                  spacing: Style.spacing.sm

                  Text {
                    Layout.alignment: Qt.AlignVCenter
                    visible: !root.isLive
                    textFormat: Text.PlainText
                    text: root.formatTime(root.service ? root.service.position : 0)
                    color: root.faint
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    renderType: Text.NativeRendering
                  }

                  // A live stream has no length and nothing to seek to: the
                  // tag and what is on air stand where the bar would be.
                  Item {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    implicitHeight: liveRow.implicitHeight
                    visible: root.isLive && (!root.service || root.service.prepareMessage === "")

                    Row {
                      id: liveRow
                      anchors.horizontalCenter: parent.horizontalCenter
                      width: Math.min(implicitWidth, parent.width)
                      spacing: Style.spacing.sm

                      LiveBadge {
                        anchors.verticalCenter: parent.verticalCenter
                        accent: root.accent
                        textColor: root.background
                        fontFamily: root.fontFamily
                      }

                      Text {
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.min(implicitWidth, liveRow.parent.width - Style.space(60))
                        textFormat: Text.PlainText
                        text: root.hasSong ? String(root.nowPlaying.artist || "") : ""
                        color: root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        elide: Text.ElideRight
                        renderType: Text.NativeRendering
                      }
                    }
                  }

                  SeekBar {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    visible: (!root.service || root.service.prepareMessage === "") && !root.isLive
                    foreground: root.foreground
                    accent: root.accent
                    position: root.service ? root.service.position : 0
                    duration: root.service ? root.service.duration : 0
                    enabled: root.service ? root.service.canSeek === true : false
                    onSeeked: function(position) { if (root.service) root.service.seek(position) }
                  }

                  // The picked track is still being fetched and decrypted:
                  // the old file's progress would be a lie, so say so.
                  Text {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    visible: !!root.service && root.service.prepareMessage !== ""
                    horizontalAlignment: Text.AlignHCenter
                    textFormat: Text.PlainText
                    text: root.service ? String(root.service.prepareMessage || "") : ""
                    color: root.service && root.service.prepareFailed ? root.urgent : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    elide: Text.ElideRight
                    renderType: Text.NativeRendering
                  }

                  Text {
                    Layout.alignment: Qt.AlignVCenter
                    visible: !root.isLive
                    textFormat: Text.PlainText
                    text: root.formatTime(root.service ? root.service.duration : 0)
                    color: root.faint
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    renderType: Text.NativeRendering
                  }
                }
              }

              // volume + queue
              Column {
                Layout.preferredWidth: Style.space(236)
                Layout.maximumWidth: Style.space(236)
                Layout.alignment: Qt.AlignVCenter
                spacing: Style.spacing.sm

                RowLayout {
                  width: parent.width
                  spacing: Style.spacing.xs

                  Text {
                    textFormat: Text.PlainText
                    text: root.service && root.service.volume === 0 ? "󰖁" : "󰕾"
                    color: root.dim
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    renderType: Text.NativeRendering
                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                    // Same mute as the dropdown's: the daemon keeps the level
                    // to come back to.
                    TapHandler { onTapped: if (root.service) root.service.mute() }
                  }

                  SeekBar {
                    Layout.fillWidth: true
                    foreground: root.foreground
                    accent: root.accent
                    position: root.service ? root.service.volume : 0
                    duration: 100
                    onSeeked: function(position) { if (root.service) root.service.setVolume(Math.round(position)) }
                  }

                  Text {
                    Layout.preferredWidth: Style.space(30)
                    horizontalAlignment: Text.AlignRight
                    textFormat: Text.PlainText
                    text: root.service ? root.service.volume + "%" : ""
                    color: root.faint
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    renderType: Text.NativeRendering
                  }
                }

                Item {
                  width: parent.width
                  implicitHeight: queueButton.height

                  Row {
                    anchors.right: parent.right
                    spacing: Style.spacing.sm

                    // Lyrics of the song on air; a station has none.
                    Button {
                      id: lyricsButton
                      height: Style.space(26)
                      enabled: root.canShowLyrics
                      foreground: enabled ? root.foreground : Util.alpha(root.foreground, 0.35)
                      fontFamily: root.fontFamily
                      fontSize: Style.font.bodySmall
                      iconText: "󰉾"
                      iconSize: Style.font.bodySmall
                      text: "Lyrics"
                      tooltipText: "Lyrics of the song that is playing  (l)"
                      bordered: true
                      selected: root.view === "lyrics"
                      onClicked: root.navigate("lyrics", {}, true)
                    }

                    Button {
                      id: queueButton
                      height: Style.space(26)
                      foreground: root.foreground
                      fontFamily: root.fontFamily
                      fontSize: Style.font.bodySmall
                      iconText: "󰐑"
                      iconSize: Style.font.bodySmall
                      text: "Up Next" + (root.service && root.service.queueItems.length > 0
                                          ? "  " + String(root.service.queueItems.length) : "")
                      tooltipText: "The playback queue  (q)"
                      bordered: true
                      onClicked: root.navigate("queue", {}, true)
                    }
                  }
                }
              }
            }
          }
        }
      }

      // ---- song menu ----
      // Above everything in the card. A click or a wheel anywhere outside
      // the menu closes it, and that click does nothing else: a stray click
      // after a menu must not also start a song.
      Item {
        id: menuLayer
        anchors.fill: parent
        visible: root.songMenuOpen
        z: 10

        MouseArea {
          anchors.fill: parent
          acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
          onClicked: root.closeSongMenu()
          onWheel: function(wheel) { root.closeSongMenu(); wheel.accepted = true }
        }

        BorderSurface {
          id: menuBox
          readonly property real margin: Style.spacing.md
          width: Style.space(208)
          height: menuColumn.implicitHeight + contentTopInset + contentBottomInset
          // Under the click, or above what was clicked when there is no
          // room below; never past the card's edge.
          x: Math.max(margin, Math.min(root.menuX, menuLayer.width - width - margin))
          y: {
            var below = root.menuY
            var top = below + height > menuLayer.height - margin ? root.menuTop - height : below
            return Math.max(margin, Math.min(top, menuLayer.height - height - margin))
          }
          radius: Style.cornerRadius
          color: Color.popups.background
          borderSpec: Border.localOrSurfaceSpec("popups", "border", Color.popups.border, Color.popups.border, Math.max(1, Style.normalBorderWidth))
          padding: Style.spacing.xs

          // Swallows clicks between the rows, so they do not close the menu.
          MouseArea { anchors.fill: parent; acceptedButtons: Qt.LeftButton | Qt.RightButton }

          Column {
            id: menuColumn
            x: menuBox.contentLeftInset
            y: menuBox.contentTopInset
            width: menuBox.width - menuBox.contentLeftInset - menuBox.contentRightInset
            spacing: Style.spacing.xxs

            Text {
              width: parent.width
              leftPadding: Style.spacing.controlPaddingX
              rightPadding: Style.spacing.controlPaddingX
              topPadding: Style.spacing.xs
              bottomPadding: Style.spacing.xs
              textFormat: Text.PlainText
              text: root.menuSong ? String(root.menuSong.title || "") : ""
              color: Util.alpha(Color.popups.text, 0.55)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
              renderType: Text.NativeRendering
            }

            Repeater {
              model: root.menuActions

              Button {
                required property var modelData
                required property int index
                width: menuColumn.width
                height: Style.space(30)
                leftAlign: true
                foreground: Color.popups.text
                accent: root.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.body
                iconText: String(modelData.glyph || "")
                iconSize: Style.font.body
                text: String(modelData.label || "")
                hasCursor: root.menuCursor === index
                onHovered: function(isHovered) { if (isHovered) root.menuCursor = index }
                onClicked: root.runSongMenu(String(modelData.action))
              }
            }
          }
        }
      }
    }
  }

  function formatTime(seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0))
    var minutes = Math.floor(total / 60)
    var rest = total % 60
    return minutes + ":" + (rest < 10 ? "0" : "") + rest
  }
}
