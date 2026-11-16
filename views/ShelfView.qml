import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../components"

// Every catalogue page that is a stack of shelves: Home (personal picks),
// New, Radio, a genre page, a radio show or curator, and Charts. Which one is
// `kind` (the view name the window navigated to); `args.id` names the genre,
// show or curator.
//
// The page is a ListView of sections, so a long Radio page builds only the
// shelves near the screen, and each shelf builds only the covers in view.
Item {
  id: root

  property var service: null
  property string kind: "home"     // home | new | radio | grouping | curator | charts
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent
  // Shared with the window, so walking back to a page shows it at once (and
  // then refreshes) instead of starting from "Loading…".
  property var pageCache: null

  signal open(string view, var args)
  signal playTracks(var tracks, int index, var context)
  signal activateItem(var entry)
  signal songMenu(var song, var source, real x, real y)

  property var sections: []
  property var info: null           // a curator or show page's own card
  property string pageTitle: ""
  property bool loading: false
  property string error: ""
  property int loadToken: 0
  // The page a request is out for: opening the window asks twice in a row
  // (the load on creation, then activated), and one answer is enough.
  property string loadingKey: ""
  // What `sections` belongs to, so a page answered after the user moved on
  // is not shown under the next page's title.
  property string shownKey: ""

  readonly property bool signedIn: service ? service.signedIn === true && service.authExpired !== true : false
  readonly property bool needsAccount: kind === "home" && !signedIn
  readonly property string pageId: args && args.id ? String(args.id) : ""
  readonly property string key: kind + ":" + pageId

  readonly property string fallbackTitle: {
    switch (root.kind) {
      case "home": return "Home"
      case "new": return "New"
      case "radio": return "Radio"
      case "charts": return "Charts"
      default: return root.args && root.args.title ? String(root.args.title) : ""
    }
  }

  function apply(result) {
    var list = result && Array.isArray(result.sections) ? result.sections : []
    // Apple leaves some shelves empty (a region without a show); a title
    // over nothing reads as broken.
    list = list.filter(function(section) { return section && Array.isArray(section.items) && section.items.length > 0 })
    var nextInfo = result && result.info ? result.info : null
    // A refresh that brought the same page back must not reset the list
    // (and throw the reader back to the top).
    if (JSON.stringify(list) !== JSON.stringify(root.sections)) root.sections = list
    if (JSON.stringify(nextInfo) !== JSON.stringify(root.info)) root.info = nextInfo
    var title = result && result.title ? String(result.title) : (nextInfo && nextInfo.title ? String(nextInfo.title) : "")
    root.pageTitle = title !== "" ? title : root.fallbackTitle
  }

  function load() {
    if (!root.service) return
    var key = root.key
    if (root.loading && root.loadingKey === key) return
    var token = ++root.loadToken
    var cached = root.pageCache ? root.pageCache[key] : undefined
    root.error = ""
    if (root.shownKey !== key) {
      root.sections = []
      root.info = null
      root.pageTitle = root.fallbackTitle
      if (cached) root.apply(cached)
      root.shownKey = key
      list.positionViewAtBeginning()
    }
    if (root.needsAccount) { root.loading = false; return }
    var done = function(ok, result) {
      if (token !== root.loadToken) return
      root.loading = false
      if (!ok) {
        root.error = result && result.message ? String(result.message) : "Could not load this page"
        return
      }
      if (root.pageCache) root.pageCache[key] = result
      root.apply(result)
    }
    root.loading = true
    root.loadingKey = key
    switch (root.kind) {
      case "home": case "new": case "radio": root.service.browse(root.kind, done); break
      case "grouping": root.service.grouping(root.pageId, done); break
      case "curator": root.service.curator(root.pageId, root.args && root.args.kind ? String(root.args.kind) : "", done); break
      case "charts": root.service.charts(done); break
      default:
        root.loading = false
        root.error = "This page is not available"
    }
  }

  // A kind and its args change in the same navigation (New to a genre), so
  // the load waits a turn and runs once for both.
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

  onKeyChanged: root.scheduleLoad()
  onNeedsAccountChanged: root.scheduleLoad()
  Component.onCompleted: root.scheduleLoad()
  function activated() { root.scheduleLoad() }

  Text {
    anchors.centerIn: parent
    visible: root.needsAccount
    textFormat: Text.PlainText
    text: "Sign in to see your recommendations."
    color: Util.alpha(root.foreground, 0.55)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  Text {
    anchors.centerIn: parent
    visible: !root.needsAccount && root.loading && root.sections.length === 0 && root.info === null
    textFormat: Text.PlainText
    text: "Loading…"
    color: Util.alpha(root.foreground, 0.5)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  Text {
    anchors.centerIn: parent
    width: Math.min(implicitWidth, parent.width - Style.spacing.xl * 2)
    visible: !root.needsAccount && root.error !== "" && root.sections.length === 0
    textFormat: Text.PlainText
    text: root.error
    color: Color.urgent
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    wrapMode: Text.WordWrap
    horizontalAlignment: Text.AlignHCenter
  }

  Text {
    anchors.centerIn: parent
    visible: !root.needsAccount && !root.loading && root.error === "" && root.shownKey === root.key
             && root.sections.length === 0 && root.info === null
    textFormat: Text.PlainText
    text: "Nothing here right now."
    color: Util.alpha(root.foreground, 0.5)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  ListView {
    id: list
    anchors.fill: parent
    visible: !root.needsAccount && (root.sections.length > 0 || root.info !== null)
    clip: true
    model: root.sections
    spacing: Style.spacing.xl
    boundsBehavior: Flickable.StopAtBounds
    // A shelf is a few hundred pixels tall: keep the next couple built so
    // a wheel step never lands on an empty band.
    cacheBuffer: Math.max(0, height)
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

    // A show or curator page opens with its own card.
    header: Item {
      width: list.width
      height: root.info ? infoRow.implicitHeight + Style.spacing.xl : 0
      visible: root.info !== null

      Row {
        id: infoRow
        width: parent.width
        spacing: Style.spacing.lg

        Cover {
          id: infoCover
          size: Style.space(112)
          source: root.info ? String(root.info.artwork || "") : ""
          glyph: "󰐹"
          foreground: root.foreground
          accent: root.accent
        }

        Column {
          width: parent.width - infoCover.width - Style.spacing.lg
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
            visible: text !== ""
            textFormat: Text.PlainText
            text: {
              if (!root.info) return ""
              var parts = []
              if (root.info.kind) parts.push(String(root.info.kind))
              if (root.info.artist) parts.push(String(root.info.artist))
              return parts.join("  ·  ")
            }
            color: Util.alpha(root.foreground, 0.6)
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            renderType: Text.NativeRendering
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            visible: text !== ""
            textFormat: Text.PlainText
            text: root.info ? String(root.info.description || "") : ""
            color: Util.alpha(root.foreground, 0.55)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            renderType: Text.NativeRendering
            wrapMode: Text.WordWrap
            maximumLineCount: 3
            elide: Text.ElideRight
          }
        }
      }
    }

    footer: Item { width: list.width; height: Style.spacing.lg }

    // The section from `sections` itself, not the delegate's modelData: a
    // model hands out a converted copy whose arrays are no longer arrays.
    delegate: Shelf {
      required property int index
      width: list.width
      section: root.sections[index] || ({})
      service: root.service
      foreground: root.foreground
      accent: root.accent
      onActivateItem: function(entry) { root.activateItem(entry) }
      onPlayTracks: function(tracks, index, context) { root.playTracks(tracks, index, context) }
      onOpenTarget: function(view, args) { root.open(view, args) }
      onSongMenu: function(song, source, x, y) { root.songMenu(song, source, x, y) }
    }
  }
}
