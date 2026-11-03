import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "components"

// The now-playing dropdown under the bar icon.
//
// A qs.Ui.Panel owned by BarWidget.qml, which injects `bar`, `settings`,
// `anchorItem`, `hostWidget` and `service`. It exists so the common actions —
// pause, skip, seek, volume, a glance at Up Next — never require opening the
// full window. "Open Apple Music" is one row away when they do.
//
// Keyboard and mouse share one cursor (the CursorSurface contract): hovering
// a row moves the cursor rather than painting its own highlight.
Panel {
  id: root
  moduleName: "io.github.hominluo.apple-music"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var service: null

  readonly property var barIdentity: hostWidget || root
  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color dim: Util.alpha(foreground, 0.6)
  readonly property color faint: Util.alpha(foreground, 0.4)
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // ---- service shortcuts -------------------------------------------------
  readonly property var player: service ? service.player : null
  readonly property bool connected: service ? service.connected : false
  readonly property bool hasSong: service ? service.hasSong : false
  readonly property bool playing: service ? service.playing : false
  readonly property string title: service ? service.title : ""
  readonly property string artist: service ? service.artist : ""
  readonly property string albumName: service ? service.album : ""
  readonly property string artwork: service ? service.artwork : ""
  readonly property real position: service ? service.position : 0
  readonly property real duration: service ? service.duration : 0
  readonly property int volume: service && service.player ? Math.round(Number(service.player.volume) || 0) : 0
  readonly property bool shuffleOn: service && service.queue ? service.queue.shuffle === true : false
  readonly property string repeatMode: service && service.queue ? String(service.queue.repeat || "off") : "off"

  // Five rows is enough to answer "what is next"; the window has the rest.
  readonly property int queueLimit: 5
  readonly property var queueRows: {
    if (!root.opened || !root.service) return []
    var items = root.service.queueItems
    var start = Math.max(0, root.service.queueIndex + 1)
    return items.slice(start, start + root.queueLimit)
  }
  readonly property int queueRemaining: {
    if (!root.service) return 0
    var start = Math.max(0, root.service.queueIndex + 1)
    return Math.max(0, root.service.queueItems.length - start)
  }

  property string notice: ""
  property string noticeLevel: "info"
  property int volumeBeforeMute: 100

  implicitWidth: 0
  implicitHeight: 0

  // ---- cursor model ------------------------------------------------------
  property bool cursorActive: false
  property string focusSection: "hero"
  property int selectedIndex: 0

  readonly property var transportActions: ["shuffle", "previous", "playPause", "next", "repeat"]
  readonly property var footerActions: ["open", "queue"]

  readonly property var visibleSections: {
    var list = []
    if (root.hasSong) list.push("hero", "seek", "transport", "volume")
    else list.push("hero")
    if (root.queueRows.length > 0) list.push("queue")
    list.push("footer")
    return list
  }

  function sectionCount(section) {
    if (section === "transport") return transportActions.length
    if (section === "footer") return footerActions.length
    if (section === "queue") return queueRows.length
    return 1
  }

  function sectionIsRow(section) {
    return section === "transport" || section === "footer" || section === "queue"
  }

  function setCursor(section, index) {
    cursorActive = true
    focusSection = section
    selectedIndex = index
  }

  function moveCursor(dy) {
    var sections = visibleSections
    var index = sections.indexOf(focusSection)
    if (index < 0) { focusSection = sections[0]; selectedIndex = 0; return }
    if (focusSection === "queue") {
      var next = selectedIndex + dy
      if (next >= 0 && next < queueRows.length) { selectedIndex = next; return }
    }
    var target = index + dy
    if (target < 0 || target >= sections.length) return
    focusSection = sections[target]
    selectedIndex = focusSection === "queue" && dy < 0 ? queueRows.length - 1 : 0
    if (sectionIsRow(focusSection)) selectedIndex = Math.min(selectedIndex, sectionCount(focusSection) - 1)
  }

  function moveCursorH(dx) {
    if (focusSection === "transport" || focusSection === "footer") {
      selectedIndex = Math.max(0, Math.min(sectionCount(focusSection) - 1, selectedIndex + dx))
      return
    }
    if (!service) return
    if (focusSection === "volume") root.setVolume(root.volume + dx * 5)
    else if (focusSection === "seek") root.seekBy(dx * 10)
  }

  function activateCursor() {
    if (!service) return
    switch (focusSection) {
      case "hero": root.browse(""); return
      case "seek": service.toggle(); return
      case "transport": root.runTransport(transportActions[selectedIndex]); return
      case "volume": root.toggleMute(); return
      case "queue":
        if (queueRows[selectedIndex]) service.jump(queueRows[selectedIndex].index !== undefined
          ? queueRows[selectedIndex].index
          : Math.max(0, service.queueIndex + 1 + selectedIndex))
        return
      case "footer": root.runFooter(footerActions[selectedIndex]); return
    }
  }

  function runTransport(action) {
    if (!service || !action) return
    switch (action) {
      case "shuffle": service.setShuffle(!root.shuffleOn); break
      case "previous": service.previous(); break
      case "playPause": service.toggle(); break
      case "next": service.next(); break
      case "repeat": service.cycleRepeat(); break
    }
  }

  function runFooter(action) {
    if (action === "open") root.browse("")
    else if (action === "queue") root.browse("queue")
  }

  function seekBy(seconds) {
    if (!service) return
    var target = Math.max(0, Math.min(root.duration || 0, root.position + seconds))
    service.seek(target)
  }

  function setVolume(value) {
    if (!service) return
    service.setVolume(Math.max(0, Math.min(130, Math.round(value))))
  }

  function toggleMute() {
    if (!service) return
    if (root.volume > 0) {
      root.volumeBeforeMute = root.volume
      root.setVolume(0)
    } else {
      root.setVolume(root.volumeBeforeMute > 0 ? root.volumeBeforeMute : 60)
    }
  }

  function browse(view) {
    root.close()
    if (service) service.browse(view || "")
  }

  function showNotice(text, level) {
    notice = String(text || "")
    noticeLevel = level || "info"
    noticeTimer.restart()
  }

  // Tab / Shift+Tab move to the neighbouring bar panel, the host way.
  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function") {
      if (bar.switchPanelFrom(root.barIdentity, direction)) return
    }
    root.close()
  }

  function ensureVisible(item) {
    if (!item || !flick) return
    var maxY = Math.max(0, flick.contentHeight - flick.height)
    if (maxY <= 0) { flick.contentY = 0; return }
    var margin = Style.space(6)
    var point = item.mapToItem(flick.contentItem, 0, 0)
    var top = point.y
    var bottom = top + item.height
    if (top < flick.contentY + margin) flick.contentY = Math.max(0, top - margin)
    else if (bottom > flick.contentY + flick.height - margin) flick.contentY = Math.min(maxY, bottom + margin - flick.height)
  }

  onOpenedChanged: {
    if (opened) {
      cursorActive = false
      focusSection = "hero"
      selectedIndex = 0
      Qt.callLater(function() { if (flick) flick.contentY = 0 })
    }
  }

  onVisibleSectionsChanged: {
    if (visibleSections.indexOf(focusSection) < 0) { focusSection = visibleSections[0]; selectedIndex = 0 }
    if (selectedIndex >= sectionCount(focusSection)) selectedIndex = Math.max(0, sectionCount(focusSection) - 1)
  }

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onNotice(payload) {
      if (!payload || !root.opened) return
      root.showNotice(payload.message, payload.level)
    }
  }

  Timer {
    id: noticeTimer
    interval: 4000
    onTriggered: root.notice = ""
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else root.moveCursorH(dx)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
                            else if (root.service) root.service.toggle()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (!root.service) return
        switch (t) {
          case "m": case "M": root.toggleMute(); break
          case "n": case "N": root.service.next(); break
          case "p": case "P": root.service.previous(); break
          case "s": case "S": root.service.setShuffle(!root.shuffleOn); break
          case "r": case "R": root.service.cycleRepeat(); break
          case "o": case "O": root.browse(""); break
          case "q": case "Q": root.browse("queue"); break
          case ",": root.seekBy(-10); break
          case ".": root.seekBy(10); break
        }
      }

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width
          spacing: Style.space(10)

          // ---------- Hero ----------
          CursorSurface {
            id: hero
            width: parent.width
            implicitHeight: heroRow.implicitHeight + Style.spacing.sm * 2
            hasCursor: root.cursorActive && root.focusSection === "hero"
            foreground: root.foreground
            accent: root.accent
            onHasCursorChanged: if (hasCursor) root.ensureVisible(hero)

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onEntered: root.setCursor("hero", 0)
              onClicked: root.browse("")
            }

            RowLayout {
              id: heroRow
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.margins: Style.spacing.sm
              spacing: Style.space(12)

              Cover {
                size: Style.space(58)
                source: root.artwork
                glyph: "󰝚"
                foreground: root.foreground
                accent: root.accent
                Layout.alignment: Qt.AlignVCenter
              }

              Column {
                Layout.fillWidth: true
                spacing: Style.spacing.xxs

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  text: root.hasSong ? root.title
                        : (root.connected ? "Nothing playing" : "Starting Apple Music…")
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.subtitle
                  font.bold: root.hasSong
                  wrapMode: Text.WordWrap
                  maximumLineCount: 2
                  elide: Text.ElideRight
                  renderType: Text.NativeRendering
                }

                Text {
                  width: parent.width
                  visible: text !== ""
                  textFormat: Text.PlainText
                  text: root.hasSong
                        ? (root.artist + (root.albumName !== "" ? "  ·  " + root.albumName : ""))
                        : "Open the window to browse your library"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                  maximumLineCount: 1
                  renderType: Text.NativeRendering
                }
              }
            }
          }

          // ---------- Seek ----------
          CursorSurface {
            id: seekRow
            visible: root.hasSong
            width: parent.width
            implicitHeight: seekLayout.implicitHeight + Style.spacing.sm * 2
            hasCursor: root.cursorActive && root.focusSection === "seek"
            outline: true
            foreground: root.foreground
            accent: root.accent
            onHasCursorChanged: if (hasCursor) root.ensureVisible(seekRow)

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              acceptedButtons: Qt.NoButton
              onEntered: root.setCursor("seek", 0)
            }

            RowLayout {
              id: seekLayout
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(10)
              anchors.rightMargin: Style.space(10)
              spacing: Style.spacing.sm

              Text {
                textFormat: Text.PlainText
                text: root.formatTime(root.position)
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                renderType: Text.NativeRendering
              }

              SeekBar {
                Layout.fillWidth: true
                foreground: root.foreground
                accent: root.accent
                position: root.position
                duration: root.duration
                onSeeked: function(seconds) { if (root.service) root.service.seek(seconds) }
              }

              Text {
                textFormat: Text.PlainText
                text: root.formatTime(root.duration)
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                renderType: Text.NativeRendering
              }
            }
          }

          // ---------- Transport ----------
          Item {
            id: transport
            visible: root.hasSong
            width: parent.width
            implicitHeight: transportRow.implicitHeight

            TransportRow {
              id: transportRow
              anchors.horizontalCenter: parent.horizontalCenter
              service: root.service
              foreground: root.foreground
              accent: root.accent
              background: Color.menu.background
              fontFamily: root.fontFamily
              hasCursor: root.cursorActive && root.focusSection === "transport"
              cursorIndex: root.selectedIndex
              onHovered: function(index) { root.setCursor("transport", index) }
            }
          }

          // ---------- Volume ----------
          CursorSurface {
            id: volumeRow
            visible: root.hasSong
            width: parent.width
            implicitHeight: Style.space(32)
            hasCursor: root.cursorActive && root.focusSection === "volume"
            foreground: root.foreground
            accent: root.accent
            onHasCursorChanged: if (hasCursor) root.ensureVisible(volumeRow)

            MouseArea {
              anchors.fill: parent
              hoverEnabled: true
              acceptedButtons: Qt.NoButton
              onEntered: root.setCursor("volume", 0)
            }

            RowLayout {
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(10)
              anchors.rightMargin: Style.space(10)
              spacing: Style.space(8)

              Text {
                textFormat: Text.PlainText
                text: root.volume === 0 ? "󰖁" : "󰕾"
                color: root.dim
                font.family: Style.font.family
                font.pixelSize: Style.font.icon
                renderType: Text.NativeRendering
                HoverHandler { cursorShape: Qt.PointingHandCursor }
                TapHandler { onTapped: root.toggleMute() }
              }

              PanelSlider {
                Layout.fillWidth: true
                bar: root.bar
                value: root.volume
                minimum: 0
                maximum: 100
                step: 1
                integer: true
                trackColor: Util.alpha(root.foreground, 0.18)
                fillColor: root.accent
                knobColor: root.accent
                onReleased: function(value) { root.setVolume(value) }
              }

              Text {
                Layout.preferredWidth: Style.space(34)
                horizontalAlignment: Text.AlignRight
                textFormat: Text.PlainText
                text: root.volume + "%"
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                renderType: Text.NativeRendering
              }
            }
          }

          // ---------- Up Next ----------
          Column {
            visible: root.queueRows.length > 0
            width: parent.width
            spacing: Style.space(4)

            PanelSeparator { foreground: root.foreground }

            Item {
              width: parent.width
              implicitHeight: upNextHeader.implicitHeight

              PanelSectionHeader {
                id: upNextHeader
                text: "UP NEXT"
                foreground: root.foreground
                fontFamily: root.fontFamily
              }

              Text {
                anchors.right: parent.right
                anchors.baseline: upNextHeader.baseline
                textFormat: Text.PlainText
                text: root.queueRemaining === 1 ? "1 track" : root.queueRemaining + " tracks"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                renderType: Text.NativeRendering
              }
            }

            Repeater {
              model: root.queueRows

              TrackRow {
                id: queueRow
                width: parent.width
                song: modelData
                number: Math.max(0, root.service ? root.service.queueIndex : 0) + index + 2
                foreground: root.foreground
                accent: root.accent
                onActivated: {
                  if (!root.service) return
                  var start = Math.max(0, root.service.queueIndex + 1)
                  root.service.jump(start + index)
                }
              }
            }

            Button {
              visible: root.queueRemaining > root.queueRows.length
              anchors.horizontalCenter: parent.horizontalCenter
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              text: "+" + (root.queueRemaining - root.queueRows.length) + " more in Up Next"
              onClicked: root.browse("queue")
            }
          }

          // ---------- Footer ----------
          Column {
            width: parent.width
            spacing: Style.space(6)

            PanelSeparator { foreground: root.foreground }

            RowLayout {
              width: parent.width
              spacing: Style.space(6)

              Button {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.minimumWidth: 0
                foreground: root.foreground
                fontFamily: root.fontFamily
                hasCursor: root.cursorActive && root.focusSection === "footer" && root.selectedIndex === 0
                bordered: true
                iconText: "󰋜"
                text: "Open Apple Music"
                tooltipText: "The full window  (o)"
                onHovered: function(isHovered) { if (isHovered) root.setCursor("footer", 0) }
                onClicked: root.browse("")
              }

              Button {
                visible: root.queueRemaining > 0
                foreground: root.foreground
                fontFamily: root.fontFamily
                hasCursor: root.cursorActive && root.focusSection === "footer" && root.selectedIndex === 1
                bordered: true
                iconText: "󰐑"
                text: "Up Next"
                tooltipText: "The full queue  (q)"
                onHovered: function(isHovered) { if (isHovered) root.setCursor("footer", 1) }
                onClicked: root.browse("queue")
              }
            }

            Text {
              width: parent.width
              visible: text !== ""
              textFormat: Text.PlainText
              text: {
                if (root.notice !== "") return root.notice
                if (root.service && !root.connected) return "Connecting to the player…"
                if (root.service && root.service.auth && !root.service.auth.signedIn) return "Sign in from the window to start listening."
                return ""
              }
              color: root.noticeLevel === "error" ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              maximumLineCount: 2
              elide: Text.ElideRight
              renderType: Text.NativeRendering
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
