import QtQuick
import qs.Commons
import qs.Ui
import "../components"

// The saved library: songs, albums, playlists, artists.
Item {
  id: root

  property var service: null
  property string kind: "songs"
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal open(string view, var args)
  signal playTracks(var tracks, int index, var context)

  property var items: []
  property bool loading: false
  property string error: ""
  property bool signedIn: service ? service.signedIn : false

  readonly property string title: kind === "songs" ? "Songs"
    : kind === "albums" ? "Albums"
    : kind === "playlists" ? "Playlists" : "Artists"

  function load() {
    if (!root.service || root.loading) return
    root.loading = true
    root.error = ""
    root.service.library(root.kind, function(ok, result) {
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not load the library"; return }
      root.items = result && result.items ? result.items : []
    })
  }

  onKindChanged: root.load()
  Component.onCompleted: root.load()
  function activated() { root.load() }

  Text {
    anchors.centerIn: parent
    visible: !root.signedIn
    textFormat: Text.PlainText
    text: "Sign in to see your library."
    color: Util.alpha(root.foreground, 0.55)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
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

  Text {
    anchors.centerIn: parent
    visible: root.signedIn && !root.loading && root.error === "" && root.items.length === 0
    textFormat: Text.PlainText
    text: "Nothing here yet."
    color: Util.alpha(root.foreground, 0.5)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  Flickable {
    anchors.fill: parent
    visible: root.kind === "songs" && root.items.length > 0
    contentHeight: songsColumn.implicitHeight
    clip: true
    boundsBehavior: Flickable.StopAtBounds

    Column {
      id: songsColumn
      width: parent.width
      spacing: Style.spacing.xs

      Repeater {
        model: root.kind === "songs" ? root.items : []

        TrackRow {
          width: songsColumn.width
          song: modelData
          number: index + 1
          foreground: root.foreground
          accent: root.accent
          onActivated: root.playTracks(root.items, index, { title: "Library" })
        }
      }
    }
  }

  Flickable {
    anchors.fill: parent
    visible: root.kind !== "songs" && root.items.length > 0
    contentHeight: flow.implicitHeight
    clip: true
    boundsBehavior: Flickable.StopAtBounds

    Flow {
      id: flow
      width: parent.width
      spacing: Style.spacing.md

      Repeater {
        model: root.kind === "songs" ? [] : root.items

        Card {
          tileSize: Style.space(140)
          item: modelData
          foreground: root.foreground
          accent: root.accent
          onActivated: root.open(modelData.type === "artist" ? "artist"
                                 : (modelData.type === "playlist" ? "playlist" : "album"),
                                 { id: modelData.id, library: modelData.library === true, title: modelData.title })
        }
      }
    }
  }
}
