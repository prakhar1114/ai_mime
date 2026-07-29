import Foundation
import AppIntents

// MARK: - App Entity & Dynamic Query

public struct WorkflowEntity: AppEntity {
    public static var typeDisplayRepresentation: TypeDisplayRepresentation = "AI Mime Workflow"
    
    public var id: String
    public var name: String
    public var descriptionText: String
    
    public var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(name)", subtitle: "\(descriptionText)")
    }
    
    public static var defaultQuery = WorkflowEntityQuery()
    
    public init(id: String, name: String, descriptionText: String) {
        self.id = id
        self.name = name
        self.descriptionText = descriptionText
    }
}

public struct WorkflowEntityQuery: EntityStringQuery {
    public init() {}

    public func entities(for identifiers: [String]) async throws -> [WorkflowEntity] {
        let all = try await fetchWorkflows()
        return all.filter { identifiers.contains($0.id) }
    }

    public func entities(matching string: String) async throws -> [WorkflowEntity] {
        let all = try await fetchWorkflows()
        if string.isEmpty { return all }
        let lower = string.lowercased()
        return all.filter { $0.name.lowercased().contains(lower) || $0.id.lowercased().contains(lower) }
    }

    public func suggestedEntities() async throws -> [WorkflowEntity] {
        return try await fetchWorkflows()
    }

    private func fetchWorkflows() async throws -> [WorkflowEntity] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let workflowsDir = home.appendingPathComponent("Library/Application Support/AI Mime/workflows")
        
        guard FileManager.default.fileExists(atPath: workflowsDir.path) else {
            return []
        }

        var results: [WorkflowEntity] = []
        let children = try FileManager.default.contentsOfDirectory(at: workflowsDir, includingPropertiesForKeys: nil)

        for child in children {
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: child.path, isDirectory: &isDir), isDir.boolValue {
                let metaPath = child.appendingPathComponent("metadata.json")
                var name = child.lastPathComponent
                var desc = "AI Mime Workflow"
                
                if let data = try? Data(contentsOf: metaPath),
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    if let metaName = json["name"] as? String, !metaName.isEmpty {
                        name = metaName
                    }
                    if let metaDesc = json["description"] as? String {
                        desc = metaDesc
                    }
                }
                results.append(WorkflowEntity(id: name, name: name, descriptionText: desc))
            }
        }
        return results
    }
}

// MARK: - App Intent

public struct RunWorkflowIntent: AppIntent {
    public static var title: LocalizedStringResource = "Run Workflow in AI Mime"
    public static var description = IntentDescription("Executes an AI Mime automation workflow directly via Siri or Shortcuts.")

    @Parameter(title: "Workflow", requestValueDialog: IntentDialog("Which workflow would you like to run?"))
    public var workflow: WorkflowEntity

    @Parameter(title: "Input Parameters (JSON)", default: "{}")
    public var inputParameters: String?

    public init() {}

    public init(workflow: WorkflowEntity, inputParameters: String? = nil) {
        self.workflow = workflow
        self.inputParameters = inputParameters
    }

    public func perform() async throws -> some IntentResult & ProvidesDialog {
        // 1. Check if Siri is enabled in AI Mime settings
        let siriEnabled = await checkSiriSetting()
        guard siriEnabled else {
            return .result(dialog: "Siri automations are currently disabled in AI Mime settings.")
        }

        // 2. Call local backend REST API
        let encodedName = workflow.name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? workflow.name
        guard let url = URL(string: "http://127.0.0.1:58838/api/skills/\(encodedName)/run/stream") else {
            return .result(dialog: "Failed to construct API URL for workflow \(workflow.name).")
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        var paramsObj: [String: Any] = [:]
        if let jsonStr = inputParameters,
           let data = jsonStr.data(using: .utf8),
           let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            paramsObj = parsed
        }
        let bodyPayload: [String: Any] = ["params": paramsObj]
        request.httpBody = try? JSONSerialization.data(withJSONObject: bodyPayload)

        do {
            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                return .result(dialog: "Failed to start workflow \(workflow.name). Please make sure AI Mime is running.")
            }

            var logs: [String] = []
            for try await line in bytes.lines {
                if line.hasPrefix("data: ") {
                    let jsonText = String(line.dropFirst(6))
                    if let data = jsonText.data(using: .utf8),
                       let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        let evType = obj["event"] as? String ?? ""
                        if evType == "error" {
                            let msg = obj["message"] as? String ?? "Unknown error"
                            return .result(dialog: "Workflow \(workflow.name) encountered an error: \(msg)")
                        } else if evType == "done" {
                            let success = obj["success"] as? Bool ?? false
                            if success {
                                return .result(dialog: "Workflow \(workflow.name) completed successfully.")
                            }
                        } else if let lineText = obj["line"] as? String {
                            logs.append(lineText)
                        }
                    }
                }
            }

            return .result(dialog: "Workflow \(workflow.name) finished execution.")
        } catch {
            return .result(dialog: "Error executing \(workflow.name): \(error.localizedDescription)")
        }
    }

    private func checkSiriSetting() async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:58838/api/settings/siri") else { return true }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2.0
        do {
            let (data, response) = try await URLSession.shared.data(for: req)
            if let httpRes = response as? HTTPURLResponse, httpRes.statusCode == 200,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let enabled = json["enabled"] as? Bool {
                return enabled
            }
        } catch {}
        return true
    }
}

// MARK: - App Shortcuts Provider

public struct AIMimeShortcuts: AppShortcutsProvider {
    public static var shortcutTileColor: ShortcutTileColor = .blue

    public static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: RunWorkflowIntent(),
            phrases: [
                "Run \(\.$workflow) in \(.applicationName)",
                "Execute \(\.$workflow) with \(.applicationName)",
                "Ask \(.applicationName) to run \(\.$workflow)"
            ],
            shortTitle: "Run AI Mime Workflow",
            systemImageName: "play.circle.fill"
        )
    }
}

// MARK: - CLI Entrypoint

@main
struct AIMimeIntentsCLI {
    static func main() async {
        let args = CommandLine.arguments
        if args.count > 1, args[1] == "--list" {
            let query = WorkflowEntityQuery()
            if let items = try? await query.suggestedEntities() {
                print("Available Workflows:")
                for item in items {
                    print("- \(item.name) (ID: \(item.id))")
                }
            }
        } else if args.count > 1, args[1] == "--run", args.count > 2 {
            let name = args[2]
            var inputJSON = "{}"
            if args.count > 4, args[3] == "--params" {
                inputJSON = args[4]
            }
            let entity = WorkflowEntity(id: name, name: name, descriptionText: "")
            let intent = RunWorkflowIntent(workflow: entity, inputParameters: inputJSON)
            do {
                let res = try await intent.perform()
                print("Result: \(res)")
                print("Execution complete for: \(name)")
            } catch {
                print("Execution failed: \(error)")
            }
        } else if args.count > 1, args[1] == "--register" {
            AIMimeShortcuts.updateAppShortcutParameters()
            print("Registered App Shortcuts with macOS system.")
        } else if args.count > 1, args[1] == "--unregister" {
            AIMimeShortcuts.updateAppShortcutParameters()
            print("Deregistered App Shortcuts with macOS system.")
        } else {
            print("AIMimeIntents macOS Bridge (AppIntents Framework)")
            print("Usage:")
            print("  AIMimeIntents --list")
            print("  AIMimeIntents --run <workflow_name>")
            print("  AIMimeIntents --register")
            print("  AIMimeIntents --unregister")
        }
    }
}
