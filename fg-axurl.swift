import Cocoa
import ApplicationServices

func attr(_ element: AXUIElement, _ key: String) -> AnyObject? {
    var value: AnyObject?
    let result = AXUIElementCopyAttributeValue(element, key as CFString, &value)
    return result == .success ? value : nil
}

func findURL(_ element: AXUIElement, depth: Int) -> String? {
    if depth > 12 { return nil }
    if let url = attr(element, kAXURLAttribute as String) as? URL {
        return url.absoluteString
    }
    if let role = attr(element, kAXRoleAttribute as String) as? String,
       role == "AXTextField",
       let value = attr(element, kAXValueAttribute as String) as? String,
       value.contains("://") || value.contains(".") {
        return value
    }
    if let children = attr(element, kAXChildrenAttribute as String) as? [AXUIElement] {
        for child in children {
            if let found = findURL(child, depth: depth + 1) { return found }
        }
    }
    return nil
}

if CommandLine.arguments.contains("--prompt") {
    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
    let trusted = AXIsProcessTrustedWithOptions(options as CFDictionary)
    FileHandle.standardError.write("trusted=\(trusted)\n".data(using: .utf8)!)
    exit(trusted ? 0 : 1)
}

let frontApp = NSWorkspace.shared.frontmostApplication
let appName = frontApp?.localizedName ?? ""

var url = ""
var title = ""

if let pid = frontApp?.processIdentifier {
    let appElement = AXUIElementCreateApplication(pid)
    if let window = attr(appElement, kAXFocusedWindowAttribute as String) {
        let win = window as! AXUIElement
        title = attr(win, kAXTitleAttribute as String) as? String ?? ""
        url = findURL(win, depth: 0) ?? ""
    }
}

print("\(appName)||\(url)||\(title)")
