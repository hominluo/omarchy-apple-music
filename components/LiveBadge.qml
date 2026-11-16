import QtQuick
import qs.Commons
import qs.Ui

// The "LIVE" tag of a live radio stream (Apple Music 1, a broadcaster).
//
// Shown where a seek bar or a clock would be, and on the cover of a live
// station's card: a live stream has no length and nothing behind "now", so
// the tag is the honest answer to "where am I in this".
Rectangle {
  id: root

  property color accent: Color.accent
  property color textColor: Color.menu.background
  property string fontFamily: Style.font.family
  property string text: "LIVE"
  property real fontSize: Style.font.caption

  implicitWidth: label.implicitWidth + Style.spacing.md * 2
  implicitHeight: label.implicitHeight + Style.spacing.xxs * 2
  width: implicitWidth
  height: implicitHeight
  radius: Math.min(Style.cornerRadius, height / 2)
  color: root.accent

  Text {
    id: label
    anchors.centerIn: parent
    textFormat: Text.PlainText
    text: root.text
    color: root.textColor
    font.family: root.fontFamily
    font.pixelSize: root.fontSize
    font.weight: Font.Bold
    font.letterSpacing: 0.6
    renderType: Text.NativeRendering
  }
}
