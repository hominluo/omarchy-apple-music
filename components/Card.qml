import QtQuick
import qs.Commons
import qs.Ui

// A cover tile for an album, playlist or artist.
Item {
  id: root

  property var item: ({})
  property real tileSize: Style.space(150)
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal activated()

  readonly property bool hovered: hover.hovered
  readonly property string title: item ? String(item.title || "") : ""
  readonly property string subtitle: item ? String(item.artist || item.description || "") : ""

  implicitWidth: tileSize
  implicitHeight: tileSize + label.implicitHeight + Style.spacing.sm
  width: tileSize
  height: implicitHeight

  HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
  TapHandler { onTapped: root.activated() }

  Cover {
    id: cover
    anchors.top: parent.top
    anchors.left: parent.left
    anchors.right: parent.right
    height: root.tileSize
    source: root.item ? String(root.item.artwork || "") : ""
    size: root.tileSize
    glyph: root.item && root.item.type === "artist" ? "󰠃" : "󰝚"
  }

  Column {
    id: label
    anchors.top: cover.bottom
    anchors.topMargin: Style.spacing.sm
    anchors.left: parent.left
    anchors.right: parent.right
    spacing: 1

    Text {
      width: parent.width
      textFormat: Text.PlainText
      text: root.title
      color: root.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      font.weight: root.hovered ? Font.DemiBold : Font.Normal
      renderType: Text.NativeRendering
      elide: Text.ElideRight
    }

    Text {
      width: parent.width
      visible: root.subtitle !== ""
      textFormat: Text.PlainText
      text: root.subtitle
      color: Util.alpha(root.foreground, 0.55)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      renderType: Text.NativeRendering
      elide: Text.ElideRight
    }
  }
}
