import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import QtQuick.Window
import qs.Commons
import qs.Ui
import "dictation.js" as Dictation

Item {
  id: root

  property var shell: null
  property var manifest: null

  property bool opened: false
  property string step: "menu"
  property string promptText: ""
  property int selectedIndex: 0
  property string caption: ""
  property string kind: "desktop"
  property string contextPath: ""
  property string screenshotPath: ""
  property int screenshotGen: 0
  property string lastPrompt: ""
  property bool dictationEnabled: false
  property bool dictationOwned: false
  property bool dictationSawActive: false
  property bool keepCapture: false
  property string voxtypeState: "idle"
  property int clickLocalX: 0
  property int clickLocalY: 0
  property int viewW: 0
  property int viewH: 0
  property string monitorName: ""

  readonly property color menuBackground: Color.menu ? Color.menu.background : Color.background
  readonly property color menuForeground: Color.menu ? Color.menu.text : Color.foreground
  readonly property color popupBorder: Color.popups ? Color.popups.border : Color.accent
  property color background: Util.alpha(root.menuBackground, 0.97)
  property color foreground: root.menuForeground
  property var borderSpec: Border.surfaceSpec("popups", "border", root.popupBorder, Math.max(1, Style.space(2)))
  readonly property int cornerRadius: Style.cornerRadius
  property string fontFamily: Style.font.menuFamily || Style.font.family
  property int contentMargin: Style.spacing.panelPadding
  property int cardWidth: Style.space(340)
  property int menuGap: Style.space(10)
  property int actionHeight: Math.max(Style.space(34), Style.font.body + Style.spacing.controlPaddingY * 2)
  property int thumbHeight: Style.space(128)

  readonly property string pluginId: (root.manifest && root.manifest.id) ? String(root.manifest.id) : ""
  readonly property string pluginDir: {
    var sourceDir = root.manifest && root.manifest.__sourceDir ? String(root.manifest.__sourceDir) : ""
    if (sourceDir.length) return sourceDir
    if (root.pluginId.length)
      return Quickshell.env("HOME") + "/.config/omarchy/plugins/" + root.pluginId
    return ""
  }

  readonly property string actionScript: root.pluginDir + "/scripts/run-action.sh"
  readonly property string screenshotUrl: root.screenshotPath.length ? Util.fileUrl(root.screenshotPath) : ""
  readonly property string defaultContextPath: Quickshell.env("HOME") + "/.local/state/omarchy/desktop-context/context.json"
  readonly property bool dictating: Dictation.isDictating(root.dictationOwned, root.voxtypeState)
  readonly property bool promptStep: root.step === "edit" || root.step === "continue"

  ListModel { id: actionModel }

  Process {
    id: voxtypeStatus
    command: ["voxtype", "status", "--follow", "--format", "json"]
    running: root.opened && root.dictationEnabled && root.promptStep
    stdout: SplitParser {
      onRead: function(line) {
        try {
          var data = JSON.parse(String(line || ""))
          var state = String(data.alt || data.class || "idle")
          var tracked = Dictation.ingest(root.dictationOwned, root.dictationSawActive, state)
          root.dictationOwned = tracked.owned
          root.dictationSawActive = tracked.sawActive
          root.voxtypeState = state
        } catch (e) {}
      }
    }
    onRunningChanged: if (!running) root.voxtypeState = "idle"
  }

  function screenForName(name) {
    var screens = Quickshell.screens
    if (!screens || !screens.length) return null
    for (var i = 0; i < screens.length; i++) {
      if (name && screens[i].name === name) return screens[i]
    }
    return null
  }

  function screenForPayload(monitor) {
    var name = monitor && monitor.name ? String(monitor.name) : ""
    var named = root.screenForName(name)
    if (named) return named
    var screens = Quickshell.screens
    if (!screens || !screens.length) return null
    var width = monitor && monitor.width ? Math.round(Number(monitor.width)) : 0
    var height = monitor && monitor.height ? Math.round(Number(monitor.height)) : 0
    if (width > 0 && height > 0) {
      for (var i = 0; i < screens.length; i++) {
        if (Math.round(screens[i].width) === width && Math.round(screens[i].height) === height)
          return screens[i]
      }
    }
    return screens[0]
  }

  readonly property string headingText: {
    if (root.step === "edit") return "Edit"
    if (root.step === "continue") return "Continue"
    if (root.step === "undo") return "Undo"
    return "Context Deployment"
  }

  function coercePayload(payloadJson) {
    if (!payloadJson) return ({})
    if (typeof payloadJson === "object") return payloadJson
    try { return JSON.parse(payloadJson) } catch (e) { return ({}) }
  }

  function defaultActions() {
    return [
      { actionId: "edit", label: "Edit", hint: "Change this with your default agent" },
      { actionId: "inspect", label: "What is this?", hint: "Explain this without changing anything" }
    ]
  }

  function applyPayload(next) {
    if (!next || typeof next !== "object") next = ({})
    root.stopDictation()
    root.discardCapture()
    root.keepCapture = false

    var actions = Array.isArray(next.actions) && next.actions.length ? next.actions : root.defaultActions()
    actionModel.clear()
    for (var i = 0; i < actions.length; i++) {
      var rawId = actions[i].actionId || ""
      if (!rawId) continue
      actionModel.append({
        actionId: String(rawId),
        label: String(actions[i].label || "Action"),
        hint: String(actions[i].hint || "")
      })
    }
    if (actionModel.count === 0) {
      var fallback = root.defaultActions()
      for (var j = 0; j < fallback.length; j++) {
        actionModel.append(fallback[j])
      }
    }

    if (next.caption) root.caption = String(next.caption)
    if (next.kind) root.kind = String(next.kind)
    if (next.contextPath) root.contextPath = String(next.contextPath)
    else if (!root.contextPath.length) root.contextPath = root.defaultContextPath

    var shot = next.screenshot ? String(next.screenshot) : ""
    root.screenshotGen += 1
    var shotGen = root.screenshotGen
    if (shot) {
      root.screenshotPath = ""
      Qt.callLater(function() {
        if (root.screenshotGen === shotGen)
          root.screenshotPath = shot
      })
    } else {
      root.screenshotPath = ""
    }

    if (next.lastPrompt !== undefined) root.lastPrompt = String(next.lastPrompt || "")
    if (next.dictation !== undefined) root.dictationEnabled = next.dictation === true
    root.dictationOwned = false
    root.dictationSawActive = false
    root.voxtypeState = "idle"

    if (next.local && next.local.x !== undefined) {
      var nx = Math.round(Number(next.local.x))
      root.clickLocalX = isFinite(nx) ? nx : 0
    } else if (!next.local) {
      root.clickLocalX = 0
    }
    if (next.local && next.local.y !== undefined) {
      var ny = Math.round(Number(next.local.y))
      root.clickLocalY = isFinite(ny) ? ny : 0
    } else if (!next.local) {
      root.clickLocalY = 0
    }

    var nextScreen = root.screenForPayload(next.monitor)
    if (nextScreen) {
      panel.screen = nextScreen
      root.monitorName = nextScreen.name || root.monitorName
      root.viewW = Math.round(nextScreen.width)
      root.viewH = Math.round(nextScreen.height)
    } else if (next.monitor && next.monitor.width && next.monitor.height) {
      var screens = Quickshell.screens
      if (screens && screens.length)
        panel.screen = screens[0]
      root.monitorName = String(next.monitor.name || "")
      root.viewW = Math.round(Number(next.monitor.width))
      root.viewH = Math.round(Number(next.monitor.height))
    } else {
      var fallbackScreens = Quickshell.screens
      if (fallbackScreens && fallbackScreens.length) {
        panel.screen = fallbackScreens[0]
        root.viewW = Math.round(fallbackScreens[0].width)
        root.viewH = Math.round(fallbackScreens[0].height)
      }
    }

    root.step = "menu"
    root.promptText = ""
    root.selectedIndex = 0
    if (root.opened) {
      root.opened = false
      Qt.callLater(function() { root.opened = true })
    } else {
      root.opened = true
    }
  }

  function open(payloadJson) {
    root.applyPayload(root.coercePayload(payloadJson))
  }

  function stopDictation() {
    var result = Dictation.cancelCommand(root.dictationOwned, root.dictationSawActive)
    root.dictationOwned = result.owned
    root.dictationSawActive = result.sawActive
    if (result.command === "cancel") {
      Quickshell.execDetached(["voxtype", "record", "cancel"])
      root.voxtypeState = "idle"
    }
  }

  function discardCapture() {
    if (root.keepCapture) return
    if (!root.contextPath.length || !root.pluginDir.length) return
    Quickshell.execDetached([
      "python3",
      root.pluginDir + "/scripts/context.py",
      "--discard",
      root.contextPath
    ])
  }

  function close() {
    root.stopDictation()
    root.discardCapture()
    root.opened = false
    root.step = "menu"
    root.promptText = ""
    root.voxtypeState = "idle"
  }

  function dismiss() {
    root.close()
    if (root.pluginId.length && root.shell && typeof root.shell.hide === "function")
      root.shell.hide(root.pluginId)
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  function currentHint() {
    if (root.selectedIndex < 0 || root.selectedIndex >= actionModel.count) return ""
    return actionModel.get(root.selectedIndex).hint || ""
  }

  function activateIndex(index) {
    if (index < 0 || index >= actionModel.count) return
    var action = actionModel.get(index)
    if (!action) return
    root.activateAction(action.actionId)
  }

  function activateAction(actionId) {
    var id = String(actionId || "")
    if (id === "edit") {
      root.promptText = ""
      root.step = "edit"
      return
    }
    if (id === "continue") {
      root.promptText = ""
      root.step = "continue"
      return
    }
    if (id === "inspect") {
      root.runAction(["inspect", root.contextPath])
      root.dismiss()
      return
    }
    if (id === "undo") {
      root.step = "undo"
    }
  }

  function runAction(args) {
    if (!root.pluginDir.length) return
    root.keepCapture = true
    Util.execArgv([root.actionScript].concat(args))
  }

  function submitPrompt() {
    var text = root.promptText.trim()
    if (!text || root.dictating) return
    var mode = root.step === "continue" ? "continue" : "edit"
    root.stopDictation()
    root.runAction([mode, text, root.contextPath])
    root.dismiss()
  }

  function submitUndo() {
    root.runAction(["undo"])
    root.dismiss()
  }

  function toggleDictation() {
    if (!root.dictationEnabled) return
    promptField.forceActiveFocus()
    var result = Dictation.dictateCommand(root.dictationOwned, root.dictationSawActive, root.voxtypeState)
    root.dictationOwned = result.owned
    root.dictationSawActive = result.sawActive
    if (result.command === "cancel")
      Quickshell.execDetached(["voxtype", "record", "cancel"])
    else if (result.command === "toggle")
      Quickshell.execDetached(["voxtype", "record", "toggle"])
    Qt.callLater(function() { promptField.forceActiveFocus() })
  }

  function dictationLabel() {
    if (root.dictationOwned && root.voxtypeState === "recording") return "Stop dictation"
    if (root.dictationOwned && root.voxtypeState === "transcribing") return "Transcribing…"
    return "Dictate"
  }

  function backToMenu() {
    root.stopDictation()
    root.step = "menu"
    root.promptText = ""
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function clampX(nx, cardW) {
    var margin = Style.gapsOut
    var view = stage.width > 0 ? stage.width : root.viewW
    if (view > 0 && nx + cardW > view - margin)
      nx = Math.max(margin, view - cardW - margin)
    if (nx < margin) nx = margin
    return Math.round(nx)
  }

  function clampY(ny, cardH) {
    var margin = Style.gapsOut
    var view = stage.height > 0 ? stage.height : root.viewH
    if (view > 0 && ny + cardH > view - margin)
      ny = root.clickLocalY - cardH - root.menuGap
    if (ny < margin) ny = margin
    return Math.round(ny)
  }

  PanelWindow {
    id: panel
    visible: root.opened
    color: "transparent"
    implicitWidth: root.viewW
    implicitHeight: root.viewH
    WlrLayershell.namespace: "omarchy-desktop-context"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: visible ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    anchors.left: true
    anchors.right: true
    anchors.top: true
    anchors.bottom: true

    onVisibleChanged: if (visible) Qt.callLater(function() { keyCatcher.forceActiveFocus() })

    Item {
      id: stage
      anchors.fill: parent

      MouseArea {
        anchors.fill: parent
        onClicked: root.dismiss()
      }

      BorderSurface {
        id: card
        width: root.cardWidth
        height: inner.implicitHeight + contentTopInset + contentBottomInset
        radius: root.cornerRadius
        color: root.background
        borderSpec: root.borderSpec
        padding: root.contentMargin
        x: root.clampX(root.clickLocalX, root.cardWidth)
        y: root.clampY(root.clickLocalY + root.menuGap, height)

        MouseArea {
          anchors.fill: parent
          z: -1
          onClicked: {}
        }

        Item {
          id: keyCatcher
          anchors.fill: parent
          focus: true

          Keys.priority: Keys.BeforeItem
          Keys.onPressed: function(event) {
            if (root.promptStep) {
              if (event.key === Qt.Key_Escape) {
                root.dismiss()
                event.accepted = true
              }
              return
            }

            if (root.step === "undo") {
              if (event.key === Qt.Key_Escape) {
                root.dismiss()
                event.accepted = true
              } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                root.submitUndo()
                event.accepted = true
              }
              return
            }

            if (event.key === Qt.Key_Escape) {
              root.dismiss()
              event.accepted = true
            } else if (event.key === Qt.Key_Down) {
              root.selectedIndex = (root.selectedIndex + 1) % Math.max(1, actionModel.count)
              event.accepted = true
            } else if (event.key === Qt.Key_Up) {
              root.selectedIndex = (root.selectedIndex - 1 + actionModel.count) % Math.max(1, actionModel.count)
              event.accepted = true
            } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
              root.activateIndex(root.selectedIndex)
              event.accepted = true
            }
          }
        }

        Column {
          id: inner
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: parent.top
          anchors.topMargin: card.contentTopInset
          anchors.leftMargin: card.contentLeftInset
          anchors.rightMargin: card.contentRightInset
          spacing: Style.spacing.sm

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.headingText
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            font.bold: true
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.caption
            color: root.foreground
            opacity: 0.62
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          Rectangle {
            id: thumbFrame
            width: parent.width
            height: root.thumbHeight
            radius: Math.max(2, root.cornerRadius - 4)
            color: Util.alpha(root.foreground, 0.08)
            clip: true
            visible: root.screenshotUrl.length > 0 && thumb.status !== Image.Error

            Image {
              id: thumb
              anchors.fill: parent
              source: root.screenshotUrl
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
              cache: false
              smooth: true
              sourceSize.width: width * Screen.devicePixelRatio
              sourceSize.height: height * Screen.devicePixelRatio
            }
          }

          Column {
            width: parent.width
            spacing: Style.spacing.xs
            visible: root.step === "menu"

            Repeater {
              model: actionModel
              Button {
                required property int index
                required property string actionId
                required property string label
                width: inner.width
                height: root.actionHeight
                leftAlign: true
                text: label
                selected: root.selectedIndex === index
                onClicked: root.activateAction(actionId)
                onHovered: function(isHovered) {
                  if (isHovered) root.selectedIndex = index
                }
              }
            }
          }

          Text {
            width: parent.width
            visible: root.step === "menu"
            textFormat: Text.PlainText
            text: root.currentHint()
            color: root.foreground
            opacity: 0.55
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          Column {
            width: parent.width
            spacing: Style.spacing.sm
            visible: root.promptStep

            Text {
              width: parent.width
              visible: root.step === "continue" && root.lastPrompt.length > 0
              textFormat: Text.PlainText
              text: "Last: " + root.lastPrompt
              color: root.foreground
              opacity: 0.55
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.Wrap
              maximumLineCount: 3
              elide: Text.ElideRight
            }

            TextField {
              id: promptField
              width: parent.width
              placeholderText: root.step === "continue" ? "What next?" : "What should change?"
              onTextChanged: root.promptText = text
              onAccepted: root.submitPrompt()
              onVisibleChanged: if (visible) {
                text = root.promptText
                forceActiveFocus()
              }
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) {
                  root.dismiss()
                  event.accepted = true
                }
              }
            }

            Button {
              width: parent.width
              height: root.actionHeight
              visible: root.dictationEnabled
              text: root.dictationLabel()
              leftAlign: true
              active: root.dictating
              onClicked: root.toggleDictation()
            }

            Button {
              width: parent.width
              height: root.actionHeight
              text: "Send to agent"
              leftAlign: true
              enabled: root.promptText.trim().length > 0 && !root.dictating
              onClicked: root.submitPrompt()
            }
          }

          Column {
            width: parent.width
            spacing: Style.spacing.sm
            visible: root.step === "undo"

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: "Restore files from the last agent edit? Files you changed again are kept."
              color: root.foreground
              opacity: 0.72
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.Wrap
            }

            Button {
              width: parent.width
              height: root.actionHeight
              text: "Restore"
              leftAlign: true
              onClicked: root.submitUndo()
            }

            Button {
              width: parent.width
              height: root.actionHeight
              text: "Cancel"
              leftAlign: true
              onClicked: root.backToMenu()
            }
          }
        }
      }
    }
  }

  onVoxtypeStateChanged: {
    if (root.promptStep && root.dictating)
      Qt.callLater(function() { promptField.forceActiveFocus() })
  }
}
