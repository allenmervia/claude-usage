// Headless preview for the menu-bar dropdown: renders MenuView from a saved --json payload to a
// PNG, so layout changes can be checked without opening the real status-bar window.
//
//   swiftc -swift-version 5 -parse-as-library -D SNAPSHOT \
//       native/ClaudeUsageBar.swift native/snapshot.swift -o snap
//   claude-usage --json > payload.json
//   ./snap payload.json out.png [light|dark]

import SwiftUI
import AppKit

@main
enum Snapshot {
    static func main() {
        let args = CommandLine.arguments
        guard args.count >= 3 else {
            FileHandle.standardError.write(Data("usage: snap payload.json out.png [light|dark]\n".utf8))
            exit(2)
        }
        let appearance = args.count > 3 ? args[3] : "dark"
        DispatchQueue.main.async {
            do { try render(payloadPath: args[1], outPath: args[2], appearance: appearance) }
            catch {
                FileHandle.standardError.write(Data("snapshot failed: \(error)\n".utf8))
                exit(1)
            }
            exit(0)
        }
        RunLoop.main.run()
    }

    @MainActor
    static func render(payloadPath: String, outPath: String, appearance: String) throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: payloadPath))
        let payload = try JSONDecoder().decode(Payload.self, from: data)
        let model = Model()
        model.payload = payload
        let view = MenuView()
            .environmentObject(model)
            .background(Color(nsColor: .windowBackgroundColor))
            .environment(\.colorScheme, appearance == "light" ? .light : .dark)
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2
        guard let img = renderer.nsImage, let tiff = img.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else {
            throw NSError(domain: "snap", code: 1)
        }
        try png.write(to: URL(fileURLWithPath: outPath))
    }
}
