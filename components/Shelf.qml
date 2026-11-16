import QtQuick
import qs.Commons
import qs.Ui

// One section of a catalogue page (Home, New, Radio, a genre, a show, an
// artist): a title, a "See All" link when Apple has more, and the items laid
// out the way the section's kind asks.
//
//   hero   a row of large wide tiles (On Air Now, featured stations)
//   cards  a row of cover tiles that scrolls sideways
//   songs  up to eight track rows, in two columns when there is room
//   links  pill buttons (genres, Charts, Moods and Activities)
//
// Rows are horizontal ListViews, so a shelf of fifty covers builds only the
// few on screen. They do not take the vertical wheel: a page of shelves has
// to scroll as one page with the cursor over any of them. Sideways is the
// chevrons, a horizontal wheel or touchpad swipe, or Shift+wheel.
Column {
  id: root

  property var section: ({})
  property var service: null
  property color foreground: Color.menu.text
  property color accent: Color.accent

  // Anything but a song: the window routes it (detail page, station, link).
  signal activateItem(var entry)
  signal playTracks(var tracks, int index, var context)
  signal openTarget(string view, var args)
  signal songMenu(var song, var source, real x, real y)

  readonly property string kind: section ? String(section.kind || "cards") : "cards"
  readonly property string title: section ? String(section.title || "") : ""
  readonly property var entries: section && Array.isArray(section.items) ? section.items : []
  readonly property var seeAll: section && section.seeAll && section.seeAll.view ? section.seeAll : null
  readonly property bool isRow: kind === "hero" || kind === "cards"
  // The section's songs, as the queue a click on one of them plays.
  readonly property var songs: entries.filter(function(entry) { return entry && entry.type === "song" })
  readonly property real tile: kind === "hero" ? Style.space(170) : Style.space(140)

  spacing: Style.spacing.sm

  function activate(entry) {
    if (!entry) return
    if (entry.type === "song") {
      root.playTracks(root.songs, Math.max(0, root.songs.indexOf(entry)), { title: root.title })
      return
    }
    root.activateItem(entry)
  }

  function nowId() {
    return root.service && root.service.nowPlaying ? String(root.service.nowPlaying.id) : ""
  }

  // ---- header ----
  Item {
    width: root.width
    height: Math.max(titleText.implicitHeight, Style.space(26))
    visible: root.title !== "" || root.seeAll !== null

    Text {
      id: titleText
      anchors.left: parent.left
      anchors.right: tools.left
      anchors.rightMargin: Style.spacing.md
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: root.title
      color: root.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.title
      font.weight: Font.DemiBold
      renderType: Text.NativeRendering
      elide: Text.ElideRight
    }

    Row {
      id: tools
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.spacing.sm

      Text {
        id: seeAllLink
        anchors.verticalCenter: parent.verticalCenter
        visible: root.seeAll !== null
        textFormat: Text.PlainText
        text: "See All"
        color: seeAllHover.hovered ? root.accent : Util.alpha(root.foreground, 0.6)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        font.underline: seeAllHover.hovered
        renderType: Text.NativeRendering

        HoverHandler { id: seeAllHover; cursorShape: Qt.PointingHandCursor }
        TapHandler {
          onTapped: {
            if (!root.seeAll) return
            root.openTarget(String(root.seeAll.view), root.seeAll.args || {})
          }
        }
      }

      // Only when the row is wider than the page.
      Repeater {
        model: root.isRow && body.item && body.item.contentWidth > body.item.width + 1 ? [-1, 1] : []

        Rectangle {
          required property int modelData
          readonly property bool canMove: body.item
            ? (modelData < 0 ? body.item.contentX > body.item.originX + 1
                             : body.item.contentX + body.item.width < body.item.originX + body.item.contentWidth - 1)
            : false
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(24)
          height: width
          radius: width / 2
          color: chevronHover.hovered && canMove ? Util.alpha(root.foreground, 0.12) : "transparent"

          Text {
            anchors.centerIn: parent
            textFormat: Text.PlainText
            text: parent.modelData < 0 ? "󰅁" : "󰅂"
            color: Util.alpha(root.foreground, parent.canMove ? 0.8 : 0.25)
            font.family: Style.font.family
            font.pixelSize: Style.font.title
            renderType: Text.NativeRendering
          }

          HoverHandler { id: chevronHover; cursorShape: parent.canMove ? Qt.PointingHandCursor : Qt.ArrowCursor }
          TapHandler { onTapped: if (body.item) body.item.page(parent.modelData) }
        }
      }
    }
  }

  // ---- body ----
  Loader {
    id: body
    width: root.width
    sourceComponent: root.kind === "songs" ? songsComponent
                   : (root.kind === "links" ? linksComponent : rowComponent)
  }

  // Sizes a row from a real card, so a theme's fonts never clip the
  // second line of text.
  Card {
    id: probe
    visible: false
    tileSize: root.tile
    wide: root.kind === "hero"
    item: ({ title: "Probe", artist: "Probe" })
  }

  Component {
    id: rowComponent

    ListView {
      id: strip
      orientation: ListView.Horizontal
      height: probe.implicitHeight
      spacing: Style.spacing.md
      clip: true
      interactive: false
      boundsBehavior: Flickable.StopAtBounds
      reuseItems: true
      model: root.entries

      function clampX(x) {
        var max = strip.originX + Math.max(0, strip.contentWidth - strip.width)
        return Math.max(strip.originX, Math.min(max, x))
      }

      // A chevron moves most of a page, keeping a card of overlap.
      function page(direction) {
        slide.stop()
        slide.to = strip.clampX(strip.contentX + direction * Math.max(Style.space(140), strip.width * 0.8))
        slide.start()
      }

      NumberAnimation {
        id: slide
        target: strip
        property: "contentX"
        duration: 260
        easing.type: Easing.OutCubic
      }

      // Picks hand on the entry from `entries`, not modelData: the model's
      // copy is not the object the queue and the menu should get.
      delegate: Card {
        required property var modelData
        required property int index
        tileSize: root.tile
        wide: root.kind === "hero"
        item: modelData
        foreground: root.foreground
        accent: root.accent
        onActivated: root.activate(root.entries[index])
        onMenuRequested: function(source, x, y) {
          var entry = root.entries[index]
          if (entry && entry.type === "song") root.songMenu(entry, source, x, y)
        }
      }

      // Sideways input scrolls the row; anything vertical is left to the
      // page underneath (accepted = false passes it on).
      MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.NoButton
        onWheel: function(wheel) {
          var shifted = (wheel.modifiers & Qt.ShiftModifier) !== 0
          var dx = shifted ? (wheel.pixelDelta.y || wheel.angleDelta.y) : (wheel.pixelDelta.x || wheel.angleDelta.x)
          var dy = shifted ? 0 : wheel.angleDelta.y
          if (dx === 0 || Math.abs(dx) < Math.abs(dy)) { wheel.accepted = false; return }
          slide.stop()
          var step = wheel.pixelDelta.x !== 0 || (shifted && wheel.pixelDelta.y !== 0) ? dx : dx / 120 * Style.space(90)
          strip.contentX = strip.clampX(strip.contentX - step)
          wheel.accepted = true
        }
      }
    }
  }

  Component {
    id: songsComponent

    Grid {
      id: grid
      readonly property var shown: root.entries.slice(0, 8)
      columns: root.width >= Style.space(620) && shown.length > 4 ? 2 : 1
      columnSpacing: Style.spacing.lg
      rowSpacing: Style.spacing.xs
      flow: Grid.TopToBottom
      rows: Math.ceil(shown.length / columns)

      Repeater {
        model: grid.shown

        TrackRow {
          required property var modelData
          required property int index
          width: Math.floor((root.width - grid.columnSpacing * (grid.columns - 1)) / grid.columns)
          song: modelData
          number: index + 1
          foreground: root.foreground
          accent: root.accent
          menuEnabled: true
          current: root.nowId() !== "" && root.nowId() === String(modelData.id)
          playing: current && root.service ? root.service.playing : false
          onActivated: root.activate(grid.shown[index])
          onMenuRequested: function(source, x, y) { root.songMenu(grid.shown[index], source, x, y) }
        }
      }
    }
  }

  Component {
    id: linksComponent

    Flow {
      spacing: Style.spacing.sm

      Repeater {
        model: root.entries

        Button {
          required property var modelData
          required property int index
          height: Style.space(30)
          radius: height / 2
          bordered: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: Style.font.family
          fontSize: Style.font.bodySmall
          text: String(modelData.title || "")
          onClicked: root.activate(root.entries[index])
        }
      }
    }
  }
}
