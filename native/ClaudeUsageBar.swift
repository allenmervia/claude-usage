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
    var gauges: [[Double?]]?
    var desktop: Desktop?
    var mock: Bool?
    var updated_ts: Double?
}

// The desktop app's account is a set of files rather than a token, so it carries no usage and
// sits apart from `accounts`. `needs_confirm` is the backend's judgement, not ours: with the app
// closed a swap costs nothing and behaves like any other switch, and only an open app turns the
// same click into "quit this and close what is running in it".
struct Desktop: Decodable {
    var active: String?
    var stashes: [Stash]
    var needs_repair: Bool?
    var app_running: Bool?
    var needs_confirm: Bool?
}

struct Stash: Decodable, Identifiable {
    var label: String
    var files: Int?
    var age_days: Double?
    var app_version: String?
    var stale: Bool?
    var active: Bool?
    var can_switch: Bool?
    var matched_uuid: String?
    var id: String { label }
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
    var desktop: DisplayDesktop?
}

// The desktop app's stash for this same account, folded into the account's card so one click
// moves both surfaces. Present only when a stash's name pairs with this account.
struct DisplayDesktop: Decodable {
    var label: String
    var active: Bool?
    var can_switch: Bool?
    var stale: Bool?
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
    var reset_ts: Double?
    var window_s: Double?
}

// claude-usage insights --json: the trailing week's transcript aggregates
struct InsightsPayload: Decodable {
    var as_of: Double?
    var ttl_s: Double?
    var window_days: Int?
    var total_cost: Double?
    var today_cost: Double?
    var models: [ModelRow]?
}

struct ModelRow: Decodable {
    var name: String
    var family: String?
    var msgs: Int?
    var cost: Double?
    var output: Double?
    var cache_read: Double?
    var efforts: [EffortSlice]?
}

struct EffortSlice: Decodable {
    var effort: String?
    var rank: Int?
    var cost: Double?
    var msgs: Int?
}

// Identity colors for series and model families — distinct from the severity palette so a line or
// bar never reads as a state.
let seriesPalette: [Color] = [
    Color(red: 0.22, green: 0.53, blue: 0.90),   // blue
    Color(red: 0.85, green: 0.35, blue: 0.15),   // orange
    Color(red: 0.10, green: 0.62, blue: 0.44),   // green-aqua
    Color(red: 0.57, green: 0.52, blue: 0.91),   // violet
    Color(red: 0.84, green: 0.32, blue: 0.51),   // magenta
]

// MARK: - Severity colors (the tool's green/amber/red at 65/90, dim for no reading)

func sevColor(_ pct: Double?) -> Color { Color(nsColor: sevNSColor(pct)) }

func sevNSColor(_ pct: Double?) -> NSColor {
    guard let p = pct else { return NSColor(red: 0.51, green: 0.54, blue: 0.58, alpha: 1) }
    if p >= 90 { return NSColor(red: 0.90, green: 0.33, blue: 0.29, alpha: 1) }
    if p >= 65 { return NSColor(red: 0.85, green: 0.63, blue: 0.23, alpha: 1) }
    return NSColor(red: 0.25, green: 0.73, blue: 0.31, alpha: 1) }

// MARK: - Status-bar icon: one gauge per provider — ring = weekly window, centre pie = 5-hour

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
        // the script path is baked in at build time; if that checkout moved, every call would
        // fail with a bare python error that never names the fix
        guard FileManager.default.fileExists(atPath: script) else {
            throw BackendError.failed("backend script missing at \(script) — run `claude-usage app` from your checkout to rebuild")
        }
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
    @Published var confirmTarget: String?
    // A failed switch is reported separately from a failed refresh: the refresh that every switch
    // triggers succeeds, and if both shared one field that success would erase the failure before
    // it could be read — the click would look like it did nothing at all. This one survives until
    // the next switch is attempted.
    @Published var actionError: String?
    @Published var insights: InsightsPayload?
    @Published var insightsError: String?
    private var insightsFetching = false
    @Published var hoveredMixRow: String?      // drives the ledger drawn at the window root
    @Published var tab = 0                     // 0 Usage · 1 Insights
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

    func refresh(force: Bool = false) async {
        // force lets a completed switch redraw immediately even if a timer refresh is mid-flight —
        // otherwise the guard would swallow it and the ▶ would sit on the old account for a tick.
        if refreshing && !force { return }
        refreshing = true
        do {
            let data = try await Backend.run(["--json"])
            let p = try JSONDecoder().decode(Payload.self, from: data)
            // Animate the swap: after a switch the ▶ moves rows around, and an unanimated reflow
            // reads as the window jumping.
            withAnimation(.easeOut(duration: 0.18)) { payload = p }
            // No readable gauge still needs a visible status item: a dim empty ring, not a blank.
            let specs = (p.gauges?.isEmpty == false) ? p.gauges! : [[nil, nil]]
            icon = IconRenderer.render(specs: specs)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
        refreshing = false
        // Insights ride along only once the backend's cache has aged out (the payload carries its
        // TTL), so ordinary refreshes spawn nothing extra — and a cold scan never blocks the
        // usage data above, which has already landed.
        let age = Date().timeIntervalSince1970 - (insights?.as_of ?? 0)
        if !insightsFetching, insights == nil || age > (insights?.ttl_s ?? 1800) {
            insightsFetching = true
            defer { insightsFetching = false }
            do {
                let data = try await Backend.run(["insights", "--json"])
                insights = try JSONDecoder().decode(InsightsPayload.self, from: data)
                insightsError = nil
            } catch {
                insightsError = error.localizedDescription
            }
        }
    }

    func switchTo(_ uuid: String) async {
        guard switching == nil else { return }
        switching = uuid
        defer { switching = nil }
        actionError = nil
        do {
            _ = try await Backend.run(["switch", uuid])
        } catch {
            actionError = error.localizedDescription
        }
        await refresh(force: true)
    }

    /// One click, both surfaces: the CLI switch (instant) and then the desktop swap. Ordered so
    /// the cheap, reliable part lands even when the desktop half fails; errors from either side
    /// surface together rather than the second silently masking the first.
    func switchAll(_ account: Account) async {
        guard switching == nil else { return }
        switching = account.uuid
        defer { switching = nil }
        var errs: [String] = []
        actionError = nil
        if account.display?.can_switch == true {
            do { _ = try await Backend.run(["switch", account.uuid]) }
            catch { errs.append("CLI: \(error.localizedDescription)") }
        }
        if let st = account.display?.desktop, st.can_switch == true,
           payload?.desktop?.needs_repair != true {
            do { _ = try await Backend.run(["desktop-switch", st.label]) }
            catch { errs.append("desktop: \(error.localizedDescription)") }
        }
        // actionError, not lastError: the refresh below succeeds and clears lastError, which
        // would erase this before it could be read and leave a failed click looking like a
        // click that did nothing.
        actionError = errs.isEmpty ? nil : errs.joined(separator: " · ")
        await refresh(force: true)
    }

    func switchDesktop(_ label: String) async {
        guard switching == nil else { return }
        switching = "desktop:" + label
        defer { switching = nil }
        actionError = nil
        do {
            _ = try await Backend.run(["desktop-switch", label])
        } catch {
            actionError = error.localizedDescription
        }
        await refresh(force: true)
    }

    var desktop: Desktop? { payload?.desktop }

    /// Whether the desktop app has any captured accounts here. When it does, it is the surface
    /// that defines which account you are on — someone who set it up works in the app, and the
    /// CLI follows. With nothing captured, the CLI is the only surface and answers by itself.
    var desktopOnboarded: Bool { !(payload?.desktop?.stashes.isEmpty ?? true) }

    var claude: [Account] { payload?.accounts.filter { !$0.isCodex } ?? [] }
    var codex: [Account] { payload?.accounts.filter { $0.isCodex } ?? [] }
    var multiProvider: Bool { !claude.isEmpty && !codex.isEmpty }
}

// MARK: - Floating chip: the shared dress for hover popovers (mix ledger, strip identity)

struct FloatingChip: ViewModifier {
    let border: Color
    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: 7)
                    .fill(Color(nsColor: .windowBackgroundColor))
                    .shadow(color: .black.opacity(0.35), radius: 10, y: 4)
            )
            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(border))
            .allowsHitTesting(false)
    }
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

func agoText(since ts: Double?) -> String {
    guard let ts = ts else { return "—" }
    let secs = Int(Date().timeIntervalSince1970 - ts)
    // "just now" means it: only within a fresh-fetch buffer, then honest seconds/minutes
    if secs < 15 { return "just now" }
    if secs < 90 { return "\(secs)s ago" }
    if secs < 3600 { return "\(secs / 60)m ago" }
    return "\(secs / 3600)h ago"
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
                // Centered, Settings-style; the window keeps one width across tabs so switching
                // never moves or resizes the panel.
                Picker("", selection: $model.tab) {
                    Text("Usage").tag(0)
                    Text("Insights").tag(1)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 200)
                .frame(maxWidth: .infinity)
                .padding(.bottom, 4)
                if model.payload?.mock == true {
                    // Invented figures look exactly like real ones, so the panel says so on both
                    // tabs rather than trusting anyone to remember they turned it on.
                    Text("MOCK DATA — every figure below is invented")
                        .font(.system(size: 10, weight: .semibold))
                        .tracking(0.8)
                        .foregroundStyle(sevColor(70))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 4)
                        .background(RoundedRectangle(cornerRadius: 6).fill(sevColor(70).opacity(0.14)))
                        .padding(.bottom, 2)
                }
                if model.tab == 1 {
                    InsightsTab()
                } else {
                    accountsColumn
                }
                FooterView()
            }
        }
        .padding(8)
        .frame(width: 420)
        .onChange(of: model.tab) { _ in model.hoveredMixRow = nil }
        .overlayPreferenceValue(MixAnchorKey.self) { anchors in
            // drawn from the root so no sibling can paint over it; flipped above the row when the
            // window's bottom edge would clip it
            GeometryReader { geo in
                if model.tab == 1, let name = model.hoveredMixRow,
                   let ins = model.insights, let rows = ins.models,
                   let row = rows.first(where: { $0.name == name }),
                   let anchor = anchors[name] {
                    let rect = geo[anchor]
                    let h = ModelMixPanel.ledgerHeight(row)
                    let below = rect.maxY + 6
                    let y = below + h > geo.size.height - 8 ? max(8, rect.minY - h - 6) : below
                    let x = min(max(8, rect.minX + 60), geo.size.width - 223)
                    ModelMixPanel.ledger(row)
                        .offset(x: x, y: y)
                }
            }
            .allowsHitTesting(false)
        }
        .onAppear {
            model.hoveredMixRow = nil
            model.confirmTarget = nil
            Task { await model.refresh() }
        }
    }

    @ViewBuilder private var accountsColumn: some View {
        if model.claude.isEmpty && model.codex.isEmpty {
            Text("No accounts found — run `claude`, then /login, and refresh.")
                .font(.system(size: 11.5)).foregroundStyle(.secondary)
                .padding(.vertical, 14)
                .frame(maxWidth: .infinity)
        }
        if model.desktop?.needs_repair == true {
            Text("A desktop switch was interrupted. Run `desktop-switch.py repair` before switching again.")
                .font(.system(size: 11)).foregroundStyle(sevColor(95))
                .padding(.horizontal, 6).padding(.bottom, 2)
        }
        if model.multiProvider { SectionHeader("CLAUDE") }
        ForEach(model.claude) { AccountCard(account: $0) }
        if !model.codex.isEmpty {
            SectionHeader("CODEX")
            ForEach(model.codex) { AccountCard(account: $0) }
        }
        if let d = model.desktop {
            let orphans = d.stashes.filter { $0.matched_uuid == nil }
            if !orphans.isEmpty {
                SectionHeader("DESKTOP APP")
                Text("Not paired with an account above. Rename a stash to the account's email to merge them.")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .padding(.horizontal, 6)
                ForEach(orphans) { StashCard(stash: $0, desktop: d) }
            }
        }
    }
}

// MARK: - Insights tab: the weekly burn (per-account window bands on one timeline) and the model
// mix (the trailing week's transcript aggregates from the backend scan).

struct InsightsTab: View {
    @EnvironmentObject var model: Model

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionHeader("WEEKLY BURN")
            WindowStrips(accounts: model.payload?.accounts ?? [])
                .padding(.horizontal, 8)
                .padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.03)))
            SectionHeader("MODEL MIX · PAST \(model.insights?.window_days ?? 7) DAYS")
            ModelMixPanel(insights: model.insights, error: model.insightsError)
        }
    }
}

// Every account's window as a band on one shared timeline: each band spans that account's
// current window (start → reset, length from the backend), the line inside is its recorded burn,
// and a single now-line crosses all rows — the reset stagger reads as a shape.
struct WindowStrips: View {
    let accounts: [Account]
    @State private var hover: CGPoint?
    @State private var hoveredLabel: Int?

    private static let rowH: CGFloat = 34, gap: CGFloat = 9, axisH: CGFloat = 15, labelW: CGFloat = 60

    var body: some View {
        // Color keys on the account's position in the payload, then rows sort by window start so
        // the bands cascade top-left to bottom-right — identity stays put while order follows the
        // week as resets rotate.
        let entries: [Entry] = accounts.enumerated().compactMap { idx, a -> Entry? in
            guard let t = a.display?.trend, (t.series?.count ?? 0) >= 2,
                  let rt = t.reset_ts else { return nil }
            let win = t.window_s ?? 7 * 86400
            let email = (a.email?.contains("@") == true && a.email != a.label) ? a.email : nil
            return Entry(name: a.label ?? a.uuid, email: email, isCodex: a.isCodex, trend: t,
                         color: seriesPalette[idx % seriesPalette.count],
                         start: rt - win, reset: rt)
        }
        .sorted { $0.start < $1.start }
        if entries.isEmpty {
            Text("collecting history…").font(.system(size: 11)).foregroundStyle(.secondary)
        } else {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: Self.gap) {
                    ForEach(Array(entries.enumerated()), id: \.offset) { i, e in
                        VStack(alignment: .leading, spacing: 0) {
                            Text(e.name).font(.system(size: 10.5, weight: .semibold))
                                .foregroundStyle(e.color)
                            Text("\(Int(e.trend.series?.last?.last ?? 0))%")
                                .font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                        }
                        .frame(width: Self.labelW, height: Self.rowH, alignment: .leading)
                        .contentShape(Rectangle())
                        .onHover { inside in
                            if inside { hoveredLabel = i }
                            else if hoveredLabel == i { hoveredLabel = nil }
                        }
                        .overlay(alignment: .leading) {
                            // identity on demand: the short name hides which login and provider
                            if hoveredLabel == i {
                                Text("\(e.email.map { "\($0) · " } ?? "")\(e.isCodex ? "Codex" : "Claude")")
                                    .font(.system(size: 9.5, design: .monospaced))
                                    .padding(.horizontal, 8)
                                    .padding(.vertical, 4)
                                    .fixedSize()
                                    .modifier(FloatingChip(border: e.color.opacity(0.5)))
                                    .offset(x: Self.labelW + 4)
                            }
                        }
                    }
                }
                .zIndex(hoveredLabel != nil ? 1 : 0)
                canvas(entries)
            }
        }
    }

    struct Entry {
        let name: String
        let email: String?
        let isCodex: Bool
        let trend: Trend
        let color: Color
        let start: Double
        let reset: Double
    }

    // One canvas, one time axis: every account's week is a band placed where it falls in real
    // time, so a single now-line crosses all rows and the reset stagger reads as a shape.
    private static let dayFmt: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "EEE"; return f
    }()
    private static let resetFmt: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "EEE ha"; return f
    }()
    private static let hoverFmt: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "EEE h:mm a"; return f
    }()

    private func canvas(_ entries: [Entry]) -> some View {
        let H = CGFloat(entries.count) * Self.rowH + CGFloat(entries.count - 1) * Self.gap + Self.axisH
        return Canvas { ctx, size in
            let now = Date().timeIntervalSince1970
            // A week back and a week forward: the span of the current windows themselves. Earlier
            // history is drawn where it falls inside this, and simply runs off the left edge where
            // it does not — widening the axis to fit it would shrink every live window to pay for
            // context.
            let t0 = (entries.map { $0.start }.min() ?? now - 7 * 86400) - 3600
            var t1 = max(entries.map { $0.reset }.max() ?? now, now) + 3600
            t1 += (t1 - t0) * 0.01
            func X(_ t: Double) -> CGFloat {
                CGFloat((min(max(t, t0), t1) - t0) / (t1 - t0)) * size.width
            }
            func rowTop(_ i: Int) -> CGFloat { CGFloat(i) * (Self.rowH + Self.gap) }
            let axisY = H - Self.axisH
            // shared day grid + weekday labels, once, under all rows
            var day = Calendar.current.startOfDay(for: Date(timeIntervalSince1970: t0)).addingTimeInterval(86400)
            while day.timeIntervalSince1970 < t1 {
                let x = X(day.timeIntervalSince1970)
                var p = Path(); p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: axisY))
                ctx.stroke(p, with: .color(Color.primary.opacity(0.06)))
                if x > 12 && x < size.width - 12 {
                    ctx.draw(Text(Self.dayFmt.string(from: day)).font(.system(size: 7.5)).foregroundColor(.secondary),
                             at: CGPoint(x: x, y: axisY + Self.axisH / 2 + 1))
                }
                day = day.addingTimeInterval(86400)
            }
            // the row under the pointer draws at full strength; its siblings recede
            let hoverRow: Int? = hover.flatMap { h in
                let slot = Self.rowH + Self.gap
                let idx = Int(h.y / slot)
                let within = h.y.truncatingRemainder(dividingBy: slot) <= Self.rowH
                return (idx >= 0 && idx < entries.count && within) ? idx : nil
            }
            for (i, e) in entries.enumerated() {
                let dim: Double = (hoverRow == nil || hoverRow == i) ? 1 : 0.4
                let color = e.color.opacity(dim), ws = e.start, rt = e.reset
                let top = rowTop(i)
                let band = CGRect(x: X(ws), y: top, width: X(rt) - X(ws), height: Self.rowH)
                func Y(_ v: Double) -> CGFloat {
                    top + 3 + CGFloat(1 - min(100, max(0, v)) / 100) * (Self.rowH - 6)
                }
                // The window before this one, drawn faintly behind: last week's shape is the only
                // thing that says whether this week's is normal. It is context, so it stays quiet
                // enough that the live window still reads first.
                let win = rt - ws
                let pStart = max(ws - win, t0)     // clipped to the axis; X() would otherwise
                                                   // stack everything earlier onto the left edge
                if X(ws) - X(pStart) > 6 {
                    let pBand = CGRect(x: X(pStart), y: top + 4, width: X(ws) - X(pStart),
                                       height: Self.rowH - 8)
                    ctx.drawLayer { layer in
                        layer.clip(to: Path(roundedRect: pBand, cornerRadius: 4))
                        layer.fill(Path(pBand), with: .color(Color.primary.opacity(0.025)))
                        let pp = (e.trend.series ?? []).filter {
                            $0.count >= 2 && $0[0] >= pStart && $0[0] < ws
                        }
                        if pp.count >= 2 {
                            var lp = Path()
                            for (j, pt) in pp.enumerated() {
                                // squeezed into the shorter band so it cannot be mistaken for live
                                let y = top + 4 + CGFloat(1 - min(100, max(0, pt[1])) / 100) * (Self.rowH - 11)
                                let p = CGPoint(x: X(pt[0]), y: y)
                                j == 0 ? lp.move(to: p) : lp.addLine(to: p)
                            }
                            layer.stroke(lp, with: .color(color.opacity(0.32)),
                                         style: StrokeStyle(lineWidth: 1, lineJoin: .round))
                        }
                    }
                }
                ctx.drawLayer { layer in
                    layer.clip(to: Path(roundedRect: band, cornerRadius: 5))
                    layer.fill(Path(band), with: .color(Color.primary.opacity(hoverRow == i ? 0.08 : 0.05)))
                    if now > ws {   // the stretch still to come sits slightly brighter
                        layer.fill(Path(CGRect(x: X(now), y: top, width: band.maxX - X(now),
                                               height: Self.rowH)),
                                   with: .color(Color.primary.opacity(0.03)))
                    }
                    // burn line with a soft area fill beneath it
                    let pts = (e.trend.series ?? []).filter { $0.count >= 2 && $0[0] >= ws }
                    if pts.count >= 2 {
                        var lp = Path(), ap = Path()
                        for (j, pt) in pts.enumerated() {
                            let p = CGPoint(x: X(pt[0]), y: Y(pt[1]))
                            if j == 0 {
                                lp.move(to: p)
                                ap.move(to: CGPoint(x: p.x, y: band.maxY)); ap.addLine(to: p)
                            } else { lp.addLine(to: p); ap.addLine(to: p) }
                        }
                        ap.addLine(to: CGPoint(x: X(pts.last![0]), y: band.maxY))
                        ap.closeSubpath()
                        layer.fill(ap, with: .color(color.opacity(0.13)))
                        layer.stroke(lp, with: .color(color),
                                     style: StrokeStyle(lineWidth: 1.5, lineJoin: .round))
                        let lastP = CGPoint(x: X(pts.last![0]), y: Y(pts.last![1]))
                        layer.fill(Path(ellipseIn: CGRect(x: lastP.x - 2, y: lastP.y - 2,
                                                          width: 4, height: 4)),
                                   with: .color(color))
                    }
                }
                // reset label just inside the band's end
                ctx.draw(Text("↺ \(Self.resetFmt.string(from: Date(timeIntervalSince1970: rt)))")
                            .font(.system(size: 8.5)).foregroundColor(.secondary),
                         at: CGPoint(x: band.maxX - 4, y: top + 8), anchor: .trailing)
            }
            // one now-line through every row
            var nowLine = Path()
            nowLine.move(to: CGPoint(x: X(now), y: 0)); nowLine.addLine(to: CGPoint(x: X(now), y: axisY))
            ctx.stroke(nowLine, with: .color(Color.primary.opacity(0.45)), style: StrokeStyle(lineWidth: 1.5))
            // hover: crosshair, the time small in the chart's corner, and the hovered row's
            // value as a chip at the intersection — the rest of the old tooltip restated what the
            // chart already shows.
            if let h = hover, h.x >= 0, h.x <= size.width {
                let t = t0 + Double(h.x / size.width) * (t1 - t0)
                var cross = Path()
                cross.move(to: CGPoint(x: h.x, y: 0)); cross.addLine(to: CGPoint(x: h.x, y: axisY))
                ctx.stroke(cross, with: .color(Color.primary.opacity(0.3)))
                ctx.draw(Text(Self.hoverFmt.string(from: Date(timeIntervalSince1970: t)))
                            .font(.system(size: 8.5, design: .monospaced)).foregroundColor(.secondary),
                         at: CGPoint(x: 4, y: 7), anchor: .leading)
                if let i = hoverRow {
                    let e = entries[i]
                    let pts = (e.trend.series ?? []).filter { $0.count >= 2 && $0[0] >= e.start }
                    if t <= now, t >= e.start, let v = Self.value(of: pts, at: t) {
                        let top = CGFloat(i) * (Self.rowH + Self.gap)
                        let y = top + 3 + CGFloat(1 - min(100, max(0, v)) / 100) * (Self.rowH - 6)
                        let label = "\(Int(v.rounded()))%"
                        let chipW = CGFloat(label.count) * 6 + 10
                        var cx = h.x + 8
                        if cx + chipW > size.width - 4 { cx = h.x - 8 - chipW }
                        let chip = CGRect(x: cx, y: min(max(0, y - 8), axisY - 16), width: chipW, height: 16)
                        ctx.fill(Path(roundedRect: chip, cornerRadius: 5),
                                 with: .color(Color(nsColor: .windowBackgroundColor).opacity(0.95)))
                        ctx.stroke(Path(roundedRect: chip, cornerRadius: 5),
                                   with: .color(e.color.opacity(0.6)))
                        ctx.draw(Text(label).font(.system(size: 9, weight: .semibold, design: .monospaced))
                                    .foregroundColor(e.color),
                                 at: CGPoint(x: chip.midX, y: chip.midY))
                    }
                }
            }
        }
        .frame(height: H)
        .onContinuousHover { phase in
            switch phase {
            case .active(let p): hover = p
            case .ended: hover = nil
            }
        }
    }
}

extension WindowStrips {
    // linear interpolation within the sampled span; nil outside it (no invented values)
    static func value(of series: [[Double]], at t: Double) -> Double? {
        let pts = series.filter { $0.count >= 2 }
        guard let first = pts.first, let last = pts.last, t >= first[0], t <= last[0] else { return nil }
        var prev = first
        for p in pts {
            if p[0] >= t {
                let span = p[0] - prev[0]
                if span <= 0 { return p[1] }
                return prev[1] + (p[1] - prev[1]) * (t - prev[0]) / span
            }
            prev = p
        }
        return last[1]
    }
}


// Rows report where they are; the window root draws the ledger — an overlay attached to a row can
// be painted over by later siblings and clipped at the window's bottom edge, the root can't be.
struct MixAnchorKey: PreferenceKey {
    static var defaultValue: [String: Anchor<CGRect>] = [:]
    static func reduce(value: inout [String: Anchor<CGRect>], nextValue: () -> [String: Anchor<CGRect>]) {
        value.merge(nextValue()) { _, new in new }
    }
}

struct ModelMixPanel: View {
    let insights: InsightsPayload?
    let error: String?
    @EnvironmentObject var model: Model
    // keyed by the backend's family field — versions within a family share its hue
    static let colors: [String: Color] = [
        "Opus": seriesPalette[0], "Fable": seriesPalette[1],
        "Sonnet": seriesPalette[2], "Haiku": Color(red: 0.79, green: 0.52, blue: 0.0),
        "GPT": seriesPalette[3],
    ]
    static func color(for row: ModelRow) -> Color {
        colors[row.family ?? String(row.name.split(separator: " ").first ?? "")] ?? .gray
    }
    // effort shows as opacity within a row's bar: deeper effort, fuller ink. Levels the backend
    // adds later still separate via their rank.
    static func effortOpacity(_ effort: String?, rank: Int?) -> Double {
        switch effort {
        case "max": return 1.0
        case "xhigh": return 0.86
        case "high": return 0.72
        case "medium": return 0.48
        case "low": return 0.3
        case nil: return 0.6
        default: return rank.map { max(0.25, 1.0 - 0.14 * Double($0)) } ?? 0.6
        }
    }
    static func effortLabel(_ effort: String?) -> String {
        effort == "medium" ? "med" : (effort ?? "unset")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let ins = insights, let rows = ins.models, !rows.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(Self.usd(ins.total_cost)).font(.system(size: 20, weight: .bold))
                    Text("this week · \(Self.usd(ins.today_cost)) today")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                let maxCost = max(rows.first?.cost ?? 0, 0.001)
                let labelW = Self.labelWidth(rows)
                ForEach(rows, id: \.name) { row in
                    mixRow(row, maxCost: maxCost, labelW: labelW)
                }
            } else if let error = error {
                Text("⚠ couldn't read the transcript scan — \(error)")
                    .font(.system(size: 10.5)).foregroundStyle(sevColor(70))
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 8)
            } else {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("scanning the week's transcripts…")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                .padding(.vertical, 10)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 9)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.03)))
    }

    // Resting rows carry name · sections · dollars; the counts, tokens, and comparisons live in
    // the hover ledger, which also labels each effort section in place — no legend to decode.
    // the label column fits the longest name, capped so the bars keep most of the row
    static func labelWidth(_ rows: [ModelRow]) -> CGFloat {
        let font = NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)
        let widest = rows.map { ($0.name as NSString).size(withAttributes: [.font: font]).width }.max() ?? 64
        return min(120, ceil(widest) + 4)
    }

    private func mixRow(_ row: ModelRow, maxCost: Double, labelW: CGFloat) -> some View {
        HStack(spacing: 6) {
            Text(row.name)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .frame(width: labelW, alignment: .leading)
            GeometryReader { geo in
                let rowCost = max(row.cost ?? 0, 0.001)
                let rowW = max(3, geo.size.width * rowCost / maxCost)
                let segs = row.efforts ?? []
                let usable = max(1, rowW - 1.5 * CGFloat(max(0, segs.count - 1)))
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.08))
                    HStack(spacing: 1.5) {
                        ForEach(Array(segs.enumerated()), id: \.offset) { _, seg in
                            Rectangle()
                                .fill(Self.color(for: row)
                                    .opacity(Self.effortOpacity(seg.effort, rank: seg.rank)))
                                .frame(width: max(0.5, usable * (seg.cost ?? 0) / rowCost))
                        }
                    }
                    .frame(width: rowW, alignment: .leading)
                    .clipShape(Capsule())
                }
            }
            .frame(height: 8)
            Text(Self.usd(row.cost))
                .font(.system(size: 10.5, design: .monospaced))
                .frame(width: 52, alignment: .trailing)
        }
        .padding(.vertical, 1)
        .contentShape(Rectangle())
        .background(RoundedRectangle(cornerRadius: 4)
            .fill(Color.primary.opacity(model.hoveredMixRow == row.name ? 0.05 : 0)))
        .anchorPreference(key: MixAnchorKey.self, value: .bounds) { [row.name: $0] }
        .onHover { inside in
            if inside { model.hoveredMixRow = row.name }
            else if model.hoveredMixRow == row.name { model.hoveredMixRow = nil }
        }
    }

    static func ledgerHeight(_ row: ModelRow) -> CGFloat {
        var lines = CGFloat((row.efforts ?? []).count)
        if row.output != nil { lines += 1 }
        if row.cache_read != nil { lines += 1 }
        return 16 + 7 + lines * 15 + 17                      // header + divider + rows + padding
    }

    static func ledger(_ row: ModelRow) -> some View {
        VStack(alignment: .leading, spacing: 2.5) {
            Text("\(row.name) · \(Self.usd(row.cost)) · \((row.msgs ?? 0).formatted()) msgs")
                .font(.system(size: 10.5, weight: .semibold))
            ForEach(Array((row.efforts ?? []).enumerated()), id: \.offset) { _, seg in
                Self.ledgerLine(Self.effortLabel(seg.effort),
                           "\(Self.usd(seg.cost)) · \((seg.msgs ?? 0).formatted()) msgs")
            }
            Divider().padding(.vertical, 1)
            if let out = row.output { Self.ledgerLine("output", Self.fmtTok(out)) }
            if let cr = row.cache_read { Self.ledgerLine("cache read", Self.fmtTok(cr)) }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(width: 215, alignment: .leading)
        .modifier(FloatingChip(border: Color.primary.opacity(0.14)))
    }

    static func ledgerLine(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).font(.system(size: 9.5)).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value).font(.system(size: 9.5, design: .monospaced))
        }
    }

    static func fmtTok(_ v: Double) -> String {
        if v >= 1e9 { return String(format: "%.1fB tok", v / 1e9) }
        if v >= 1e6 { return String(format: "%.1fM tok", v / 1e6) }
        if v >= 1e3 { return String(format: "%.0fK tok", v / 1e3) }
        return String(format: "%.0f tok", v)
    }

    static func usd(_ v: Double?) -> String {
        guard let v = v else { return "—" }
        if v >= 10 { return "$\(Int(v.rounded()))" }
        return String(format: "$%.2f", v)
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

    private var confirming: Bool { model.confirmTarget == account.uuid }
    private var stash: DisplayDesktop? { account.display?.desktop }

    /// The account you are on, and the only card that lights up. Exactly one surface answers
    /// this: the desktop app when it has been set up, the CLI otherwise. Two lit cards would ask
    /// the reader to work out which one counts, and drift is not what this indicator is for.
    private var isCurrent: Bool {
        // Alone in its section there is no question of which account you are on, so lighting the
        // only card states the obvious in the loudest available color.
        guard (account.isCodex ? model.codex : model.claude).count > 1 else { return false }
        // Codex has no desktop surface, so its own active flag is the whole answer. Letting the
        // desktop rule reach these rows would leave the Codex section with nothing lit as soon as
        // any desktop account was captured.
        if account.isCodex { return account.active == true }
        return model.desktopOnboarded ? (stash?.active == true) : (account.active == true)
    }
    private var cliSwitchable: Bool { account.display?.can_switch == true }
    private var desktopSwitchable: Bool {
        stash?.can_switch == true && model.desktop?.needs_repair != true
    }

    // A switchable card is one control, the way a table row or menu row is: click anywhere on it
    // and this account becomes the one you are on — CLI and desktop app together, because being
    // half-switched is never what anyone wants. The only fork is cost: with the desktop app open
    // the click asks first (it closes live sessions); otherwise it just goes. A card whose CLI
    // side is already active still switches the desktop here when the two have drifted apart.
    var body: some View {
        Group {
            if confirming {
                confirmCard
            } else if cliSwitchable || desktopSwitchable {
                Button {
                    if desktopSwitchable && model.desktop?.needs_confirm == true {
                        model.confirmTarget = account.uuid
                    } else if model.confirmTarget != nil {
                        // A question is open on another card, and this click lands outside it:
                        // that is an answer of "not that", never a request to act here. Acting
                        // would mean an instant switch happens or not depending on whether some
                        // other card was asking — the same click must not carry two meanings.
                        model.confirmTarget = nil
                    } else {
                        Task { await model.switchAll(account) }
                    }
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
        // a confirmation the user walked away from must not be waiting next time the panel opens
        .onDisappear { if confirming { model.confirmTarget = nil } }
    }

    private var confirmCard: some View {
        VStack(alignment: .leading, spacing: 7) {
            card
            Text("Claude Code Desktop will quit and reopen. Make sure your sessions are at a good stopping place.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Spacer()
                Button("Cancel") { model.confirmTarget = nil }
                    .controlSize(.small)
                if cliSwitchable {
                    // the escape valve for "my sessions are mid-flight": the CLI moves now, and a
                    // later click on this same card brings the desktop app across
                    Button("CLI only") {
                        model.confirmTarget = nil
                        Task { await model.switchTo(account.uuid) }
                    }
                    .controlSize(.small)
                }
                Button(cliSwitchable ? "Switch both" : "Move desktop here") {
                    model.confirmTarget = nil
                    Task { await model.switchAll(account) }
                }
                .controlSize(.small)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.08)))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(Color.accentColor.opacity(0.55), lineWidth: 1)
        )
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
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(isCurrent ? AnyShapeStyle(Color.accentColor.opacity(0.12))
                                : AnyShapeStyle(Color.primary.opacity(hovered ? 0.08 : 0.03)))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(Color.accentColor.opacity(isCurrent ? 0.55 : 0), lineWidth: 1)
        )
        .onHover { hovered = $0 }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            // One reserved leading slot for all rows keeps every name on the same left edge and
            // gives the hover ⇄ and the switching spinner a home. Which account you are on is
            // carried by the lit card, not by anything here. The slot is always one Text in one
            // font — a baseline-less placeholder would flip the row's alignment mode on hover and
            // shift every glyph beside it.
            Text(hovered && (cliSwitchable || desktopSwitchable) ? "⇄" : "")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Color.accentColor)
                .opacity(model.switching == account.uuid ? 0 : 1)
                .frame(width: 13, alignment: .leading)
                .overlay(alignment: .leading) {
                    if model.switching == account.uuid { ProgressView().controlSize(.mini) }
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
            if let plan = account.display?.plan, !plan.isEmpty {
                Text(plan)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 1)
                    .background(Capsule().fill(Color.primary.opacity(0.07)))
            }
        }
    }
}

// A stash the tool holds that pairs with no account above, usually because its label matches no
// email. Same card grammar as an account: one control, lit when it is the one installed, ⇄ on
// hover. A click can close live sessions, so with the app open it asks first.
struct StashCard: View {
    @EnvironmentObject var model: Model
    let stash: Stash
    let desktop: Desktop
    @State private var hovered = false

    private var confirming: Bool { model.confirmTarget == "desktop:" + stash.label }
    private var isSwitching: Bool { model.switching == "desktop:" + stash.label }

    var body: some View {
        Group {
            if confirming {
                confirmCard
            } else if stash.can_switch == true {
                Button {
                    if desktop.needs_confirm == true { model.confirmTarget = "desktop:" + stash.label }
                    else if model.confirmTarget != nil { model.confirmTarget = nil }  // dismiss, don't act
                    else { Task { await model.switchDesktop(stash.label) } }
                } label: { card }
                .buttonStyle(.plain)
                .disabled(model.switching != nil || desktop.needs_repair == true)
                .onHover { inside in
                    if inside { NSCursor.pointingHand.set() } else { NSCursor.arrow.set() }
                }
            } else {
                card
            }
        }
        // The panel is torn down and rebuilt as it opens and closes, and a confirmation the user
        // walked away from must not be waiting for them next time: reopening the menu is itself
        // a decision not to answer it.
        .onDisappear { if confirming { model.confirmTarget = nil } }
    }

    private var confirmCard: some View {
        VStack(alignment: .leading, spacing: 7) {
            card
            Text("Claude Code Desktop will quit and reopen. Make sure your sessions are at a good stopping place.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Spacer()
                Button("Cancel") { model.confirmTarget = nil }
                    .controlSize(.small)
                Button("Quit and switch") {
                    model.confirmTarget = nil
                    Task { await model.switchDesktop(stash.label) }
                }
                .controlSize(.small)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.08)))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(Color.accentColor.opacity(0.55), lineWidth: 1)
        )
    }

    private var card: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(hovered && stash.can_switch == true ? "⇄" : "")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Color.accentColor)
                .opacity(isSwitching ? 0 : 1)
                .frame(width: 13, alignment: .leading)
                .overlay(alignment: .leading) {
                    if isSwitching { ProgressView().controlSize(.mini) }
                }
            Text(stash.label).font(.system(size: 13, weight: .semibold))
            if stash.stale == true {
                Text("may have expired")
                    .font(.system(size: 10))
                    .foregroundStyle(sevColor(70))
            }
            Spacer(minLength: 4)
            Text(stash.active == true ? "in use" : "captured")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 7).padding(.vertical, 1)
                .background(Capsule().fill(Color.primary.opacity(0.07)))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(stash.active == true ? AnyShapeStyle(Color.accentColor.opacity(0.12))
                                       : AnyShapeStyle(Color.primary.opacity(hovered ? 0.08 : 0.03)))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(Color.accentColor.opacity(stash.active == true ? 0.55 : 0), lineWidth: 1)
        )
        .onHover { hovered = $0 }
    }
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

struct FooterView: View {
    @EnvironmentObject var model: Model
    @State private var loginItem = SMAppService.mainApp.status == .enabled

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let err = model.actionError {
                Text("⚠ \(err)").font(.system(size: 11)).foregroundStyle(sevColor(95)).lineLimit(3)
            }
            if let err = model.lastError {
                Text("⚠ \(err)").font(.system(size: 10.5)).foregroundStyle(sevColor(70)).lineLimit(2)
            }
            if let old = model.desktop?.stashes.first(where: { $0.stale == true && $0.active == true }) {
                Text("⚠ the desktop app's saved sign-in for \(old.label) is old; if it stops working, re-add it")
                    .font(.system(size: 10.5)).foregroundStyle(sevColor(70)).lineLimit(2)
            }
            if model.payload?.accounts.contains(where: { $0.stale == true }) == true {
                Text("⚠ last known values — rate-limited; updates on the next refresh")
                    .font(.system(size: 10.5)).foregroundStyle(sevColor(70))
            }
            HStack(spacing: 10) {
                TimelineView(.periodic(from: .now, by: 10)) { ctx in
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
                    Section("Refresh every") {
                        ForEach([1, 5, 10, 30], id: \.self) { m in
                            Button {
                                model.intervalMinutes = m
                                model.startTimer()
                            } label: {
                                let name = m == 1 ? "1 minute" : "\(m) minutes"
                                if model.intervalMinutes == m { Text("✓ \(name)") } else { Text(name) }
                            }
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
        return "Updated \(agoText(since: ts))"
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
