import QtQuick
import qs.Commons
import qs.Ui
import "../components"

// One album, playlist or artist, opened from a card or a search result.
Item {
  id: root

  property var service: null
  property string kind: "album"          // album | playlist | artist
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal open(string view, var args)
  signal playTracks(var tracks, int index, var context)

  property var info: null
  property var tracks: []

  // Library playlists carry no artwork of their own; the first track's
  // cover is what the web player shows, so use it as the fallback.
  readonly property string artwork: {
    if (root.info && root.info.artwork) return String(root.info.artwork)
    if (root.tracks.length > 0 && root.tracks[0].artwork) return String(root.tracks[0].artwork)
    return ""
  }
  property var albums: []
  property bool loading: false
  property string error: ""

  property int loadToken: 0

  function load() {
    if (!root.service) return
    var id = root.args && root.args.id ? String(root.args.id) : ""
    if (id === "") { root.error = "Nothing selected"; return }
    // A kind and its args change in the same navigation, so the load (and
    // the token that says whether its answer is still wanted) is taken last.
    var token = ++root.loadToken
    var isLibrary = root.args.library === true
    root.loading = true
    root.error = ""
    // Do not leave the previous album on screen under a new title.
    root.info = null
    root.tracks = []
    root.albums = []
    var done = function(ok, result) {
      if (token !== root.loadToken) return
      // The Loader keeps this item alive when only the id changes, so the
      // previous album's scroll offset would otherwise carry over.
      scroll.contentY = 0
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not load"; return }
      if (root.kind === "artist") {
        root.info = result.info || null
        root.albums = result.albums || []
        root.tracks = []
      } else {
        root.info = result.info || null
        root.tracks = result.tracks || []
        root.albums = []
      }
    }
    if (root.kind === "artist") root.service.artistDetail(id, isLibrary, done)
    else if (root.kind === "playlist") root.service.playlistDetail(id, isLibrary, done)
    else root.service.albumDetail(id, isLibrary, done)
  }

  property bool loadQueued: false

  function scheduleLoad() {
    if (root.loadQueued) return
    root.loadQueued = true
    Qt.callLater(function() {
      root.loadQueued = false
      root.load()
    })
  }

  onKindChanged: root.scheduleLoad()
  onArgsChanged: root.scheduleLoad()
  Component.onCompleted: root.scheduleLoad()
  function activated() { root.scheduleLoad() }

  function playAll() {
    var context = {
      title: root.info ? String(root.info.title || "") : "",
      artwork: root.artwork
    }
    if (root.tracks.length > 0) root.playTracks(root.tracks, 0, context)
  }

  Text {
    anchors.centerIn: parent
    visible: root.loading
    textFormat: Text.PlainText
    text: "Loading…"
    color: Util.alpha(root.foreground, 0.5)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  Text {
    anchors.centerIn: parent
    visible: root.error !== ""
    textFormat: Text.PlainText
    text: root.error
    color: Color.urgent
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  Column {
    anchors.fill: parent
    spacing: Style.spacing.lg
    visible: root.info !== null && !root.loading

    Row {
      width: parent.width
      spacing: Style.spacing.lg

      Cover {
        size: Style.space(132)
        source: root.artwork
        glyph: root.kind === "artist" ? "󰠃" : "󰝚"
        foreground: root.foreground
        accent: root.accent
      }

      Column {
        width: parent.width - Style.space(132) - Style.spacing.lg
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.spacing.xs

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: root.info ? String(root.info.title || "") : ""
          color: root.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.display
          font.weight: Font.DemiBold
          renderType: Text.NativeRendering
          elide: Text.ElideRight
        }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: {
            if (!root.info) return ""
            if (root.kind === "artist") {
              var count = root.albums.length || 0
              return count === 1 ? "1 album" : count + " albums"
            }
            var parts = []
            if (root.info.artist) parts.push(String(root.info.artist))
            if (root.info.year) parts.push(String(root.info.year))
            var trackCount = root.kind === "playlist" ? (root.tracks.length || 0) : Number(root.info.trackCount || 0)
            if (trackCount > 0) parts.push(trackCount === 1 ? "1 track" : trackCount + " tracks")
            return parts.join("  ·  ")
          }
          color: Util.alpha(root.foreground, 0.6)
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          renderType: Text.NativeRendering
          elide: Text.ElideRight
        }

        Item { width: 1; height: Style.spacing.xs }

        Row {
          spacing: Style.spacing.md
          visible: root.tracks.length > 0

          Rectangle {
            width: playLabel.implicitWidth + Style.spacing.xl * 2
            height: Style.space(34)
            radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, Style.spacing.sm) : Style.space(17)
            color: playArea.hovered ? Qt.lighter(root.accent, 1.15) : root.accent

            Text {
              id: playLabel
              anchors.centerIn: parent
              textFormat: Text.PlainText
              text: "󰐊  Play"
              color: Color.menu.background
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              font.weight: Font.DemiBold
              renderType: Text.NativeRendering
            }

            HoverHandler { id: playArea; cursorShape: Qt.PointingHandCursor }
            TapHandler { onTapped: root.playAll() }
          }
        }
      }
    }

    // A library playlist can hold a thousand tracks, so the list is a
    // ListView: only the visible rows are instantiated.
    ListView {
      id: scroll
      width: parent.width
      height: Math.max(0, parent.height - Style.space(132) - Style.spacing.lg)
      clip: true
      model: root.kind === "artist" ? [] : root.tracks
      spacing: Style.spacing.xs
      boundsBehavior: Flickable.StopAtBounds
      reuseItems: true

      delegate: TrackRow {
        required property int index
        required property var modelData

        width: scroll.width
        song: modelData
        number: index + 1
        foreground: root.foreground
        accent: root.accent
        current: root.service && root.service.nowPlaying
                 ? String(root.service.nowPlaying.id) === String(modelData.id) : false
        playing: root.service && root.service.nowPlaying
                 ? String(root.service.nowPlaying.id) === String(modelData.id) && root.service.playing : false
        stage: root.service && root.service.build && root.service.build[modelData.id]
               ? String(root.service.build[modelData.id].stage || "") : ""
        onActivated: root.playTracks(root.tracks, index, {
          title: root.info ? String(root.info.title || "") : "",
          artwork: root.artwork
        })
      }

      footer: Flow {
        width: scroll.width
        spacing: Style.spacing.md
        visible: root.kind === "artist"

        Repeater {
          model: root.albums

          Card {
            tileSize: Style.space(140)
            item: modelData
            foreground: root.foreground
            accent: root.accent
            onActivated: root.open("album", { id: modelData.id, library: modelData.library === true, title: modelData.title })
          }
        }
      }
    }
  }
}
