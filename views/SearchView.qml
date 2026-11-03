import QtQuick
import qs.Commons
import qs.Ui
import "../components"

// Catalogue search. The field is local so the view owns its own typing
// state; results are grouped the way the web player groups them.
Item {
  id: root

  property var service: null
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal open(string view, var args)
  signal playTracks(var tracks, int index, var context)

  property string term: ""
  property var results: ({ songs: [], albums: [], artists: [], playlists: [] })
  property bool loading: false
  property string error: ""
  readonly property bool editing: field.activeFocus
  readonly property bool hasResults: (results.songs || []).length + (results.albums || []).length
                                        + (results.artists || []).length + (results.playlists || []).length > 0

  function activated() {
    field.forceActiveFocus()
    field.selectAll()
  }

  // Opening straight into Search (sidebar or IPC) must put the caret in the
  // field without a click; the Browser focuses its key catcher afterwards,
  // so wait one turn.
  Component.onCompleted: Qt.callLater(function() {
    if (root.visible) { field.forceActiveFocus(); field.selectAll() }
  })

  function run() {
    if (!root.service) return
    var text = root.term.trim()
    if (text === "") { root.results = ({ songs: [], albums: [], artists: [], playlists: [] }); return }
    root.loading = true
    root.error = ""
    root.service.search(text, function(ok, result) {
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Search failed"; return }
      root.results = result || ({ songs: [], albums: [], artists: [], playlists: [] })
    })
  }

  Timer {
    id: debounce
    interval: 280
    repeat: false
    onTriggered: root.run()
  }

  onTermChanged: debounce.restart()

  Column {
    anchors.fill: parent
    spacing: Style.spacing.lg

    TextField {
      id: field
      width: parent.width
      placeholderText: "Search Apple Music"
      font.pixelSize: Style.font.subtitle
      foreground: root.foreground
      accent: root.accent
      onTextChanged: root.term = text
      onAccepted: { debounce.stop(); root.run() }
      Keys.onEscapePressed: root.term = ""
    }

    Text {
      visible: root.loading
      textFormat: Text.PlainText
      text: "Searching…"
      color: Util.alpha(root.foreground, 0.5)
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }

    Text {
      visible: root.error !== ""
      textFormat: Text.PlainText
      text: root.error
      color: Color.urgent
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }

    Text {
      visible: !root.loading && root.error === "" && !root.hasResults && root.term.trim() !== ""
      textFormat: Text.PlainText
      text: "No results."
      color: Util.alpha(root.foreground, 0.5)
      font.family: Style.font.family
      font.pixelSize: Style.font.body
    }

    Flickable {
      width: parent.width
      height: Math.max(0, parent.height - field.height - Style.spacing.lg)
      visible: root.hasResults
      contentHeight: resultsColumn.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds

      Column {
        id: resultsColumn
        width: parent.width
        spacing: Style.spacing.xl

        Column {
          width: parent.width
          spacing: Style.spacing.sm
          visible: (root.results.songs || []).length > 0

          Text {
            textFormat: Text.PlainText
            text: "Songs"
            color: Util.alpha(root.foreground, 0.6)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            font.weight: Font.DemiBold
          }

          Repeater {
            model: root.results.songs || []

            TrackRow {
              width: resultsColumn.width
              song: modelData
              number: index + 1
              foreground: root.foreground
              accent: root.accent
              onActivated: root.playTracks(root.results.songs, index, { title: "Search: " + root.term })
            }
          }
        }

        Repeater {
          model: [
            { key: "albums", title: "Albums" },
            { key: "playlists", title: "Playlists" },
            { key: "artists", title: "Artists" }
          ]

          Column {
            id: group
            required property var modelData
            property var entries: root.results[modelData ? modelData.key : ""] || []
            width: resultsColumn.width
            spacing: Style.spacing.sm
            visible: entries.length > 0

            Text {
              textFormat: Text.PlainText
              text: group.modelData.title
              color: Util.alpha(root.foreground, 0.6)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              font.weight: Font.DemiBold
            }

            Flow {
              width: parent.width
              spacing: Style.spacing.md

              Repeater {
                model: group.entries

                Card {
                  tileSize: Style.space(130)
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
      }
    }
  }
}
