import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui

// A cover tile for anything the catalogue lists: an album, playlist, artist,
// station, radio show or curator, a link to another page, or a video.
//
// Stations wear what makes them different on the cover: a LIVE tag for a
// live stream, otherwise the kind of station or episode Apple gives as its
// subtitle ("Station", "Episode · 2 hr"). Videos are listed by Apple but this
// is an audio player, so they are shown dimmed and do nothing when clicked;
// the tooltip says why rather than leaving a dead tile.
Item {
  id: root

  property var item: ({})
  // The cover's height, and its width too unless the tile is wide.
  property real tileSize: Style.space(150)
  // A wide (hero) tile: the featured row at the top of Radio and New.
  property bool wide: false
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal activated()
  // Right-click: the view decides whether this item has a menu (songs do).
  signal menuRequested(var source, real x, real y)

  readonly property bool hovered: hover.hovered
  readonly property string type: item ? String(item.type || "") : ""
  readonly property bool isVideo: type === "video"
  readonly property bool isStation: type === "station"
  readonly property bool isLive: isStation && item.live === true
  readonly property real coverWidth: wide ? Math.round(tileSize * 1.6) : tileSize
  readonly property string title: item ? String(item.title || "") : ""
  // "LIVE · The Ebro Show": the tag says LIVE already, the rest is the show.
  readonly property string stationSubtitle: {
    if (!isStation) return ""
    var text = String(item.subtitle || "")
    return isLive ? text.replace(/^\s*LIVE\s*·\s*/i, "") : text
  }
  readonly property string subtitle: {
    if (!item) return ""
    if (item.artist) return String(item.artist)
    if (isLive && stationSubtitle !== "") return stationSubtitle
    if (type === "curator" && item.kind) return String(item.kind)
    return String(item.description || "")
  }
  // The chip on a station's cover that is not live: what kind it is.
  readonly property string hint: isStation && !isLive ? stationSubtitle : ""

  implicitWidth: coverWidth
  implicitHeight: tileSize + label.implicitHeight + Style.spacing.sm
  width: coverWidth
  height: implicitHeight
  opacity: isVideo ? 0.45 : 1

  HoverHandler { id: hover; cursorShape: root.isVideo ? Qt.ArrowCursor : Qt.PointingHandCursor }
  TapHandler { onTapped: if (!root.isVideo) root.activated() }
  TapHandler {
    acceptedButtons: Qt.RightButton
    onTapped: function(eventPoint) {
      if (!root.isVideo) root.menuRequested(root, eventPoint.position.x, eventPoint.position.y)
    }
  }

  ToolTip {
    visible: root.isVideo && hover.hovered
    text: "Videos are not supported"
    delay: 400
    padding: 0
    background: Rectangle {
      color: Color.tooltip.background
      border.color: Color.tooltip.border
      border.width: Math.max(1, Style.normalBorderWidth)
    }
    contentItem: Text {
      textFormat: Text.PlainText
      text: "Videos are not supported"
      color: Color.tooltip.text
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      leftPadding: Style.spacing.controlPaddingX
      rightPadding: Style.spacing.controlPaddingX
      topPadding: Style.spacing.controlPaddingY
      bottomPadding: Style.spacing.controlPaddingY
    }
  }

  Cover {
    id: cover
    anchors.top: parent.top
    anchors.left: parent.left
    anchors.right: parent.right
    height: root.tileSize
    source: root.item ? String(root.item.artwork || "") : ""
    size: root.tileSize
    foreground: root.foreground
    accent: root.accent
    glyph: {
      switch (root.type) {
        case "artist": return "󰠃"
        case "station": return "󰐹"
        case "curator": return root.item && String(root.item.kind || "") === "Show" ? "󰐹" : "󰠃"
        case "video": return "󰕧"
        case "link": return "󰁔"
        default: return "󰝚"
      }
    }

    LiveBadge {
      visible: root.isLive
      anchors.left: parent.left
      anchors.bottom: parent.bottom
      anchors.margins: Style.spacing.md
      accent: root.accent
    }

    // Light text on a dark chip reads on any artwork.
    Rectangle {
      visible: root.hint !== ""
      anchors.left: parent.left
      anchors.bottom: parent.bottom
      anchors.margins: Style.spacing.md
      width: Math.min(hintText.implicitWidth + Style.spacing.md * 2, parent.width - Style.spacing.md * 2)
      height: hintText.implicitHeight + Style.spacing.xxs * 2
      radius: Math.min(Style.cornerRadius, height / 2)
      color: Qt.rgba(0, 0, 0, 0.62)

      Text {
        id: hintText
        anchors.centerIn: parent
        width: Math.min(implicitWidth, parent.width - Style.spacing.md * 2)
        textFormat: Text.PlainText
        text: root.hint
        color: "white"
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        renderType: Text.NativeRendering
        elide: Text.ElideRight
      }
    }
  }

  Column {
    id: label
    anchors.top: cover.bottom
    anchors.topMargin: Style.spacing.sm
    anchors.left: parent.left
    anchors.right: parent.right
    spacing: 1

    Text {
      width: parent.width
      textFormat: Text.PlainText
      text: root.title
      color: root.foreground
      font.family: Style.font.family
      font.pixelSize: Style.font.body
      font.weight: root.hovered && !root.isVideo ? Font.DemiBold : Font.Normal
      renderType: Text.NativeRendering
      elide: Text.ElideRight
    }

    Text {
      width: parent.width
      visible: root.subtitle !== ""
      textFormat: Text.PlainText
      text: root.subtitle
      color: Util.alpha(root.foreground, 0.55)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      renderType: Text.NativeRendering
      elide: Text.ElideRight
    }
  }
}
