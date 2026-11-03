import QtQuick
import Quickshell.Widgets
import qs.Commons
import qs.Ui

// A square cover with a glyph standing in until the artwork arrives.
//
// Decoded at display size, never at the file's own size: a grid of 1400px
// covers decoded at native size is a grid that costs hundreds of megabytes
// to scroll.
Item {
  id: root

  property string source: ""
  property real size: Style.space(64)
  property real radius: Style.cornerRadius > 0 ? Math.min(Style.spacing.md, Style.cornerRadius) : 0
  property color foreground: Color.menu.text
  property color accent: Color.accent
  property string glyph: "󰝚"
  property bool bordered: true

  readonly property bool ready: image.status === Image.Ready && root.source !== ""

  implicitWidth: size
  implicitHeight: size
  width: size
  height: size

  BorderSurface {
    anchors.fill: parent
    radius: root.radius
    color: Util.alpha(root.foreground, 0.07)
    borderSpec: root.bordered ? Border.controlSpec("normal", root.foreground, root.accent) : Border.none()
  }

  ClippingRectangle {
    id: clip
    anchors.fill: parent
    anchors.margins: root.bordered ? Math.max(1, Style.normalBorderWidth) : 0
    radius: Math.max(0, root.radius - anchors.margins)
    color: "transparent"

    Image {
      id: image
      anchors.fill: parent
      source: root.source
      asynchronous: true
      cache: true
      smooth: true
      fillMode: Image.PreserveAspectCrop
      sourceSize.width: Math.round(root.size * 2)
      sourceSize.height: Math.round(root.size * 2)
      visible: root.ready
      opacity: root.ready ? 1 : 0
      Behavior on opacity { NumberAnimation { duration: 160 } }
    }
  }

  OpticalGlyph {
    anchors.centerIn: parent
    width: Math.round(root.size * 0.42)
    height: width
    text: root.glyph
    fontFamily: Style.font.family
    fontSize: Math.round(root.size * 0.4)
    color: Util.alpha(root.foreground, 0.35)
    visible: !root.ready
  }
}
