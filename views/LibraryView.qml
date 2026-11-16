import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../components"

// The saved library: recently added, songs, albums, playlists, artists.
//
// Apple hands the library out a page at a time (100 items at most), so the
// list grows as it is scrolled: reaching the end asks for the next page, and
// a "Load more" row does the same for anyone who would rather click.
Item {
  id: root

  property var service: null
  property string kind: "songs"
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal open(string view, var args)
  signal playTracks(var tracks, int index, var context)
  signal activateItem(var entry)
  signal songMenu(var song, var source, real x, real y)

  // The loaded items, for Play (which hands the daemon the plain objects)
  // and the counts. The views read `rows` instead: a ListModel that grows by
  // append() gets rows inserted, where a reassigned array resets the view
  // and threw the user back to the top on every page.
  property var items: []
  ListModel { id: rows }
  // The kind `items` belongs to. The Loader keeps this item when only the
  // kind changes, so without it a songs page could render as album cards
  // (or the reverse) until the new answer arrives.
  property string itemsKind: ""
  // Offset of the next page, or -1 when the last page is in.
  property int nextOffset: -1
  property bool loading: false
  property string loadingKind: ""
  property bool loadingMore: false
  property string error: ""
  property bool signedIn: service ? service.signedIn : false

  // Every load takes a new token; an answer whose token is stale belongs to
  // a kind (or a refresh) the user has already moved past.
  property int loadToken: 0

  readonly property bool showingItems: root.itemsKind === root.kind && root.items.length > 0
  readonly property bool hasMore: root.itemsKind === root.kind && root.nextOffset >= 0

  readonly property string title: kind === "songs" ? "Songs"
    : kind === "albums" ? "Albums"
    : kind === "playlists" ? "Playlists"
    : kind === "recently-added" ? "Recently Added" : "Artists"

  function nextFrom(result, received) {
    var next = result ? result.next : null
    if (next === null || next === undefined || next === false) return -1
    var offset = Number(next)
    return isFinite(offset) && offset > 0 ? offset : received
  }

  function load() {
    if (!root.service) return
    var kind = root.kind
    // Opening the view asks twice (onCompleted, then activated); one answer
    // for the kind on screen is enough. A different kind always reloads.
    if (root.loading && root.loadingKind === kind) return
    var token = ++root.loadToken
    root.loadingKind = kind
    if (root.itemsKind !== kind) {
      root.setItems([])
      root.nextOffset = -1
    }
    root.loading = true
    root.loadingMore = false
    root.error = ""
    root.service.library(kind, 0, function(ok, result) {
      if (token !== root.loadToken) return
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not load the library"; return }
      var page = result && Array.isArray(result.items) ? result.items : []
      root.setItems(page)
      root.itemsKind = kind
      root.nextOffset = root.nextFrom(result, page.length)
    })
  }

  function loadMore() {
    if (!root.service || root.loading || root.loadingMore || !root.hasMore) return
    var token = root.loadToken
    var kind = root.kind
    var offset = root.nextOffset
    root.loadingMore = true
    root.service.library(kind, offset, function(ok, result) {
      if (token !== root.loadToken || kind !== root.kind) return
      root.loadingMore = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not load more"; return }
      var page = result && Array.isArray(result.items) ? result.items : []
      root.appendItems(page)
      root.nextOffset = page.length > 0 ? root.nextFrom(result, offset + page.length) : -1
    })
  }

  // A fresh first page: the list starts over, and so does the scroll, which
  // a clear() alone would leave past the end of the shorter list.
  function setItems(list) {
    rows.clear()
    root.items = []
    root.appendItems(list)
    songList.positionViewAtBeginning()
    cardGrid.positionViewAtBeginning()
  }

  function appendItems(list) {
    if (list.length === 0) return
    // Reassigned, not pushed, so bindings on `items` see the change.
    root.items = root.items.concat(list)
    rows.append(list.map(function(entry) { return { entry: entry } }))
  }

  onKindChanged: {
    songList.contentY = 0
    cardGrid.contentY = 0
    root.load()
  }
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
    visible: root.loading && !root.showingItems
    textFormat: Text.PlainText
    text: "Loading…"
    color: Util.alpha(root.foreground, 0.5)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  Text {
    anchors.centerIn: parent
    visible: root.error !== "" && !root.showingItems
    textFormat: Text.PlainText
    text: root.error
    color: Color.urgent
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  Text {
    anchors.centerIn: parent
    visible: root.signedIn && !root.loading && root.error === "" && root.itemsKind === root.kind && root.items.length === 0
    textFormat: Text.PlainText
    text: "Nothing here yet."
    color: Util.alpha(root.foreground, 0.5)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  // The footer row under both lists: more to fetch, or why it stopped.
  component MoreFooter: Item {
    height: visible ? Style.space(52) : 0
    visible: root.hasMore || (root.error !== "" && root.showingItems)

    Text {
      anchors.centerIn: parent
      visible: root.loadingMore
      textFormat: Text.PlainText
      text: "Loading more…"
      color: Util.alpha(root.foreground, 0.5)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
    }

    Button {
      anchors.centerIn: parent
      visible: !root.loadingMore && (root.hasMore || root.error !== "")
      foreground: root.foreground
      fontFamily: Style.font.family
      fontSize: Style.font.bodySmall
      bordered: true
      text: root.error !== "" ? "Try again" : "Load more"
      onClicked: {
        root.error = ""
        if (root.hasMore) root.loadMore()
        else root.load()
      }
    }
  }

  // Sizes the grid's cells from a real card, so a theme's fonts never clip
  // the second line of text.
  Card {
    id: cardProbe
    visible: false
    tileSize: cardGrid.tile
    item: ({ title: "Probe", artist: "Probe" })
  }

  // A library can hold thousands of songs; a ListView builds only the rows
  // on screen, where a Column of TrackRows built every one up front.
  ListView {
    id: songList
    anchors.fill: parent
    visible: root.kind === "songs" && root.showingItems
    clip: true
    model: root.kind === "songs" && root.itemsKind === "songs" ? rows : []
    spacing: Style.spacing.xs
    boundsBehavior: Flickable.StopAtBounds
    reuseItems: true
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    footer: Component { MoreFooter { width: songList.width } }

    delegate: TrackRow {
      required property int index
      required property var entry

      width: songList.width
      song: entry
      number: index + 1
      foreground: root.foreground
      accent: root.accent
      current: root.service && root.service.nowPlaying
               ? String(root.service.nowPlaying.id) === String(entry.id) : false
      playing: current && root.service ? root.service.playing : false
      menuEnabled: true
      // The queue is the songs loaded so far; the rest join it as they load
      // only if Play is pressed again, which the "Load more" row makes plain.
      onActivated: root.playTracks(root.items, index, { title: "Library" })
      // The plain object from `items`: a ListModel row is a copy the
      // daemon's JSON would not take.
      onMenuRequested: function(source, x, y) { root.songMenu(root.items[index], source, x, y) }
    }

    onAtYEndChanged: if (atYEnd && root.hasMore) root.loadMore()
  }

  GridView {
    id: cardGrid
    anchors.fill: parent
    visible: root.kind !== "songs" && root.showingItems
    clip: true
    model: root.kind !== "songs" && root.itemsKind === root.kind ? rows : []
    readonly property real tile: Style.space(140)
    cellWidth: tile + Style.spacing.md
    cellHeight: cardProbe.implicitHeight + Style.spacing.md
    boundsBehavior: Flickable.StopAtBounds
    reuseItems: true
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    footer: Component { MoreFooter { width: cardGrid.width } }

    // Recently Added mixes albums, playlists and the odd station, so the
    // window routes each by its type.
    delegate: Card {
      required property int index
      required property var entry

      tileSize: cardGrid.tile
      item: entry
      foreground: root.foreground
      accent: root.accent
      onActivated: root.activateItem(root.items[index])
    }

    onAtYEndChanged: if (atYEnd && root.hasMore) root.loadMore()
  }
}
