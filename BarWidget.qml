import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// The bar cell: a music glyph, the current track, and the host for the
// now-playing dropdown (NowPlayingPanel.qml).
//
// One of these exists per monitor. It owns nothing but its own pixels; state
// and the daemon connection live in Service.qml, reached through
// bar.shell.serviceFor(). Left click opens the dropdown, middle click
// play/pauses, right click opens the full window, and the wheel is volume
// (or seek, per settings).
BarWidget {
  id: root
  moduleName: "io.github.hominluo.apple-music"

  property var service: null

  function bindService() {
    if (root.service || !root.bar || !root.bar.shell || typeof root.bar.shell.serviceFor !== "function") return
    var found = root.bar.shell.serviceFor(root.moduleName)
    if (found) root.service = found
  }

  onBarChanged: { bindService(); injectPanel() }
  onSettingsChanged: injectPanel()
  onServiceChanged: injectPanel()

  // The service may not exist yet when the bar builds its widgets; it does a
  // moment later. Stop asking once it is there.
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
  readonly property int maxLabelWidth: Style.space(setting("maxLabelWidth", 120))
  readonly property string wheelAction: String(setting("wheelAction", "volume"))

  // ---- dropdown contract (Bar.findPanelWidget needs opened/open/close) ----
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  readonly property real openPanelIndicatorWidth: row.implicitWidth
  readonly property real openPanelIndicatorHeight: Style.bar.iconSlot

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
    if ("service" in target) target.service = root.service
  }

  function browse() {
    root.close()
    if (root.service && typeof root.service.browse === "function") root.service.browse("")
    else if (root.bar && root.bar.shell && typeof root.bar.shell.summon === "function") root.bar.shell.summon(root.moduleName, "{}")
  }

  function togglePlayback() {
    if (root.service && typeof root.service.toggle === "function") root.service.toggle()
  }

  implicitWidth: vertical ? barSize : Math.round(row.implicitWidth + Style.spaceReal(14))
  implicitHeight: vertical ? Style.bar.iconSlot : barSize

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("NowPlayingPanel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  // The dropdown's own IPC target. `omarchy-shell shell toggle <id>` reaches
  // the browse window instead, because this plugin also declares an overlay.
  IpcHandler {
    target: root.moduleName + ".panel"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    fixedWidth: root.vertical ? -1 : root.implicitWidth
    fixedHeight: root.vertical ? root.implicitHeight : -1
    tooltipText: root.hasSong
      ? root.trackArtist + " — " + root.trackTitle
        + (root.service ? "\n" + root.formatTime(root.service.position) + " / " + root.formatTime(root.service.duration) : "")
      : (root.service && root.service.connected ? "Apple Music" : "Apple Music — starting…")

    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.togglePlayback()
      else if (b === Qt.RightButton) root.browse()
      else root.toggle()
    }

    property real wheelAcc: 0
    onWheelMoved: function(delta) {
      if (!root.service || root.wheelAction === "none") return
      var wheel = Util.wheelSteps(button.wheelAcc, delta)
      button.wheelAcc = wheel.remainder
      if (wheel.steps === 0) return
      if (root.wheelAction === "seek" && root.hasSong) {
        var target = Math.max(0, Math.min(root.service.duration || 0,
                                           root.service.position + wheel.steps * 10))
        root.service.seek(target)
      } else {
        var volume = Math.round(Number(root.service.player ? root.service.player.volume : 0) || 0)
        root.service.setVolume(Math.max(0, Math.min(130, volume + wheel.steps * 5)))
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
        // by the title.
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
          anchors.verticalCenter: parent.verticalCenter

          readonly property bool needsScroll: implicitWidth > clipBox.width
          onNeedsScrollChanged: if (!needsScroll) x = 0

          // Only move when the title does not fit, and stop while the
          // dropdown is open so the text behind it is not in motion.
          NumberAnimation on x {
            running: label.needsScroll && clipBox.visible && !root.opened
            loops: Animation.Infinite
            duration: Math.max(6000, label.implicitWidth * 25)
            from: clipBox.width
            to: -label.implicitWidth
            easing.type: Easing.Linear
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
