import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// The bar cell: Apple Music, a music glyph and the current track.
//
// State lives in Service.qml; the bar widgets only read it, so every monitor
// shows the same thing. Left click opens the browser, middle click is the
// play/pause everyone expects from a media cell.
BarWidget {
  id: root
  moduleName: "io.github.hominluo.apple-music"

  property var service: null

  function bindService() {
    if (root.service || !root.bar || !root.bar.shell || typeof root.bar.shell.serviceFor !== "function") return
    var found = root.bar.shell.serviceFor(root.moduleName)
    if (found) root.service = found
  }

  onBarChanged: root.bindService()

  Timer {
    interval: 250
    repeat: true
    running: root.service === null
    onTriggered: root.bindService()
  }

  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool playing: service ? service.playing : false
  readonly property bool hasSong: service ? service.hasSong : false
  readonly property string trackTitle: service ? service.title : ""
  readonly property string trackArtist: service ? service.artist : ""
  readonly property bool showTitle: setting("showTitle", true) === true && hasSong && trackTitle !== ""
  readonly property int maxLabelWidth: Style.space(setting("maxLabelWidth", 220))

  implicitWidth: vertical ? barSize : Math.round(row.implicitWidth + Style.spaceReal(14))
  implicitHeight: vertical ? Style.bar.iconSlot : barSize

  function browse() {
    if (root.service && typeof root.service.browse === "function") root.service.browse("")
    else if (root.bar && root.bar.shell && typeof root.bar.shell.summon === "function") root.bar.shell.summon(root.moduleName, "{}")
  }

  function togglePlayback() {
    if (root.service && typeof root.service.toggle === "function") root.service.toggle()
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    fixedWidth: root.vertical ? -1 : root.implicitWidth
    fixedHeight: root.vertical ? root.implicitHeight : -1
    tooltipText: root.hasSong ? root.trackArtist + " — " + root.trackTitle : "Apple Music"

    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.togglePlayback()
      else root.browse()
    }
  }

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(6)

    OpticalGlyph {
      width: Style.bar.iconCanvas
      height: Style.bar.iconCanvas
      anchors.verticalCenter: parent.verticalCenter
      text: "󰝚"   // nf-md-music
      fontFamily: root.fontFamily
      fontSize: Style.bar.iconFont
      // The full bar foreground in every state: at bar size a dimmed glyph
      // reads as a broken one next to its neighbours. Paused is already told
      // by the title disappearing.
      color: root.foreground
    }

    Item {
      id: clipBox
      visible: !root.vertical && root.showTitle
      width: visible ? Math.min(root.maxLabelWidth, label.implicitWidth) : 0
      height: label.implicitHeight
      clip: true
      anchors.verticalCenter: parent.verticalCenter

      Text {
        id: label
        textFormat: Text.PlainText
        text: root.trackTitle
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        renderType: Text.NativeRendering
        elide: Text.ElideRight
        width: parent.width
        anchors.verticalCenter: parent.verticalCenter
      }
    }
  }
}
