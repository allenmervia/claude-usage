// Claude Usage — native menu-bar shell over claude-usage.py.
//
// The Python tool remains the single source of truth: this app runs `claude-usage --json` on a
// timer and renders the `display` view-model it returns (all phrasing, strategy, and account
// logic live in Python — see attach_display there). Clicking an account shells back out to
// `claude-usage switch <uuid>`. The one piece of formatting done here is re-deriving "Xh Ym left"
// countdowns from each row's resets_at, so they tick while the dropdown is open.
//
// Built by `claude-usage app`, which compiles this file with swiftc, assembles the bundle, and
// embeds the backend script's absolute path as CUBackend in Info.plist.

import SwiftUI
import AppKit
import ServiceManagement

// MARK: - Backend payload (matches claude-usage --json)

struct Payload: Decodable {
    var accounts: [Account]
    var spend_next: SpendNext?
    var gauges: [[Double?]]?
    var updated_ts: Double?
}

struct SpendNext: Decodable {
    var uuid: String
    var label: String?
    var active: Bool?
    var text: String?
}

struct Account: Decodable, Identifiable {
    var uuid: String
    var provider: String?
    var label: String?
    var email: String?
    var active: Bool?
    var error: String?
    var stale: Bool?
    var display: Display?
    var id: String { uuid }
    var isCodex: Bool { provider == "codex" }
}

struct Display: Decodable {
    var plan: String?
    var can_switch: Bool?
    var rows: [DisplayRow]?
    var trend: Trend?
}

struct DisplayRow: Decodable {
    var label: String
    var pct: Double?
    var meta: String?
    var meta_prefix: String?
    var resets_at: String?
}

struct Trend: Decodable {
    var series: [[Double]]?
    var note: String?
}

// MARK: - Severity colors (the tool's green/amber/red at 65/90, dim for no reading)

func sevColor(_ pct: Double?) -> Color { Color(nsColor: sevNSColor(pct)) }

func sevNSColor(_ pct: Double?) -> NSColor {
    guard let p = pct else { return NSColor(red: 0.51, green: 0.54, blue: 0.58, alpha: 1) }
    if p >= 90 { return NSColor(red: 0.90, green: 0.33, blue: 0.29, alpha: 1) }
    if p >= 65 { return NSColor(red: 0.85, green: 0.63, blue: 0.23, alpha: 1) }
    return NSColor(red: 0.25, green: 0.73, blue: 0.31, alpha: 1) }

// MARK: - Status-bar icon: one ring+pie gauge per provider, same design as the xbar title

enum IconRenderer {
    static func render(specs: [[Double?]]) -> NSImage {
        let d: CGFloat = 16, gap: CGFloat = 5, th: CGFloat = 2.6, pieR: CGFloat = 3.6
        let n = max(specs.count, 1)
        let size = NSSize(width: CGFloat(n) * d + CGFloat(n - 1) * gap, height: 18)
        let img = NSImage(size: size, flipped: false) { _ in
            for (i, spec) in specs.enumerated() {
                let ring = spec.count > 0 ? spec[0] : nil
                let pie  = spec.count > 1 ? spec[1] : nil
                let c = CGPoint(x: CGFloat(i) * (d + gap) + d / 2, y: 9)
                let rM = (d - th) / 2
                let ringColor = sevNSColor(ring)
                // track
                ringColor.withAlphaComponent(0.25).setStroke()
                let track = NSBezierPath()
                track.appendArc(withCenter: c, radius: rM, startAngle: 0, endAngle: 360)
                track.lineWidth = th
                track.stroke()
                // fill, clockwise from 12 o'clock
                let frac = max(0, min(1, (ring ?? 0) / 100))
                if frac > 0 {
                    ringColor.setStroke()
                    let arc = NSBezierPath()
                    arc.appendArc(withCenter: c, radius: rM, startAngle: 90,
                                  endAngle: 90 - 360 * frac, clockwise: true)
                    arc.lineWidth = th
                    arc.lineCapStyle = .round
                    arc.stroke()
                }
                // centre pie = the burst window
                if let p = pie {
                    let pieColor = sevNSColor(p)
                    pieColor.withAlphaComponent(0.25).setFill()
                    NSBezierPath(ovalIn: CGRect(x: c.x - pieR, y: c.y - pieR,
                                                width: pieR * 2, height: pieR * 2)).fill()
                    let pf = max(0, min(1, p / 100))
                    if pf > 0 {
                        pieColor.setFill()
                        let wedge = NSBezierPath()
                        wedge.move(to: c)
                        wedge.line(to: CGPoint(x: c.x, y: c.y + pieR))
                        wedge.appendArc(withCenter: c, radius: pieR, startAngle: 90,
                                        endAngle: 90 - 360 * pf, clockwise: true)
                        wedge.close()
                        wedge.fill()
                    }
                }
            }
            return true
        }
        img.isTemplate = false
        return img
    }
}

// MARK: - Backend bridge

enum Backend {
    static var script: String {
        Bundle.main.object(forInfoDictionaryKey: "CUBackend") as? String ?? "claude-usage"
    }
    static var python: String {
        for p in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
        return "/usr/bin/python3"
    }

    static func run(_ args: [String]) async throws -> Data {
        let py = python, script = script
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global().async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: py)
                p.arguments = [script] + args
                let out = Pipe(), err = Pipe()
                p.standardOutput = out
                p.standardError = err
                do {
                    try p.run()
                    let data = out.fileHandleForReading.readDataToEndOfFile()
                    let errData = err.fileHandleForReading.readDataToEndOfFile()
                    p.waitUntilExit()
                    if p.terminationStatus != 0 {
                        let msg = String(data: errData, encoding: .utf8) ?? ""
                        cont.resume(throwing: BackendError.failed(msg.trimmingCharacters(in: .whitespacesAndNewlines)))
                    } else {
                        cont.resume(returning: data)
                    }
                } catch {
                    cont.resume(throwing: error)
                }
            }
        }
    }
}

enum BackendError: LocalizedError {
    case failed(String)
    var errorDescription: String? {
        if case .failed(let m) = self { return m.isEmpty ? "backend exited with an error" : m }
        return nil
    }
}

// MARK: - Model

@MainActor
final class Model: ObservableObject {
    @Published var payload: Payload?
    @Published var icon = IconRenderer.render(specs: [[nil, nil]])
    @Published var refreshing = false
    @Published var switching: String?          // uuid mid-switch
    @Published var lastError: String?
    // Plain @Published backed by UserDefaults — @AppStorage inside an ObservableObject doesn't
    // fire objectWillChange, which would leave the gear menu's checkmark on the old cadence.
    @Published var intervalMinutes: Int {
        didSet { UserDefaults.standard.set(intervalMinutes, forKey: "intervalMinutes") }
    }

    private var ticker: Task<Void, Never>?

    init() {
        let stored = UserDefaults.standard.integer(forKey: "intervalMinutes")
        intervalMinutes = stored == 0 ? 5 : stored
    }

    // Live polling is opt-in so the snapshot tool can render a fixture payload without this
    // instance racing it with a real backend fetch.
    static func live() -> Model {
        let m = Model()
        m.startTimer()
        Task { await m.refresh() }
        return m
    }

    func startTimer() {
        ticker?.cancel()
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                let mins = self?.intervalMinutes ?? 5
                try? await Task.sleep(nanoseconds: UInt64(max(1, mins)) * 60_000_000_000)
                await self?.refresh()
            }
        }
    }

    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let data = try await Backend.run(["--json"])
            let p = try JSONDecoder().decode(Payload.self, from: data)
            payload = p
            // No readable gauge still needs a visible status item: a dim empty ring, not a blank.
            let specs = (p.gauges?.isEmpty == false) ? p.gauges! : [[nil, nil]]
            icon = IconRenderer.render(specs: specs)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    func switchTo(_ uuid: String) async {
        guard switching == nil else { return }
        switching = uuid
        defer { switching = nil }
        do {
            _ = try await Backend.run(["switch", uuid])
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
        await refresh()
    }

    var claude: [Account] { payload?.accounts.filter { !$0.isCodex } ?? [] }
    var codex: [Account] { payload?.accounts.filter { $0.isCodex } ?? [] }
    var multiProvider: Bool { !claude.isEmpty && !codex.isEmpty }
}

// MARK: - Countdown re-derivation ("3h 22m left" stays live while the window is open)

func relString(_ date: Date, now: Date) -> String {
    let secs = Int(date.timeIntervalSince(now))
    if secs <= 0 { return "now" }
    let d = secs / 86400, h = (secs % 86400) / 3600, m = (secs % 3600) / 60
    if d > 0 { return "\(d)d \(h)h" }
    if h > 0 { return "\(h)h \(m)m" }
    return "\(m)m"
}

func liveMeta(_ row: DisplayRow, now: Date) -> String {
    // The backend splits countdown rows into meta_prefix + resets_at, so ticking is recomposition,
    // not parsing: prefix ("", "~", "Tue 5pm · ") + a re-derived countdown.
    guard let prefix = row.meta_prefix, let iso = row.resets_at, let date = parseISO(iso)
    else { return row.meta ?? "" }
    return prefix + relString(date, now: now) + " left"
}

func parseISO(_ s: String) -> Date? {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = f.date(from: s) { return d }
    f.formatOptions = [.withInternetDateTime]
    return f.date(from: s)
}

// MARK: - Views

struct MenuView: View {
    @EnvironmentObject var model: Model

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if model.payload == nil {
                HStack {
                    ProgressView().controlSize(.small)
                    Text(model.lastError ?? "Reading usage…").font(.system(size: 12)).foregroundStyle(.secondary)
                }.padding(12)
            } else {
                if model.multiProvider { SectionHeader("CLAUDE") }
                ForEach(model.claude) { AccountCard(account: $0) }
                if !model.codex.isEmpty {
                    SectionHeader("CODEX")
                    ForEach(model.codex) { AccountCard(account: $0) }
                }
                FooterView()
            }
        }
        .padding(8)
        .frame(width: 344)
        .onAppear { Task { await model.refresh() } }
    }
}

struct SectionHeader: View {
    let title: String
    init(_ t: String) { title = t }
    var body: some View {
        Text(title)
            .font(.system(size: 10, weight: .semibold))
            .tracking(1.2)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 6)
            .padding(.top, 4)
    }
}

struct AccountCard: View {
    @EnvironmentObject var model: Model
    let account: Account
    @State private var hovered = false

    // A switchable card is one control, the way a table row or menu row is: click anywhere on it to
    // switch. The hover pill only labels what the click does — it is not the target.
    var body: some View {
        if account.display?.can_switch == true {
            Button {
                Task { await model.switchTo(account.uuid) }
            } label: {
                card
            }
            .buttonStyle(.plain)
            .disabled(model.switching != nil)
            .onHover { inside in
                if inside { NSCursor.pointingHand.set() } else { NSCursor.arrow.set() }
            }
        } else {
            card
        }
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 5) {
            header
            if let err = account.error {
                Text(err).font(.system(size: 11)).foregroundStyle(sevColor(95))
            } else {
                TimelineView(.periodic(from: .now, by: 60)) { ctx in
                    VStack(alignment: .leading, spacing: 5) {
                        // positional identity: labels can collide (two scoped models truncated alike)
                        ForEach(Array((account.display?.rows ?? []).enumerated()), id: \.offset) { _, row in
                            BarRow(row: row, now: ctx.date)
                        }
                    }
                }
                if let t = account.display?.trend { TrendRow(trend: t) }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.primary.opacity(account.active == true ? 0.07 : (hovered ? 0.08 : 0.03)))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(Color.primary.opacity(account.active == true ? 0.12 : 0), lineWidth: 1)
        )
        .onHover { hovered = $0 }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            if account.active == true {
                Text("▶").font(.system(size: 9)).foregroundStyle(.primary)
            }
            Text(account.label ?? account.uuid)
                .font(.system(size: 13, weight: .semibold))
            if let em = account.email, em.contains("@"), em != account.label {
                Text(em)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 4)
            // The pill stays in the layout and the switch button draws over it, so hovering never
            // changes the header's metrics — a swap would bump every row below by a pixel.
            ZStack(alignment: .trailing) {
                if let plan = account.display?.plan, !plan.isEmpty {
                    Text(plan)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(Color.primary.opacity(0.07)))
                        .opacity(showSwitch ? 0 : 1)
                }
                if showSwitch {
                    // passive label: the card itself is the button
                    if model.switching == account.uuid {
                        ProgressView().controlSize(.mini)
                    } else {
                        Text("⇄ Switch")
                            .font(.system(size: 10.5, weight: .medium))
                            .foregroundStyle(Color.accentColor)
                    }
                }
            }
        }
    }

    private var showSwitch: Bool { hovered && account.display?.can_switch == true }
}

struct BarRow: View {
    let row: DisplayRow
    let now: Date

    var body: some View {
        HStack(spacing: 4) {
            Text(row.label)
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 44, alignment: .leading)
            if let pct = row.pct {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.primary.opacity(0.10))
                        if pct > 0 {
                            Capsule()
                                .fill(sevColor(pct))
                                .frame(width: max(5, geo.size.width * min(1, pct / 100)))
                        }
                    }
                }
                .frame(height: 5)
                Text("\(Int(pct))%")
                    .font(.system(size: 10.5, design: .monospaced))
                    .frame(width: 30, alignment: .trailing)
                Text(liveMeta(row, now: now))
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(width: 150, alignment: .trailing)
            } else {
                Text(row.meta ?? "")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }
}

struct TrendRow: View {
    let trend: Trend
    var body: some View {
        HStack(spacing: 6) {
            Text("trend")
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 44, alignment: .leading)
            Sparkline(series: trend.series ?? [])
                .frame(width: 64, height: 12)
            Text(trend.note ?? "")
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
            Spacer()
        }
    }
}

struct Sparkline: View {
    let series: [[Double]]
    var body: some View {
        let pts = series.filter { $0.count >= 2 }
        GeometryReader { geo in
            if pts.count >= 2, let t0 = pts.first?[0], let t1 = pts.last?[0], t1 > t0 {
                let W = geo.size.width, H = geo.size.height
                let path = Path { p in
                    for (i, s) in pts.enumerated() {
                        let x = (s[0] - t0) / (t1 - t0) * (W - 4) + 2
                        let y = (1 - max(0, min(100, s[1])) / 100) * (H - 4) + 2
                        if i == 0 { p.move(to: CGPoint(x: x, y: y)) }
                        else { p.addLine(to: CGPoint(x: x, y: y)) }
                    }
                }
                let color = sevColor(pts.last?[1])
                path.stroke(color, style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
                Circle().fill(color).frame(width: 3.5, height: 3.5)
                    .position(x: W - 2, y: (1 - max(0, min(100, pts.last![1])) / 100) * (H - 4) + 2)
            }
        }
    }
}

struct FooterView: View {
    @EnvironmentObject var model: Model
    @State private var loginItem = SMAppService.mainApp.status == .enabled

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let sn = model.payload?.spend_next, sn.active != true {
                Button {
                    Task { await model.switchTo(sn.uuid) }
                } label: {
                    HStack(spacing: 6) {
                        Text("⇄").font(.system(size: 11))
                        Text("Spend next: \(sn.label ?? "?") — \(sn.text ?? "")")
                            .font(.system(size: 11))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.accentColor.opacity(0.10)))
                }
                .buttonStyle(.plain)
            }
            if let err = model.lastError {
                Text("⚠ \(err)").font(.system(size: 10.5)).foregroundStyle(sevColor(70)).lineLimit(2)
            }
            if model.payload?.accounts.contains(where: { $0.stale == true }) == true {
                Text("⚠ last known values — rate-limited; updates on the next refresh")
                    .font(.system(size: 10.5)).foregroundStyle(sevColor(70))
            }
            Divider()
            HStack(spacing: 10) {
                TimelineView(.periodic(from: .now, by: 30)) { ctx in
                    Text(updatedText(now: ctx.date))
                        .font(.system(size: 10.5)).foregroundStyle(.secondary)
                }
                Spacer()
                if model.refreshing {
                    ProgressView().controlSize(.mini)
                } else {
                    Button { Task { await model.refresh() } } label: {
                        Image(systemName: "arrow.clockwise").font(.system(size: 11))
                    }
                    .buttonStyle(.borderless)
                    .help("Refresh now")
                }
                Menu {
                    ForEach([1, 5, 10, 30], id: \.self) { m in
                        Button {
                            model.intervalMinutes = m
                            model.startTimer()
                        } label: {
                            if model.intervalMinutes == m { Text("✓ \(m)m") } else { Text("\(m)m") }
                        }
                    }
                    Divider()
                    Toggle("Launch at login", isOn: Binding(
                        get: { loginItem },
                        set: { on in
                            do {
                                if on { try SMAppService.mainApp.register() }
                                else { try SMAppService.mainApp.unregister() }
                                loginItem = SMAppService.mainApp.status == .enabled
                            } catch { loginItem = SMAppService.mainApp.status == .enabled }
                        }))
                    Button("Quit") { NSApp.terminate(nil) }
                } label: {
                    Image(systemName: "gearshape").font(.system(size: 11))
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
            }
            .padding(.horizontal, 2)
        }
        .padding(.top, 4)
        .padding(.horizontal, 4)
    }

    private func updatedText(now: Date) -> String {
        guard let ts = model.payload?.updated_ts else { return "—" }
        let ago = Int(now.timeIntervalSince(Date(timeIntervalSince1970: ts)))
        let agoText = ago < 90 ? "just now" : (ago < 3600 ? "\(ago / 60)m ago" : "\(ago / 3600)h ago")
        return "Updated \(agoText) · refreshes every \(model.intervalMinutes)m"
    }
}

// MARK: - App
// SNAPSHOT builds (swiftc -D SNAPSHOT, with native/snapshot.swift) reuse every view above but
// supply their own entry point that renders MenuView to a PNG — the headless preview loop.

#if !SNAPSHOT
@main
struct ClaudeUsageBarApp: App {
    @StateObject private var model = Model.live()

    var body: some Scene {
        MenuBarExtra {
            MenuView().environmentObject(model)
        } label: {
            Image(nsImage: model.icon)
        }
        .menuBarExtraStyle(.window)
    }
}
#endif
