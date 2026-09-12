import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Ui

// Sustained application bandwidth: COPY and TRIAD count useful reads + writes.
// The Python supervisor terminates the native workers when this panel closes.
Item {
  id: root

  property var shell: null
  property var manifest: null

  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\/(localhost)?/, ""))

  property bool opened: false
  property bool running: false
  property bool expectedStop: false
  property bool pendingRun: false
  property string phase: ""             // "copy" | "triad" | ""
  property string ramName: ""
  property string statusText: ""
  property string triadMBps: ""
  property string copyMBps: ""
  property string error: ""
  property string stderrText: ""

  function open(payloadJson) {
    opened = true
    runTest()
  }

  // Host-initiated close (`shell hide`). The user-initiated paths (Esc, the
  // scrim) route through shell.hide so the host's open-panel state stays
  // consistent, and land back here.
  function close() {
    opened = false
    pendingRun = false
    // Clear the phase before killing the process, so onExited reads the stop
    // as a dismissal rather than a failed run.
    phase = ""
    running = false
    if (proc.running) {
      expectedStop = true
      proc.running = false
    }
  }

  function dismiss() {
    if (shell && typeof shell.hide === "function")
      shell.hide((manifest && manifest.id) || "nixfred.ram-speedtest")
    else close()
  }

  function runTest() {
    if (proc.running) {
      // A dismissal's SIGTERM is still in flight; Process.running stays true
      // until the child exits, so queue the fresh run for onExited.
      if (expectedStop) pendingRun = true
      return
    }
    error = ""
    ramName = ""
    triadMBps = ""
    copyMBps = ""
    statusText = "Preparing sustained memory test"
    stderrText = ""
    phase = ""
    running = true
    proc.running = true
  }

  function toRate(raw) {
    var value = parseFloat(raw)
    return isFinite(value) && value > 0 ? value : 0
  }

  // Scaling is status-only. Each sustained phase finishes with its weighted mean.
  function updateLine(line) {
    var parts = String(line).trim().split(/\s+/)
    if (parts.length < 2) return
    if (parts[0] === "ram") {
      ramName = parts.slice(1).join(" ")
      return
    }
    if (parts[0] === "status") {
      statusText = parts.slice(1).join(" ")
      phase = ""
      return
    }
    var value = parseFloat(parts[1])
    if (!isFinite(value) || value < 0) return
    if (parts[0] === "triad") {
      phase = "triad"
      triadMBps = String(value)
    } else if (parts[0] === "copy") {
      phase = "copy"
      copyMBps = String(value)
    }
  }

  Process {
    id: proc
    command: ["python3", root.pluginDir + "ram-speedtest"]
    stdout: SplitParser { onRead: function(line) { root.updateLine(line) } }
    // Exit and stream-finished have no guaranteed order: when a failed exit
    // beat the collector and published the generic message, replace it with
    // the specific one once it lands.
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.stderrText = String(text || "").trim()
        if (root.error !== "" && root.stderrText !== "") root.error = root.stderrText
      }
    }
    onExited: function(exitCode) {
      if (root.pendingRun) {
        root.pendingRun = false
        root.expectedStop = false
        if (root.opened) Qt.callLater(root.runTest)
        return
      }

      if (!root.expectedStop && exitCode !== 0) {
        root.error = root.stderrText || "RAM speed test failed"
        root.copyMBps = ""
        root.triadMBps = ""
        root.phase = ""
        root.running = false
        return
      }

      root.expectedStop = false
      root.phase = ""
      root.running = false
    }
  }

  SpeedTestOverlay {
    fontFamily: Style.font.family
    layerNamespace: "omarchy-ram-speedtest"
    title: [root.ramName, root.statusText].filter(function(text) { return text !== "" }).join("\n\n")
    leftLabel: "COPY"
    rightLabel: "TRIAD"
    unit: "MB/s"
    runAgainTooltip: "Measure again"
    running: root.running
    leftValue: root.toRate(root.copyMBps)
    rightValue: root.toRate(root.triadMBps)
    leftLive: root.running && root.phase === "copy"
    rightLive: root.running && root.phase === "triad"
    error: root.error
    open: root.opened
    scaleStops: [5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000]
    onCloseRequested: root.dismiss()
    onRunAgainRequested: root.runTest()
  }
}
