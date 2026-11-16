import QtQuick
import qs.Commons
import qs.Ui

// Shuffle · previous · play/pause · next · repeat.
//
// Shared by the window footer and the bar dropdown so the same five actions
// are always in the same order, at the same sizes, with the same hit areas.
// The host owns the keyboard cursor and tells us which button has it; mouse
// hover reports back through `hovered` so the two never disagree.
Item {
  id: root

  property var service: null
  property color foreground: Color.menu.text
  property color accent: Color.accent
  property color background: Color.menu.background
  property string fontFamily: Style.font.family
  property bool large: false
  property bool hasCursor: false
  property int cursorIndex: 0
  // The key each action answers to where this row is shown, by action name.
  // Each host passes its own: the window and the dropdown bind different
  // keys, and a tooltip must never advertise a key its surface ignores.
  property var keyHints: ({})

  signal hovered(int index)

  readonly property bool hasSong: service ? service.hasSong : false
  readonly property bool playing: service ? service.playing : false
  readonly property bool preparing: service ? service.preparing === true : false
  readonly property bool shuffleOn: service && service.queue ? service.queue.shuffle === true : false
  readonly property string repeatMode: service && service.queue ? String(service.queue.repeat || "off") : "off"
  readonly property var actions: ["shuffle", "previous", "playPause", "next", "repeat"]

  // One height for the side controls, a larger circle for play: the eye
  // reads the row as transport first and shuffle/repeat as modifiers.
  readonly property real sideSize: Style.space(root.large ? 34 : 30)
  readonly property real playSize: Style.space(root.large ? 46 : 38)

  implicitWidth: row.implicitWidth
  implicitHeight: row.implicitHeight

  function run(action) {
    if (!service) return
    switch (action) {
      case "shuffle": service.setShuffle(!root.shuffleOn); break
      case "previous": service.previous(); break
      case "playPause": service.toggle(); break
      case "next": service.next(); break
      case "repeat": service.cycleRepeat(); break
    }
  }

  function runIndex(index) { run(actions[index]) }

  function withHint(label, action) {
    var key = root.keyHints ? root.keyHints[action] : undefined
    return key ? label + "  (" + key + ")" : label
  }

  function repeatLabel() {
    if (root.repeatMode === "one") return "Repeat: this track"
    if (root.repeatMode === "all") return "Repeat: queue"
    return "Repeat: off"
  }

  Row {
    id: row
    spacing: Style.space(root.large ? 8 : 6)

    Repeater {
      model: root.actions

      Button {
        id: control
        required property string modelData
        required property int index

        readonly property bool isPlay: modelData === "playPause"
        readonly property bool on: modelData === "shuffle" ? root.shuffleOn
                                 : (modelData === "repeat" ? root.repeatMode !== "off" : false)
        readonly property bool available: root.hasSong || modelData === "shuffle" || modelData === "repeat"

        foreground: isPlay
          ? (root.hasSong ? root.background : Util.alpha(root.foreground, 0.45))
          : (available ? root.foreground : Util.alpha(root.foreground, 0.32))
        fontFamily: root.fontFamily
        hasCursor: root.hasCursor && root.cursorIndex === index
        selected: !isPlay && on
        bordered: false
        width: isPlay ? root.playSize : root.sideSize
        height: width
        radius: isPlay ? height / 2 : Style.cornerRadius
        horizontalPadding: 0
        verticalPadding: 0
        iconSize: isPlay ? (root.large ? Style.font.display : Style.font.iconLarge)
                         : Style.font.iconLarge
        iconText: {
          if (modelData === "shuffle") return "󰒝"
          if (modelData === "previous") return "󰒮"
          // An hourglass while the picked track is still being prepared:
          // neither play nor pause describes that.
          if (modelData === "playPause") return root.preparing ? "󰔟" : (root.playing ? "󰏤" : "󰐊")
          if (modelData === "next") return "󰒭"
          return root.repeatMode === "one" ? "󰑘" : "󰑖"
        }
        tooltipText: {
          if (modelData === "shuffle") return root.withHint(root.shuffleOn ? "Shuffle: on" : "Shuffle: off", "shuffle")
          if (modelData === "previous") return root.withHint("Previous", "previous")
          if (modelData === "playPause")
            return root.preparing ? (root.service && root.service.isStation ? "Tuning in…" : "Preparing…")
                                  : root.withHint(root.playing ? "Pause" : "Play", "playPause")
          if (modelData === "next") return root.withHint("Next", "next")
          return root.withHint(root.repeatLabel(), "repeat")
        }

        // The play control is a filled circle, not a theme pill: keep its
        // fill across hover (brighten instead of swapping to the shared
        // translucent hover wash) while the side buttons use the kit's states.
        color: isPlay
          ? (root.hasSong ? (hot ? Qt.lighter(root.accent, 1.10) : root.accent)
                          : Util.alpha(root.foreground, 0.10))
          : (hot ? Style.hoverFillFor(root.foreground, root.accent)
                 : (selected ? Style.selectedFillFor(root.foreground, root.accent) : "transparent"))

        onHovered: function(isHovered) { if (isHovered) root.hovered(index) }
        onClicked: root.run(modelData)
      }
    }
  }
}
