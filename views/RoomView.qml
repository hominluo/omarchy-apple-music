import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../components"

// "See All": everything behind one shelf, a page at a time like the
// library. A room of songs is a track list (a click plays the room from
// there); anything else is a grid of cover tiles.
Item {
  id: root

  property var service: null
  property string kind: "room"
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal open(string view, var args)
  signal playTracks(var tracks, int index, var context)
  signal activateItem(var entry)
  signal songMenu(var song, var source, real x, real y)

  // Plain objects for Play; the views read `rows`, which grows by append()
  // so a new page does not reset the scroll (see LibraryView).
  property var items: []
  ListModel { id: rows }
  property string itemsRoom: ""
  property int nextOffset: -1
  property bool loading: false
  property bool loadingMore: false
  property string error: ""
  property string pageTitle: ""
  property int loadToken: 0

  readonly property string roomId: args && args.id ? String(args.id) : ""
  readonly property bool showingItems: root.itemsRoom === root.roomId && root.items.length > 0
  readonly property bool hasMore: root.itemsRoom === root.roomId && root.nextOffset >= 0
  // Decided by what is there: a room of songs only is a list.
  readonly property bool songsOnly: root.items.length > 0 && root.items.every(function(entry) { return entry && entry.type === "song" })

  function nextFrom(result, received) {
    var next = result ? result.next : null
    if (next === null || next === undefined || next === false) return -1
    var offset = Number(next)
    return isFinite(offset) && offset > 0 ? offset : received
  }

  function load() {
    if (!root.service) return
    var room = root.roomId
    if (room === "") { root.error = "Nothing selected"; return }
    var token = ++root.loadToken
    if (root.itemsRoom !== room) {
      root.setItems([])
      root.nextOffset = -1
      root.pageTitle = root.args && root.args.title ? String(root.args.title) : ""
    }
    root.loading = true
    root.loadingMore = false
    root.error = ""
    root.service.room(room, 0, function(ok, result) {
      if (token !== root.loadToken) return
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not load this page"; return }
      var page = result && Array.isArray(result.items) ? result.items : []
      root.setItems(page)
      root.itemsRoom = room
      root.nextOffset = root.nextFrom(result, page.length)
      if (result && result.title) root.pageTitle = String(result.title)
    })
  }

  function loadMore() {
    if (!root.service || root.loading || root.loadingMore || !root.hasMore) return
    var token = root.loadToken
    var room = root.roomId
    var offset = root.nextOffset
    root.loadingMore = true
    root.service.room(room, offset, function(ok, result) {
      if (token !== root.loadToken || room !== root.roomId) return
      root.loadingMore = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not load more"; return }
      var page = result && Array.isArray(result.items) ? result.items : []
      root.appendItems(page)
      root.nextOffset = page.length > 0 ? root.nextFrom(result, offset + page.length) : -1
    })
  }

  function setItems(list) {
    rows.clear()
    root.items = []
    root.appendItems(list)
    songList.positionViewAtBeginning()
    cardGrid.positionViewAtBeginning()
  }

  function appendItems(list) {
    if (list.length === 0) return
    root.items = root.items.concat(list)
    rows.append(list.map(function(entry) { return { entry: entry } }))
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

  onRoomIdChanged: root.scheduleLoad()
  Component.onCompleted: root.scheduleLoad()
  function activated() { root.scheduleLoad() }

  function nowId() {
    return root.service && root.service.nowPlaying ? String(root.service.nowPlaying.id) : ""
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
    visible: !root.loading && root.error === "" && root.itemsRoom === root.roomId && root.items.length === 0
    textFormat: Text.PlainText
    text: "Nothing here right now."
    color: Util.alpha(root.foreground, 0.5)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

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

  Card {
    id: cardProbe
    visible: false
    tileSize: cardGrid.tile
    item: ({ title: "Probe", artist: "Probe" })
  }

  ListView {
    id: songList
    anchors.fill: parent
    visible: root.songsOnly && root.showingItems
    clip: true
    model: root.songsOnly ? rows : []
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
      menuEnabled: true
      current: root.nowId() !== "" && root.nowId() === String(entry.id)
      playing: current && root.service ? root.service.playing : false
      onActivated: root.playTracks(root.items, index, { title: root.pageTitle })
      // The plain object from `items`: a ListModel row is a copy the
      // daemon's JSON would not take.
      onMenuRequested: function(source, x, y) { root.songMenu(root.items[index], source, x, y) }
    }

    onAtYEndChanged: if (atYEnd && root.hasMore) root.loadMore()
  }

  GridView {
    id: cardGrid
    anchors.fill: parent
    visible: !root.songsOnly && root.showingItems
    clip: true
    model: !root.songsOnly ? rows : []
    readonly property real tile: Style.space(140)
    cellWidth: tile + Style.spacing.md
    cellHeight: cardProbe.implicitHeight + Style.spacing.md
    boundsBehavior: Flickable.StopAtBounds
    reuseItems: true
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    footer: Component { MoreFooter { width: cardGrid.width } }

    delegate: Card {
      required property int index
      required property var entry

      tileSize: cardGrid.tile
      item: entry
      foreground: root.foreground
      accent: root.accent
      onActivated: {
        var plain = root.items[index]
        // A song among other kinds plays on its own.
        if (plain.type === "song") root.playTracks([plain], 0, { title: String(plain.title || "") })
        else root.activateItem(plain)
      }
      onMenuRequested: function(source, x, y) {
        var plain = root.items[index]
        if (plain.type === "song") root.songMenu(plain, source, x, y)
      }
    }

    onAtYEndChanged: if (atYEnd && root.hasMore) root.loadMore()
  }
}
