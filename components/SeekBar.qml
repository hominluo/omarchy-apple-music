import QtQuick
import qs.Commons
import qs.Ui

// The progress line under the player: filled to the play position, and a
// drag on it seeks. A drag only seeks on release, so the thumb does not
// fight the position the daemon keeps sending back.
Item {
  id: root

  property real position: 0        // seconds
  property real duration: 0        // seconds
  property color foreground: Color.menu.text
  property color accent: Color.accent
  property bool enabled: true

  signal seeked(real position)

  property bool dragging: false
  property real dragRatio: 0

  readonly property real ratio: {
    if (root.dragging) return root.dragRatio
    if (!root.duration || root.duration <= 0) return 0
    return Math.max(0, Math.min(1, root.position / root.duration))
  }

  implicitHeight: Style.space(14)

  Rectangle {
    id: track
    anchors.verticalCenter: parent.verticalCenter
    width: parent.width
    height: Math.max(2, Style.normalBorderWidth)
    radius: height / 2
    color: Util.alpha(root.foreground, 0.18)

    Rectangle {
      width: Math.round(track.width * root.ratio)
      height: parent.height
      radius: parent.radius
      color: root.accent
    }

    Rectangle {
      id: knob
      visible: hover.hovered || root.dragging
      width: Style.space(10)
      height: width
      radius: width / 2
      color: root.accent
      anchors.verticalCenter: parent.verticalCenter
      x: Math.round(track.width * root.ratio) - width / 2
    }
  }

  HoverHandler {
    id: hover
    cursorShape: root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    onHoveredChanged: if (!hovered && root.dragging) root.finishDrag()
  }

  MouseArea {
    anchors.fill: parent
    anchors.topMargin: -Style.space(6)
    anchors.bottomMargin: -Style.space(6)
    enabled: root.enabled
    preventStealing: true

    onPressed: function(mouse) {
      root.dragging = true
      root.dragRatio = Math.max(0, Math.min(1, mouse.x / track.width))
    }
    onPositionChanged: function(mouse) {
      if (!root.dragging) return
      root.dragRatio = Math.max(0, Math.min(1, mouse.x / track.width))
    }
    onReleased: root.finishDrag()
    onCanceled: root.finishDrag()
  }

  function finishDrag() {
    if (!root.dragging) return
    root.dragging = false
    if (root.duration > 0) root.seeked(root.dragRatio * root.duration)
  }
}
