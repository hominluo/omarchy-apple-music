import QtQuick
import qs.Commons
import qs.Ui

// One track in a list: number (or the playing marker), title, artist, length.
//
// With `menuEnabled`, hovering shows a "⋯" that asks for the song menu (Play
// Next, Favorite, Go to Album…), and so does a right-click anywhere on the
// row. The menu itself is one popup owned by the window, not one per row.
Item {
  id: root

  property var song: ({})
  property int number: 0
  property bool current: false
  property bool playing: false
  property bool showArtist: true
  property bool showAlbum: false
  property string stage: ""        // queued | resolving | downloading | decrypting | failed
  property color foreground: Color.menu.text
  property color accent: Color.accent
  property color selectedBackground: Color.menu.selectedBackground
  property bool menuEnabled: false

  signal activated()
  // `source` and x/y say where the menu should open, in source's coordinates.
  signal menuRequested(var source, real x, real y)

  readonly property bool hovered: hover.hovered
  readonly property string title: song ? String(song.title || "") : ""
  readonly property string artist: song ? String(song.artist || "") : ""
  readonly property string albumName: song ? String(song.album || "") : ""
  readonly property int durationMs: song ? Number(song.durationMs) || 0 : 0
  readonly property bool building: stage === "queued" || stage === "resolving"
                                   || stage === "downloading" || stage === "decrypting"
  readonly property bool failed: stage === "failed"

  implicitHeight: Style.space(44)
  // Apple lists some tracks it will not stream here (region, a pulled
  // release); they stay in the list so the numbering matches the album.
  opacity: root.song && root.song.playable === false ? 0.5 : 1

  Rectangle {
    anchors.fill: parent
    radius: Math.min(Style.cornerRadius, Style.spacing.sm)
    color: root.current ? Util.alpha(root.accent, 0.14)
         : (root.hovered ? Util.alpha(root.foreground, 0.06) : "transparent")
    Behavior on color { ColorAnimation { duration: 90 } }
  }

  HoverHandler { id: hover }
  TapHandler { onTapped: root.activated() }
  TapHandler {
    acceptedButtons: Qt.RightButton
    enabled: root.menuEnabled
    onTapped: function(eventPoint) { root.menuRequested(root, eventPoint.position.x, eventPoint.position.y) }
  }

  Item {
    id: marker
    width: Style.space(26)
    height: parent.height
    anchors.left: parent.left
    anchors.leftMargin: Style.spacing.sm

    Text {
      anchors.centerIn: parent
      visible: !root.building
      textFormat: Text.PlainText
      text: root.playing ? "󰏤" : (root.current ? "󰐊" : String(root.number))
      color: root.current ? root.accent : Util.alpha(root.foreground, 0.55)
      font.family: Style.font.family
      font.pixelSize: root.current ? Style.font.title : Style.font.body
      renderType: Text.NativeRendering
    }

    Text {
      anchors.centerIn: parent
      visible: root.building
      text: "󰑐"
      color: root.accent
      font.family: Style.font.family
      font.pixelSize: Style.font.title
      renderType: Text.NativeRendering
      RotationAnimator on rotation {
        from: 0; to: 360; duration: 900
        loops: Animation.Infinite
        running: root.building
      }
    }
  }

  Column {
    anchors.left: marker.right
    anchors.leftMargin: Style.spacing.xs
    anchors.right: more.left
    anchors.rightMargin: Style.spacing.sm
    anchors.verticalCenter: parent.verticalCenter
    spacing: 1

    Text {
      width: parent.width
      textFormat: Text.PlainText
      text: root.title
      color: root.current ? root.accent : root.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      renderType: Text.NativeRendering
      elide: Text.ElideRight
    }

    Text {
      width: parent.width
      visible: root.showArtist && root.artist !== ""
      textFormat: Text.PlainText
      text: root.showAlbum && root.albumName !== "" ? root.artist + " — " + root.albumName : root.artist
      color: Util.alpha(root.foreground, 0.55)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      renderType: Text.NativeRendering
      elide: Text.ElideRight
    }
  }

  // A MouseArea, not a TapHandler: it takes the press, so the row's own
  // TapHandler does not also start the song.
  Item {
    id: more
    anchors.right: duration.left
    anchors.rightMargin: root.menuEnabled ? Style.spacing.xs : 0
    anchors.verticalCenter: parent.verticalCenter
    width: root.menuEnabled ? Style.space(26) : 0
    height: Style.space(26)
    visible: root.menuEnabled

    Rectangle {
      anchors.fill: parent
      radius: width / 2
      color: moreArea.containsMouse ? Util.alpha(root.foreground, 0.12) : "transparent"
    }

    Text {
      anchors.centerIn: parent
      visible: root.hovered || moreArea.containsMouse
      textFormat: Text.PlainText
      text: "󰇘"
      color: moreArea.containsMouse ? root.accent : Util.alpha(root.foreground, 0.7)
      font.family: Style.font.family
      font.pixelSize: Style.font.title
      renderType: Text.NativeRendering
    }

    MouseArea {
      id: moreArea
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.menuRequested(more, 0, more.height)
    }
  }

  Text {
    id: duration
    anchors.right: parent.right
    anchors.rightMargin: Style.spacing.sm
    anchors.verticalCenter: parent.verticalCenter
    textFormat: Text.PlainText
    text: root.failed ? "unavailable" : formatTime(root.durationMs)
    color: root.failed ? Color.urgent : Util.alpha(root.foreground, 0.45)
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
    renderType: Text.NativeRendering
  }

  function formatTime(ms) {
    var total = Math.round(Number(ms || 0) / 1000)
    var minutes = Math.floor(total / 60)
    var seconds = total % 60
    return minutes + ":" + (seconds < 10 ? "0" : "") + seconds
  }
}
