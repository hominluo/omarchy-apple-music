import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui
import "../components"

// One album, playlist or artist, opened from a card or a search result.
//
// An album or playlist is its header (Play, Shuffle, Add to Library,
// Favorite, the editorial blurb) over its track list. An artist is the bio
// over the page Apple builds for them (Top Songs, Albums, Singles & EPs,
// Appears On, Similar Artists…); a library artist has no such page, only its
// albums, which keep the plain grid.
Item {
  id: root

  property var service: null
  property string kind: "album"          // album | playlist | artist
  property var args: ({})
  property color foreground: Color.menu.text
  property color accent: Color.accent

  signal open(string view, var args)
  signal playTracks(var tracks, int index, var context)
  signal activateItem(var entry)
  signal songMenu(var song, var source, real x, real y)
  signal notify(string message, bool urgent)

  property var info: null
  property var tracks: []
  property var sections: []

  // Library playlists carry no artwork of their own; the first track's
  // cover is what the web player shows, so use it as the fallback.
  readonly property string artwork: {
    if (root.info && root.info.artwork) return String(root.info.artwork)
    if (root.tracks.length > 0 && root.tracks[0].artwork) return String(root.tracks[0].artwork)
    return ""
  }
  property var albums: []
  // The engine stops paging a very long playlist (or an artist's albums)
  // at a cap and says so; the list on screen is then not the whole thing.
  property bool truncated: false
  property bool loading: false
  property string error: ""

  property int loadToken: 0

  readonly property bool isArtist: root.kind === "artist"
  readonly property bool isLibrary: (root.args && root.args.library === true) || (root.info && root.info.library === true)
  // The artist page proper, when Apple has one; else the album grid.
  readonly property bool showsSections: root.isArtist && root.sections.length > 0
  // What Play starts: the tracks, or an artist's top songs.
  readonly property var playable: {
    if (!root.isArtist) return root.tracks
    for (var i = 0; i < root.sections.length; i++) {
      var section = root.sections[i]
      if (section && section.kind === "songs" && Array.isArray(section.items) && section.items.length > 0)
        return section.items
    }
    return []
  }
  // Ratings and the library are keyed by the catalogue id where there is
  // one; a library item without one is asked about by its own id.
  readonly property string catalogId: {
    if (!root.info) return ""
    if (root.info.catalogId) return String(root.info.catalogId)
    return root.isLibrary ? "" : String(root.info.id || root.args.id || "")
  }
  readonly property string ratingId: root.catalogId !== "" ? root.catalogId
                                     : (root.info ? String(root.info.id || "") : "")
  // 1 favourite, -1 suggest less, 0 neither, null not known (yet).
  property var rating: null
  property bool added: false

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
    root.sections = []
    root.truncated = false
    root.rating = null
    root.added = false
    var done = function(ok, result) {
      if (token !== root.loadToken) return
      // The Loader keeps this item alive when only the id changes, so the
      // previous album's scroll offset would otherwise carry over.
      scroll.contentY = 0
      shelfList.contentY = 0
      root.loading = false
      if (!ok) { root.error = result && result.message ? String(result.message) : "Could not load"; return }
      root.truncated = result.truncated === true
      root.info = result.info || null
      if (root.kind === "artist") {
        root.albums = result.albums || []
        root.sections = Array.isArray(result.sections)
          ? result.sections.filter(function(section) { return section && Array.isArray(section.items) && section.items.length > 0 })
          : []
        root.tracks = []
      } else {
        root.tracks = result.tracks || []
        root.albums = []
        root.sections = []
        root.loadRating(token)
      }
    }
    if (root.kind === "artist") root.service.artistDetail(id, isLibrary, done)
    else if (root.kind === "playlist") root.service.playlistDetail(id, isLibrary, done)
    else root.service.albumDetail(id, isLibrary, done)
  }

  function loadRating(token) {
    if (!root.service || root.ratingId === "" || root.isArtist) return
    root.service.rating(root.kind, root.ratingId, function(ok, result) {
      if (token !== root.loadToken || !ok) return
      root.rating = result ? Number(result.value) || 0 : 0
    })
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

  onKindChanged: root.scheduleLoad()
  onArgsChanged: root.scheduleLoad()
  Component.onCompleted: root.scheduleLoad()
  function activated() { root.scheduleLoad() }

  function context() {
    return {
      title: root.info ? String(root.info.title || "") : "",
      artwork: root.artwork
    }
  }

  function playAll() {
    if (root.playable.length > 0) root.playTracks(root.playable, 0, root.context())
  }

  // Shuffle on first, so the daemon shuffles the queue it is handed.
  function shuffleAll() {
    if (!root.service || root.playable.length === 0) return
    root.service.setShuffle(true)
    root.playTracks(root.playable, 0, root.context())
  }

  function toggleFavorite() {
    if (!root.service || root.ratingId === "") return
    var token = root.loadToken
    var value = root.rating === 1 ? 0 : 1
    var before = root.rating
    root.rating = value
    root.service.rate(root.kind, root.ratingId, value, function(ok, result) {
      if (token !== root.loadToken) return
      if (!ok) {
        root.rating = before
        root.notify(result && result.message ? String(result.message) : "Could not change the favourite", true)
        return
      }
      root.rating = result && result.value !== undefined ? Number(result.value) || 0 : value
    })
  }

  function addToLibrary() {
    if (!root.service || root.catalogId === "") return
    var token = root.loadToken
    var name = root.info ? String(root.info.title || "") : ""
    root.service.addToLibrary(root.kind, root.catalogId, function(ok, result) {
      if (!ok) {
        root.notify(result && result.message ? String(result.message) : "Could not add to your library", true)
        return
      }
      if (token === root.loadToken) root.added = true
      root.notify("Added " + (name !== "" ? name : "it") + " to your library", false)
    })
  }

  function startStation() {
    if (!root.service || root.catalogId === "") return
    root.service.createStation(root.catalogId, "artist", function(ok, result) {
      if (!ok) root.notify(result && result.message ? String(result.message) : "Could not start the station", true)
    })
  }

  function nowId() {
    return root.service && root.service.nowPlaying ? String(root.service.nowPlaying.id) : ""
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

    Item {
      id: headerBox
      width: parent.width
      height: Math.max(headerCover.height, headerText.implicitHeight)

      Cover {
        id: headerCover
        size: Style.space(132)
        source: root.artwork
        glyph: root.isArtist ? "󰠃" : "󰝚"
        foreground: root.foreground
        accent: root.accent
      }

      Column {
        id: headerText
        anchors.left: headerCover.right
        anchors.leftMargin: Style.spacing.lg
        anchors.right: parent.right
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
            if (root.isArtist) {
              // The page itself says what there is; the count is for the
              // plain grid only.
              if (root.showsSections) return root.info.genre ? String(root.info.genre) : ""
              var count = root.albums.length || 0
              return count === 1 ? "1 album" : count + (root.truncated ? "+ albums" : " albums")
            }
            var parts = []
            if (root.info.artist) parts.push(String(root.info.artist))
            if (root.info.year) parts.push(String(root.info.year))
            var trackCount = root.kind === "playlist" ? (root.tracks.length || 0) : Number(root.info.trackCount || 0)
            if (trackCount > 0)
              parts.push(trackCount === 1 ? "1 track" : trackCount + (root.truncated && root.kind === "playlist" ? "+ tracks" : " tracks"))
            return parts.join("  ·  ")
          }
          color: Util.alpha(root.foreground, 0.6)
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          renderType: Text.NativeRendering
          elide: Text.ElideRight
        }

        // The editorial note, or an artist's bio: a taste of it, not the
        // essay, so the tracks stay on screen.
        Text {
          width: parent.width
          visible: text !== ""
          textFormat: Text.PlainText
          text: root.info ? String(root.info.description || "").replace(/\s+/g, " ").trim() : ""
          color: Util.alpha(root.foreground, 0.55)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          renderType: Text.NativeRendering
          wrapMode: Text.WordWrap
          maximumLineCount: root.isArtist ? 3 : 2
          elide: Text.ElideRight
        }

        Text {
          width: parent.width
          visible: root.truncated && !root.showsSections
          textFormat: Text.PlainText
          text: root.isArtist
                ? "Showing the first " + root.albums.length + " albums; Apple has more than the player fetches."
                : "Showing the first " + root.tracks.length + " tracks; the rest are not loaded, so Play stops there."
          color: Util.alpha(root.foreground, 0.5)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          renderType: Text.NativeRendering
          wrapMode: Text.WordWrap
          maximumLineCount: 2
          elide: Text.ElideRight
        }

        Item { width: 1; height: Style.spacing.xs }

        Row {
          spacing: Style.spacing.md

          Rectangle {
            visible: root.playable.length > 0
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

          Button {
            visible: root.playable.length > 1
            height: Style.space(34)
            bordered: true
            foreground: root.foreground
            accent: root.accent
            fontFamily: Style.font.family
            fontSize: Style.font.body
            iconText: "󰒝"
            text: "Shuffle"
            onClicked: root.shuffleAll()
          }

          // An artist station: Apple's "Create Station" on the artist page.
          Button {
            visible: root.isArtist && root.catalogId !== ""
            height: Style.space(34)
            bordered: true
            foreground: root.foreground
            accent: root.accent
            fontFamily: Style.font.family
            fontSize: Style.font.body
            iconText: "󰐹"
            text: "Station"
            tooltipText: "Play a station of this artist and music like theirs"
            onClicked: root.startStation()
          }

          Button {
            visible: !root.isArtist && !root.isLibrary && root.catalogId !== ""
            height: Style.space(34)
            bordered: true
            foreground: root.foreground
            accent: root.accent
            fontFamily: Style.font.family
            fontSize: Style.font.body
            iconText: root.added ? "󰄬" : "󰐕"
            text: root.added ? "Added" : "Add to Library"
            enabled: !root.added
            onClicked: root.addToLibrary()
          }

          Button {
            visible: !root.isArtist && root.ratingId !== ""
            height: Style.space(34)
            width: height
            horizontalPadding: 0
            bordered: true
            foreground: root.rating === 1 ? root.accent : root.foreground
            accent: root.accent
            fontFamily: Style.font.family
            iconSize: Style.font.iconLarge
            iconText: root.rating === 1 ? "󰋑" : "󰋕"
            tooltipText: root.rating === 1 ? "Remove from Favorites" : "Favorite"
            onClicked: root.toggleFavorite()
          }
        }
      }
    }

    // A library playlist can hold a thousand tracks, so the list is a
    // ListView: only the visible rows are instantiated.
    ListView {
      id: scroll
      width: parent.width
      height: Math.max(0, parent.height - headerBox.height - Style.spacing.lg)
      visible: !root.showsSections
      clip: true
      model: root.isArtist ? [] : root.tracks
      spacing: Style.spacing.xs
      boundsBehavior: Flickable.StopAtBounds
      reuseItems: true
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

      delegate: TrackRow {
        required property int index
        required property var modelData

        width: scroll.width
        song: modelData
        number: index + 1
        foreground: root.foreground
        accent: root.accent
        menuEnabled: true
        current: root.nowId() !== "" && root.nowId() === String(modelData.id)
        playing: current && root.service ? root.service.playing : false
        stage: root.service && root.service.buildStages && root.service.buildStages[modelData.id]
               ? String(root.service.buildStages[modelData.id].stage || "") : ""
        onActivated: root.playTracks(root.tracks, index, root.context())
        onMenuRequested: function(source, x, y) { root.songMenu(root.tracks[index], source, x, y) }
      }

      // A library artist: its albums, as before Apple's artist pages.
      footer: Flow {
        width: scroll.width
        spacing: Style.spacing.md
        visible: root.isArtist

        Repeater {
          model: root.isArtist ? root.albums : []

          Card {
            tileSize: Style.space(140)
            item: modelData
            foreground: root.foreground
            accent: root.accent
            onActivated: root.activateItem(root.albums[index])
          }
        }
      }
    }

    // A catalogue artist: Apple's page, shelf by shelf.
    ListView {
      id: shelfList
      width: parent.width
      height: Math.max(0, parent.height - headerBox.height - Style.spacing.lg)
      visible: root.showsSections
      clip: true
      model: root.showsSections ? root.sections : []
      spacing: Style.spacing.xl
      boundsBehavior: Flickable.StopAtBounds
      cacheBuffer: Math.max(0, height)
      ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
      footer: Item { width: shelfList.width; height: Style.spacing.lg }

      // From `sections` itself: a model's modelData is a converted copy.
      delegate: Shelf {
        required property int index
        width: shelfList.width
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
}
