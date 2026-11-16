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
// Layout: sidebar of views on the left, the current view in the main pane,
// and the player across the bottom. Navigation is a stack so Escape and
// Backspace walk back before closing. Keys: Space play/pause, ←/→ previous
// and next, q the queue, / search (the footer's tooltips name the same keys).
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

  readonly property var sidebarItems: [
    { view: "home", label: "Browse", glyph: "󰋜" },
    { view: "search", label: "Search", glyph: "󰍉" },
    { view: "library", kind: "songs", label: "Songs", glyph: "󰝚" },
    { view: "library", kind: "albums", label: "Albums", glyph: "󰀥" },
    { view: "library", kind: "playlists", label: "Playlists", glyph: "󰐑" },
    { view: "library", kind: "artists", label: "Artists", glyph: "󰠃" },
    { view: "queue", label: "Up Next", glyph: "󰐑" }
  ]

  function isSidebarActive(entry) {
    if (entry.view !== root.view) return false
    if (entry.view === "library") return root.libraryKind === entry.kind
    return true
  }

  readonly property string viewTitle: {
    switch (root.view) {
      case "home": return "Browse"
      case "search": return "Search"
      case "library": return root.libraryKind.charAt(0).toUpperCase() + root.libraryKind.slice(1)
      case "queue": return "Up Next"
      case "album": return root.viewArgs && root.viewArgs.title ? String(root.viewArgs.title) : "Album"
      case "playlist": return root.viewArgs && root.viewArgs.title ? String(root.viewArgs.title) : "Playlist"
      case "artist": return root.viewArgs && root.viewArgs.title ? String(root.viewArgs.title) : "Artist"
      default: return "Apple Music"
    }
  }

  readonly property string viewSource: {
    switch (root.view) {
      case "search": return Qt.resolvedUrl("views/SearchView.qml")
      case "library": return Qt.resolvedUrl("views/LibraryView.qml")
      case "queue": return Qt.resolvedUrl("views/QueueView.qml")
      case "album": case "playlist": case "artist": return Qt.resolvedUrl("views/DetailView.qml")
      default: return Qt.resolvedUrl("views/HomeView.qml")
    }
  }

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
    if (target !== "") root.navigate(target, payload, false)
    else if (root.view === "album" || root.view === "playlist" || root.view === "artist") {
      // Detail views cannot stand without their arguments.
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
    Qt.callLater(root.refocus)
  }

  function back() {
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
      root.noticeText = String(payload.message || "")
      root.noticeUrgent = payload.level === "error"
      noticeTimer.restart()
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

            // sidebar
            Column {
              width: root.sidebarWidth
              spacing: Style.spacing.xxs

              Repeater {
                model: root.sidebarItems

                Rectangle {
                  id: sidebarRow
                  width: parent.width
                  height: Style.space(36)
                  radius: Math.min(Style.cornerRadius, Style.spacing.sm)
                  color: root.isSidebarActive(modelData) ? Util.alpha(root.accent, 0.16)
                       : (sidebarRow.hovered ? Util.alpha(root.foreground, 0.06) : "transparent")

                  readonly property bool hovered: sidebarHover.hovered

                  Row {
                    anchors.left: parent.left
                    anchors.leftMargin: Style.spacing.sm
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Style.spacing.sm

                    Text {
                      width: Style.space(18)
                      horizontalAlignment: Text.AlignHCenter
                      anchors.verticalCenter: parent.verticalCenter
                      textFormat: Text.PlainText
                      text: String(modelData.glyph)
                      color: root.isSidebarActive(modelData) ? root.accent : root.dim
                      font.family: Style.font.family
                      font.pixelSize: Style.font.icon
                      renderType: Text.NativeRendering
                    }

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      textFormat: Text.PlainText
                      text: String(modelData.label)
                      color: root.isSidebarActive(modelData) ? root.accent : root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      font.weight: root.isSidebarActive(modelData) ? Font.DemiBold : Font.Normal
                      renderType: Text.NativeRendering
                    }
                  }

                  HoverHandler { id: sidebarHover; cursorShape: Qt.PointingHandCursor }
                  TapHandler {
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
                      text: root.hasSong ? String(root.nowPlaying.artist || "") : "Pick something from Browse or Search"
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
                    textFormat: Text.PlainText
                    text: root.formatTime(root.service ? root.service.position : 0)
                    color: root.faint
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    renderType: Text.NativeRendering
                  }

                  SeekBar {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    visible: !root.service || root.service.prepareMessage === ""
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

                  Button {
                    id: queueButton
                    anchors.right: parent.right
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
  }

  function formatTime(seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0))
    var minutes = Math.floor(total / 60)
    var rest = total % 60
    return minutes + ":" + (rest < 10 ? "0" : "") + rest
  }
}
