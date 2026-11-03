import QtQuick
import qs.Commons
import qs.Ui
import "../components"

// The playback queue, in play order, with the current track marked.
Item {
  id: root

  property var service: null
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal playTracks(var tracks, int index, var context)
  signal jump(int index)

  readonly property var items: service ? service.queueItems : []
  readonly property int currentIndex: service ? service.queueIndex : -1
  readonly property string contextTitle: service && service.queue && service.queue.context
                                           ? String(service.queue.context.title || "") : ""

  Column {
    width: parent.width
    spacing: Style.spacing.sm

    Row {
      id: header
      width: parent.width
      spacing: Style.spacing.md

      Text {
        textFormat: Text.PlainText
        text: root.contextTitle !== "" ? root.contextTitle : "Queue"
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.title
        font.weight: Font.DemiBold
        renderType: Text.NativeRendering
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: root.items.length === 1 ? "1 track" : root.items.length + " tracks"
        color: Util.alpha(root.foreground, 0.5)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }

    Text {
      visible: root.items.length === 0
      textFormat: Text.PlainText
      text: "Nothing queued yet."
      color: Util.alpha(root.foreground, 0.5)
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }

    // Queues can be long (a whole playlist), so only the visible rows are
    // built; the current track is kept in view when the queue moves on.
    ListView {
      id: list
      width: parent.width
      height: Math.max(0, root.height - header.height - Style.spacing.sm)
      visible: root.items.length > 0
      clip: true
      model: root.items
      spacing: Style.spacing.xs
      boundsBehavior: Flickable.StopAtBounds
      reuseItems: true

      function reveal() {
        if (root.currentIndex >= 0) list.positionViewAtIndex(root.currentIndex, ListView.Center)
      }

      delegate: TrackRow {
        required property int index
        required property var modelData

        width: list.width
        song: modelData
        number: index + 1
        current: index === root.currentIndex
        playing: index === root.currentIndex && root.service ? root.service.playing : false
        stage: root.service && root.service.build && root.service.build[modelData.id]
               ? String(root.service.build[modelData.id].stage || "") : ""
        foreground: root.foreground
        accent: root.accent
        onActivated: {
          if (index === root.currentIndex && root.service) root.service.toggle()
          else root.jump(index)
        }
      }

      onCountChanged: Qt.callLater(list.reveal)
    }
  }

  function activated() { Qt.callLater(list.reveal) }
}
