import QtQuick
import qs.Commons
import qs.Ui
import "../components"

// Catalogue search. The field is local so the view owns its own typing
// state; results are grouped the way the web player groups them, radio
// stations and shows included.
Item {
  id: root

  property var service: null
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal open(string view, var args)
  signal playTracks(var tracks, int index, var context)
  signal activateItem(var entry)
  signal songMenu(var song, var source, real x, real y)

  readonly property var emptyResults: ({ songs: [], albums: [], artists: [], playlists: [], stations: [], curators: [] })
  // The card groups under the songs, in the web player's order.
  readonly property var groups: [
    { key: "albums", title: "Albums" },
    { key: "playlists", title: "Playlists" },
    { key: "artists", title: "Artists" },
    { key: "stations", title: "Stations" },
    { key: "curators", title: "Shows" }
  ]

  property string term: ""
  property var results: root.emptyResults
  property bool loading: false
  property string error: ""
  readonly property bool editing: field.activeFocus
  readonly property bool hasResults: {
    if ((results.songs || []).length > 0) return true
    for (var i = 0; i < groups.length; i++)
      if ((results[groups[i].key] || []).length > 0) return true
    return false
  }

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

  // The daemon answers searches in the order they finish, not the order they
  // were typed, so each run takes a token and only the newest answer counts.
  // Clearing the field takes one too, which drops a reply still on its way.
  property int searchToken: 0

  function run() {
    if (!root.service) return
    var text = root.term.trim()
    var token = ++root.searchToken
    if (text === "") {
      root.loading = false
      root.error = ""
      root.results = root.emptyResults
      return
    }
    root.loading = true
    root.error = ""
    root.service.search(text, function(ok, result) {
      if (token !== root.searchToken) return
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Search failed"; return }
      root.results = result || root.emptyResults
    })
  }

  Timer {
    id: debounce
    interval: 280
    repeat: false
    onTriggered: root.run()
  }

  // An emptied field (Escape, or deleting the text) clears at once rather
  // than after the debounce, so a reply landing in between cannot refill
  // the list under it.
  onTermChanged: {
    if (root.term.trim() === "") { debounce.stop(); root.run() }
    else debounce.restart()
  }

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
      // The first Escape clears the field; with nothing left to clear it
      // goes on to the window, which walks back or closes. Accepting it
      // unconditionally trapped a keyboard user in Search.
      Keys.onEscapePressed: function(event) {
        if (field.text !== "" || root.term !== "") {
          field.text = ""
          root.term = ""
          event.accepted = true
        } else {
          event.accepted = false
        }
      }
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
              menuEnabled: true
              onActivated: root.playTracks(root.results.songs, index, { title: "Search: " + root.term })
              onMenuRequested: function(source, x, y) { root.songMenu(root.results.songs[index], source, x, y) }
            }
          }
        }

        Repeater {
          model: root.groups

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
                  // The entry itself, not the model's converted copy.
                  onActivated: root.activateItem(group.entries[index])
                }
              }
            }
          }
        }
      }
    }
  }
}
