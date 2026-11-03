import QtQuick
import qs.Commons
import qs.Ui
import "../components"

// Browse: recently played and the charts, as the web player's front page.
Item {
  id: root

  property var service: null
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal open(string view, var args)
  signal playTracks(var tracks, int index, var context)

  property var sections: []
  // Covers first, then the song list: a card row a third of a screen tall
  // reads as a page, a twelve-row list does not.
  readonly property var ordered: {
    var cards = []
    var songs = []
    for (var i = 0; i < sections.length; i++) {
      if (String(sections[i].kind) === "songs") songs.push(sections[i])
      else cards.push(sections[i])
    }
    return cards.concat(songs)
  }
  property bool loading: false
  property string error: ""

  function load() {
    if (!root.service || root.loading) return
    root.loading = true
    root.error = ""
    root.service.home(function(ok, result) {
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not load the catalogue"; return }
      root.sections = result && result.sections ? result.sections : []
    })
  }

  Component.onCompleted: root.load()

  function activated() {
    scroll.contentY = 0
    root.load()
  }

  Text {
    anchors.centerIn: parent
    visible: root.loading && root.sections.length === 0
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

  Flickable {
    id: scroll
    anchors.fill: parent
    visible: root.sections.length > 0
    contentHeight: column.implicitHeight + Style.spacing.lg
    clip: true
    boundsBehavior: Flickable.StopAtBounds

    Column {
      id: column
      width: parent.width
      spacing: Style.spacing.xl

      Repeater {
        model: root.ordered

        Column {
          id: section
          required property var modelData
          property var entries: modelData && modelData.items ? modelData.items : []
          width: column.width
          spacing: Style.spacing.sm

          Text {
            textFormat: Text.PlainText
            text: section.modelData.title
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.title
            font.weight: Font.DemiBold
            renderType: Text.NativeRendering
          }

          Flow {
            width: parent.width
            spacing: Style.spacing.md
            visible: section.modelData.kind !== "songs"

            Repeater {
              model: section.entries

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

          Column {
            width: parent.width
            visible: section.modelData.kind === "songs"

            Repeater {
              model: section.modelData.kind === "songs" ? section.entries.slice(0, 8) : []

              TrackRow {
                width: section.width
                song: modelData
                number: index + 1
                foreground: root.foreground
                accent: root.accent
                onActivated: root.playTracks(section.entries, index, { title: section.modelData.title })
              }
            }
          }
        }
      }
    }
  }
}
