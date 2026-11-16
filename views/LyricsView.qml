import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../components"

// The lyrics of the song that is playing, following it as the queue moves.
//
// Synced lyrics light up the line being sung and keep it in view; a click
// on a line seeks there. Unsynced ones are just the text. The daemon's
// position arrives in steps, so between updates a local clock carries it on
// while playing, or the highlight would trail the voice by up to a step.
Item {
  id: root

  property var service: null
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal open(string view, var args)

  readonly property var nowPlaying: service ? service.nowPlaying : null
  readonly property bool isStation: service ? service.isStation === true : false
  // Stations have no lyrics to ask for; nor does nothing.
  readonly property string songId: nowPlaying && !isStation ? String(nowPlaying.id || "") : ""

  property var lines: []
  // Start of every line in seconds, parallel to `lines`, for the search.
  property var begins: []
  property bool synced: false
  property var writers: []
  property string status: "idle"     // idle | loading | ready | none | error
  property string error: ""
  property string loadedId: ""
  property int loadToken: 0

  // Seconds, whatever shape the time came in: a number of seconds, or a
  // clock string ("1:02.345", "62.345s"). A list whose times run past
  // three hours was given in milliseconds.
  function toSeconds(value) {
    if (typeof value === "number") return value
    var text = String(value || "").trim()
    if (text === "") return 0
    if (/s$/.test(text)) return Number(text.slice(0, -1)) || 0
    var parts = text.split(":")
    var total = 0
    for (var i = 0; i < parts.length; i++) total = total * 60 + (Number(parts[i]) || 0)
    return total
  }

  function load() {
    var id = root.songId
    if (id === "") {
      root.loadToken++
      root.status = "idle"
      root.loadedId = ""
      root.lines = []
      root.begins = []
      root.writers = []
      root.synced = false
      return
    }
    if (!root.service) return
    if (id === root.loadedId && (root.status === "ready" || root.status === "none" || root.status === "loading")) return
    var token = ++root.loadToken
    root.loadedId = id
    root.status = "loading"
    root.error = ""
    root.lines = []
    root.begins = []
    root.writers = []
    root.synced = false
    root.service.lyrics(id, function(ok, result) {
      if (token !== root.loadToken) return
      if (!ok) {
        if (result && result.code === "not-found") { root.status = "none"; return }
        root.error = result && result.message ? String(result.message) : "Could not load the lyrics"
        root.status = "error"
        return
      }
      var list = result && Array.isArray(result.lines) ? result.lines : []
      var starts = list.map(function(line) { return root.toSeconds(line ? line.begin : 0) })
      var last = starts.length > 0 ? starts[starts.length - 1] : 0
      if (last > 3 * 3600) starts = starts.map(function(value) { return value / 1000 })
      root.synced = result.synced === true && list.length > 0
      root.lines = list
      root.begins = starts
      root.writers = result && Array.isArray(result.writers) ? result.writers : []
      root.status = list.length > 0 ? "ready" : "none"
      if (list.length > 0) view.positionViewAtBeginning()
    })
  }

  property bool loadQueued: false
  function scheduleLoad() {
    if (root.loadQueued) return
    root.loadQueued = true
    Qt.callLater(function() {
      // The view can be gone by then (the window moved on in the same turn).
      if (typeof root.load !== "function") return
      root.loadQueued = false
      root.load()
    })
  }

  onSongIdChanged: root.scheduleLoad()
  Component.onCompleted: root.scheduleLoad()
  function activated() {
    // A failed load is retried; a loaded one only scrolls back to the line.
    if (root.status === "error") root.loadedId = ""
    root.scheduleLoad()
    Qt.callLater(function() { if (root.currentLine >= 0) view.positionViewAtIndex(root.currentLine, ListView.Center) })
  }

  // ---- clock -------------------------------------------------------------
  readonly property real reported: service ? Number(service.position) || 0 : 0
  readonly property bool running: service ? service.playing === true : false
  property real basePosition: 0
  property double baseAt: 0
  property real clock: 0

  function resync() {
    root.basePosition = root.reported
    root.baseAt = Date.now()
    root.clock = root.reported
  }

  onReportedChanged: root.resync()
  onRunningChanged: root.resync()

  Timer {
    interval: 100
    repeat: true
    running: root.visible && root.synced && root.running
    onTriggered: root.clock = root.basePosition + (Date.now() - root.baseAt) / 1000
  }

  // A line lights up a moment early: reading lags hearing.
  readonly property int currentLine: {
    if (!root.synced || root.begins.length === 0) return -1
    var t = root.clock + 0.25
    var lo = 0
    var hi = root.begins.length - 1
    if (t < root.begins[0]) return -1
    while (lo < hi) {
      var mid = Math.ceil((lo + hi) / 2)
      if (root.begins[mid] <= t) lo = mid
      else hi = mid - 1
    }
    return lo
  }

  // ---- states ------------------------------------------------------------
  Text {
    anchors.centerIn: parent
    width: Math.min(implicitWidth, parent.width - Style.spacing.xl * 2)
    visible: root.status !== "ready"
    horizontalAlignment: Text.AlignHCenter
    wrapMode: Text.WordWrap
    textFormat: Text.PlainText
    text: {
      if (!root.nowPlaying) return "Nothing playing."
      if (root.isStation) return "Radio stations have no lyrics."
      switch (root.status) {
        case "loading": return "Loading…"
        case "none": return "No lyrics for this song."
        case "error": return root.error
        default: return ""
      }
    }
    color: root.status === "error" ? Color.urgent : Util.alpha(root.foreground, 0.55)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  // ---- lyrics ------------------------------------------------------------
  ListView {
    id: view
    anchors.fill: parent
    visible: root.status === "ready"
    clip: true
    model: root.lines
    spacing: Style.spacing.md
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

    // The sung line rides a third of the way down, with what comes next in
    // view below it. The reader can scroll away; the next line brings the
    // view back.
    currentIndex: root.currentLine
    highlightRangeMode: root.synced ? ListView.ApplyRange : ListView.NoHighlightRange
    preferredHighlightBegin: Math.round(height * 0.3)
    preferredHighlightEnd: Math.round(height * 0.45)
    highlightFollowsCurrentItem: true
    highlightMoveDuration: 320
    highlight: Item {}

    header: Column {
      width: view.width
      spacing: 1
      bottomPadding: Style.spacing.xl

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.nowPlaying ? String(root.nowPlaying.title || "") : ""
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.title
        font.weight: Font.DemiBold
        renderType: Text.NativeRendering
        elide: Text.ElideRight
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: root.nowPlaying ? String(root.nowPlaying.artist || "") : ""
        color: Util.alpha(root.foreground, 0.55)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        renderType: Text.NativeRendering
        elide: Text.ElideRight
      }
    }

    delegate: Item {
      id: lineItem
      required property var modelData
      required property int index

      readonly property string text: modelData ? String(modelData.text || "") : ""
      readonly property bool current: index === root.currentLine
      readonly property bool past: root.synced && root.currentLine >= 0 && index < root.currentLine
      readonly property bool seekable: root.synced && !!root.service && root.service.canSeek === true

      width: view.width
      height: lineText.implicitHeight + Style.spacing.xs * 2

      Rectangle {
        anchors.fill: parent
        anchors.leftMargin: -Style.spacing.sm
        radius: Math.min(Style.cornerRadius, Style.spacing.sm)
        color: lineHover.hovered && lineItem.seekable ? Util.alpha(root.foreground, 0.05) : "transparent"
      }

      Text {
        id: lineText
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.rightMargin: Style.spacing.xl
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        // An empty line is an instrumental break: a note, so the gap reads
        // as a pause in the song rather than a missing line.
        text: lineItem.text !== "" ? lineItem.text : "♪"
        wrapMode: Text.WordWrap
        color: !root.synced ? Util.alpha(root.foreground, 0.85)
             : (lineItem.current ? root.foreground
             : Util.alpha(root.foreground, lineItem.past ? 0.4 : 0.3))
        font.family: Style.font.family
        font.pixelSize: root.synced ? Style.font.display : Style.font.heading
        font.weight: root.synced ? Font.DemiBold : Font.Normal
        renderType: Text.NativeRendering
        Behavior on color { ColorAnimation { duration: 180 } }
      }

      HoverHandler { id: lineHover; cursorShape: lineItem.seekable ? Qt.PointingHandCursor : Qt.ArrowCursor }
      TapHandler {
        enabled: lineItem.seekable
        onTapped: {
          var at = root.begins[lineItem.index]
          if (root.service && at !== undefined) root.service.seek(at)
        }
      }
    }

    footer: Text {
      width: view.width
      visible: root.writers.length > 0
      topPadding: Style.spacing.xxl
      bottomPadding: Style.spacing.xl
      textFormat: Text.PlainText
      text: root.writers.length > 0 ? "Written by " + root.writers.join(", ") : ""
      color: Util.alpha(root.foreground, 0.45)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
      renderType: Text.NativeRendering
    }
  }
}
