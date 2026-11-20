.pragma library

// Ownership of a Voxtype session started from this plugin.
// Closing the prompt cancels a recording only when owned is true.

function ingest(owned, sawActive, state) {
  var nextOwned = !!owned
  var nextSeen = !!sawActive
  if (nextOwned && (state === "recording" || state === "transcribing"))
    nextSeen = true
  if (nextOwned && nextSeen && state === "idle") {
    nextOwned = false
    nextSeen = false
  }
  return { owned: nextOwned, sawActive: nextSeen }
}

function dictateCommand(owned, sawActive, state) {
  if (owned && state === "transcribing")
    return { command: "cancel", owned: false, sawActive: false }
  if (owned)
    return { command: "toggle", owned: true, sawActive: !!sawActive }
  if (state === "idle" || !state)
    return { command: "toggle", owned: true, sawActive: false }
  // A recording we did not start keeps running.
  return { command: "", owned: false, sawActive: false }
}

function cancelCommand(owned, sawActive) {
  if (!owned)
    return { command: "", owned: false, sawActive: !!sawActive }
  return { command: "cancel", owned: false, sawActive: false }
}

function isDictating(owned, state) {
  return !!(owned && (state === "recording" || state === "transcribing"))
}
