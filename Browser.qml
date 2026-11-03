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
// Backspace walk back before closing.
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
  readonly property int footerHeight: Style.space(92)

  // ---- service shortcuts -------------------------------------------------
  readonly property bool connected: service ? service.connected : false
  readonly property bool signedIn: service ? service.signedIn : false
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
  }

  function dismiss() { root.close() }

  function navigate(target, args, push) {
    if (push !== false && root.opened) root.history.push({ view: root.view, args: root.viewArgs, kind: root.libraryKind })
    // libraryKind first: changing `view` reloads the Loader, and onViewLoaded
    // copies the kind into the new item.
    if (target === "library" && args && args.kind) root.libraryKind = String(args.kind)
    root.viewArgs = args || {}
    root.view = target
    root.editing = false
    Qt.callLater(root.refocus)
  }

  function back() {
    if (root.history.length === 0) { root.close(); return }
    var entry = root.history.pop()
    root.libraryKind = entry.kind || "songs"
    root.viewArgs = entry.args || {}
    root.view = entry.view
    root.editing = false
    Qt.callLater(root.refocus)
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

  onServiceChanged: if (viewLoader.item) viewLoader.item.service = root.service

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
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Slash && !root.editing) {
            root.navigate("search", {}, true)
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
                width: Style.space(28)
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
                text: root.connection === "starting" ? "starting…" : "connecting…"
                color: root.faint
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
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

              Rectangle {
                visible: !root.signedIn
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
                  text: "Sign in to Apple Music"
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
                      root.noticeText = ok ? "Finish signing in in the window that opened" : "Could not open a sign-in window"
                      root.noticeUrgent = !ok
                      noticeTimer.restart()
                    })
                  }
                }
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                visible: root.signedIn && root.service && root.service.auth && root.service.auth.name !== ""
                textFormat: Text.PlainText
                text: root.service && root.service.auth ? String(root.service.auth.name) : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
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
                  height: Style.space(32)
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
                    onTapped: {
                      if (modelData.view === "library") root.libraryKind = String(modelData.kind)
                      root.navigate(String(modelData.view), modelData.kind ? { kind: modelData.kind } : {}, true)
                    }
                  }
                }
              }

              Item { width: 1; height: Style.spacing.sm }

              Text {
                width: parent.width
                visible: root.service && root.service.settings && root.service.settings.storefront
                textFormat: Text.PlainText
                text: root.service && root.service.settings
                      ? "Storefront: " + String(root.service.settings.storefront || "").toUpperCase() : ""
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
              spacing: Style.spacing.md

              Cover {
                Layout.alignment: Qt.AlignVCenter
                size: Style.space(56)
                source: root.hasSong ? String(root.nowPlaying.artwork || "") : ""
                foreground: root.foreground
                accent: root.accent
              }

              // Title block: shrinks before the transport does, so a narrow
              // window never pushes the transport past the card edge.
              Column {
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: Style.space(190)
                Layout.minimumWidth: Style.space(110)
                Layout.maximumWidth: Style.space(260)
                spacing: 1

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  text: root.hasSong ? String(root.nowPlaying.title || "") : "Nothing playing"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  font.weight: Font.DemiBold
                  renderType: Text.NativeRendering
                  elide: Text.ElideRight
                }

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  text: root.hasSong ? String(root.nowPlaying.artist || "") : "Pick something from Browse or Search"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  renderType: Text.NativeRendering
                  elide: Text.ElideRight
                }
              }

              // transport + progress
              Column {
                Layout.fillWidth: true
                Layout.minimumWidth: Style.space(200)
                Layout.alignment: Qt.AlignVCenter
                spacing: Style.spacing.xs

                Row {
                  anchors.horizontalCenter: parent.horizontalCenter
                  spacing: Style.spacing.lg

                  Text {
                    anchors.verticalCenter: parent.verticalCenter
                    textFormat: Text.PlainText
                    text: "󰒔"
                    color: root.hasSong ? root.foreground : root.faint
                    font.family: Style.font.family
                    font.pixelSize: Style.font.title
                    renderType: Text.NativeRendering
                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                    TapHandler { onTapped: if (root.service) root.service.previous() }
                  }

                  Rectangle {
                    width: Style.space(34)
                    height: width
                    radius: width / 2
                    color: root.hasSong ? root.accent : Util.alpha(root.foreground, 0.15)

                    Text {
                      anchors.centerIn: parent
                      textFormat: Text.PlainText
                      text: root.service && root.service.playing ? "󰏤" : "󰐊"
                      color: root.hasSong ? root.background : root.faint
                      font.family: Style.font.family
                      font.pixelSize: Style.font.title
                      renderType: Text.NativeRendering
                    }

                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                    TapHandler { onTapped: if (root.service) root.service.toggle() }
                  }

                  Text {
                    anchors.verticalCenter: parent.verticalCenter
                    textFormat: Text.PlainText
                    text: "󰒭"
                    color: root.hasSong ? root.foreground : root.faint
                    font.family: Style.font.family
                    font.pixelSize: Style.font.title
                    renderType: Text.NativeRendering
                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                    TapHandler { onTapped: if (root.service) root.service.next() }
                  }
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
                    foreground: root.foreground
                    accent: root.accent
                    position: root.service ? root.service.position : 0
                    duration: root.service ? root.service.duration : 0
                    enabled: root.hasSong
                    onSeeked: function(position) { if (root.service) root.service.seek(position) }
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

              // shuffle / repeat / volume
              Column {
                Layout.alignment: Qt.AlignVCenter
                Layout.preferredWidth: Style.space(150)
                Layout.minimumWidth: Style.space(96)
                Layout.maximumWidth: Style.space(150)
                spacing: Style.spacing.sm

                Row {
                  spacing: Style.spacing.md
                  anchors.horizontalCenter: parent.horizontalCenter

                  Text {
                    textFormat: Text.PlainText
                    text: "󰒝"
                    color: root.queue && root.queue.shuffle ? root.accent : root.faint
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    renderType: Text.NativeRendering
                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                    TapHandler { onTapped: if (root.service) root.service.setShuffle(!(root.queue && root.queue.shuffle)) }
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: root.queue && root.queue.repeat === "one" ? "󰑘" : "󰑖"
                    color: root.queue && root.queue.repeat !== "off" ? root.accent : root.faint
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    renderType: Text.NativeRendering
                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                    TapHandler { onTapped: if (root.service) root.service.cycleRepeat() }
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: "󰕾"
                    color: root.dim
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    renderType: Text.NativeRendering
                  }
                }

                SeekBar {
                  width: parent.width
                  foreground: root.foreground
                  accent: root.accent
                  position: root.service ? Number(root.service.player.volume) || 0 : 0
                  duration: 100
                  onSeeked: function(position) { if (root.service) root.service.setVolume(Math.round(position)) }
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
